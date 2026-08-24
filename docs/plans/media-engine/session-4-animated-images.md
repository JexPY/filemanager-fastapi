# Session 4 — Animated GIF / WebP

**Depends on:** session 1 (merged). Independent of sessions 2 and 3.
**Schema change:** none.
**Read first:** `CLAUDE.md` at the repo root, and `README.md` next to this file.

## Goal

An uploaded animated GIF becomes an **animated WebP** (typically 70–80% smaller
at similar quality) with its frames, timing and loop count preserved. Animated
WebP input keeps its animation instead of being flattened to frame 0.

This is the highest-risk session in the plan — it is the only one that can turn
a ~400ms upload into a multi-second one — so most of it is about caps and
branch isolation, not about the encode itself.

## Current behaviour, and why it is a behaviour change

`app/services/image_vips.py` loads with `pyvips.Image.new_from_buffer(file_data,
"")`, which defaults to `n=1`: **only the first frame**. GIF is already an
accepted format (`_SIGNATURES` includes `GIF87a`/`GIF89a`), so today a client
uploading an animated GIF gets back a single static WebP and has done so for the
life of the service.

After this session the same upload returns a much larger, animated file. That
is a **contract change for existing clients**, not just a new capability.
Call it out explicitly in the commit message, in `CLAUDE.md`, and in
`docs/FILEMANAGER_INTEGRATION.md`.

## The three things that break if you just pass `n=-1`

libvips represents a multi-page image as a **single tall vertical strip** of
frames, with the real frame height in the `page-height` metadata. So
`image.height` becomes `frames × frame_height`. Three existing pieces of the
function then misread it:

1. **The pixel-count guard** (`image_vips.py:94`):
   `width * height > settings.MAX_IMAGE_PIXELS`. A 30-frame 800×600 GIF reads
   as 800×18000 = 14.4M pixels. It does not trip the 50M default, but it is
   measuring the wrong thing, and a longer clip trips it for the wrong reason
   with a message about "dimensions" that names a height the user never had.

2. **The max-dim resize**: `image.resize(scale)` scales the strip as one image.
   The arithmetic happens to work for uniform scaling, but `page-height` is not
   updated by `resize`, so the output's frame boundaries are wrong and the
   saved WebP is corrupt or single-frame.

3. **`_generate_materialized_renditions`**: `thumbnail_image(300, height=300,
   crop=CENTRE)` on a 800×18000 strip crops a 300×300 window out of the middle
   of the *filmstrip* — a slice spanning two frames.

Session 1's `autorot()` is a fourth: rotating a frame strip as one image is
meaningless. **Skip `autorot()` for animated input** (animated GIFs do not
carry meaningful EXIF orientation anyway) and say so in a comment.

## Design

Add a dedicated branch, entered as early as possible. Do not thread an
`is_animated` flag through the existing linear path.

```python
def validate_and_strip_image(...) -> ProcessedImage:
    if sniff_format(file_data) is None:
        raise ImageValidationError(...)

    # Probe page count cheaply, then take one of two paths.
    ...
    if n_pages > 1:
        return _process_animated(file_data, n_pages, ...)
    # existing still-image path, unchanged
```

**Detecting animation:** load with `n=-1` and read
`image.get_n_pages()` (or `image.get("n-pages")`, guarded by
`get_typeof("n-pages") != 0`). Do this in a `try/except pyvips.Error` that
falls back to the still path — a malformed page count must not 500. Note this
also catches **animated WebP input**, which `sniff_format` reports as plain
`"webp"`; that is intended.

### Caps — implement these before the encode

Two new settings in `app/config.py`, following the file's existing `Field(...)`
+ comment style, added to `.env-example` and the `readme.md` env table:

```python
# Maximum frames accepted in an animated image. Beyond this the upload is
# rejected (400) rather than queued -- animated encoding runs on the
# synchronous request path and there is no async image pipeline.
IMAGE_ANIMATION_MAX_FRAMES: int = Field(default=300)

# Maximum frame_width * frame_height * frames. Bounds total encode work
# independently of frame count, so 500 tiny frames and 20 large ones are
# both governed.
IMAGE_ANIMATION_MAX_TOTAL_PIXELS: int = Field(default=100_000_000)
```

Validation order inside `_process_animated`:

1. `frame_width * frame_height > settings.MAX_IMAGE_PIXELS` →
   `ImageValidationError` (per-frame budget, reusing the existing setting with
   the **frame** height, not the strip height).
2. `n_pages > IMAGE_ANIMATION_MAX_FRAMES` → `ImageValidationError` with a
   message naming the limit.
3. `frame_w * frame_h * n_pages > IMAGE_ANIMATION_MAX_TOTAL_PIXELS` → same.

`ImageValidationError` surfaces as a generic 400 (`"Invalid or unsupported
image"`) at `app/routers/upload.py:217` — the real reason is logged
server-side. That is the existing sanitization contract; do not add a
detailed client-facing message.

### Resizing

Use `pyvips.Image.thumbnail_buffer(file_data, target_width, n=-1,
size=pyvips.Size.DOWN)` rather than `resize()`. `thumbnail_buffer` understands
`page-height` and maintains it across the resize; `resize()` does not. Cap at
the same `max_dim` the still path uses for the requested `optimization`
profile.

### Renditions

Materialized renditions of an animated image must be **static**, generated from
frame 0. The cleanest way is a second, cheap load at `n=1`:

```python
still = pyvips.Image.new_from_buffer(file_data, "")  # frame 0 only
renditions = _generate_materialized_renditions(still, still.width)
```

This reuses the existing tested function with no changes and no strip-aware
special cases. Note in a comment why the double load is deliberate.

### Loop and frame timing

libvips carries these as image metadata (`loop`, `delay`). **Verify
empirically whether `strip=True` on `webpsave` drops them** — write the test
first, decode the output, and assert `n-pages > 1` and the loop value survives.

If `strip=True` does drop them, the fallback is to re-set them on a copy before
saving:

```python
out = image.copy()
if image.get_typeof("loop") != 0:
    out.set("loop", image.get("loop"))
if image.get_typeof("delay") != 0:
    out.set("delay", image.get("delay"))
```

Do not switch animated output to `strip=False` as a shortcut — that would
reintroduce EXIF/GPS/ICC into stored objects and violate a CLAUDE.md invariant
(`strip=True` drops *all* metadata, intentionally, on every re-encode).

### Encode effort

Animated encoding multiplies per-frame cost by frame count. Use a **low
effort** for the animated path (start at `effort=2`) regardless of the
optimization profile, and measure. The still path's effort tuning
(`balanced`=4, `quality`=6) was calibrated for one frame; reusing it here is
how a 30-frame clip becomes a 10-second request.

### Pipeline version

Bump `IMAGE_PIPELINE_VERSION` (session 1's constant). If session 3 has merged
it is already at 3, so go to 4; otherwise 3. Output bytes change for every GIF.

## Mandatory measurement gate

Measure a realistic animated GIF — roughly 480×270, 30–60 frames, a few hundred
KB — end to end through the running stack, using the `time.perf_counter`
instrumentation method CLAUDE.md documents.

- Total upload **< 2s** → ship it.
- **≥ 2s** → stop and report the numbers. The options at that point are lower
  caps, lower effort, or moving animated encoding to the TaskIQ worker — and
  the last one is a real architectural change (an async image pipeline, a
  `processing` state for images, a poll endpoint) that is **out of scope for
  this session** and belongs to the owner.

Record the measured frame count, input size, output size and wall time in the
commit message.

## Fixtures

`tests/fixtures/tiny.gif` exists but is single-frame. Generate and commit:

- `animated.gif` — small (e.g. 32×32), 4–6 frames, non-uniform frame delays,
  loop count set. Non-uniform delays matter: they are what proves `delay`
  survived rather than being regenerated as a constant.
- `animated.webp` — the same content as animated WebP, to cover animated-WebP
  *input*.

Generate inside the Docker test image and commit the outputs (per CLAUDE.md,
fixtures are generated once, not hand-crafted byte literals).

## Tests

In `tests/test_image_vips.py`:

- An animated GIF produces a WebP whose decoded `n-pages` equals the input's
  frame count (separate asserts for `content_type` and page count).
- Frame delays survive: decode the output, read `delay`, assert it matches the
  input's non-uniform pattern.
- Loop count survives.
- The returned `width`/`height` are the **frame** dimensions, not the strip
  dimensions. This is the single most important assertion in the session — a
  wrong height here propagates into the DB and then into `_filter_renditions`.
- Renditions of an animated upload are **static** (decoded `n-pages == 1`) and
  300×300.
- An animated WebP input stays animated.
- A single-frame GIF (`tiny.gif`) still takes the still path and is unchanged
  — regression guard on the branch condition.
- Frame-count cap: monkeypatch `IMAGE_ANIMATION_MAX_FRAMES` to 2, assert
  `ImageValidationError`. Setup outside the `pytest.raises` block.
- Total-pixel cap: same shape, monkeypatching
  `IMAGE_ANIMATION_MAX_TOTAL_PIXELS`.
- Per-frame pixel cap uses frame height, not strip height: monkeypatch
  `MAX_IMAGE_PIXELS` to a value between `frame_w*frame_h` and
  `frame_w*frame_h*n_pages`, assert the upload **succeeds** (this fails on a
  naive `n=-1` implementation).

In `tests/test_routes_image_upload.py`: an animated GIF upload returns a
successful response and a record whose `width`/`height` are frame dimensions.

## Non-goals

- **No async image pipeline.** Over-cap input is rejected with a 400, not
  queued. Adding a `processing` state for images touches the record state
  machine, `GET /tasks/{id}`, and the worker — a separate project.
- No per-request "animate: true/false" parameter. Animation is a property of
  the input, not a client choice. (If the owner later wants "flatten this GIF",
  that is a new parameter and a new dedup signature component.)
- No APNG support (`sniff_format` does not distinguish APNG from PNG, and
  libvips PNG loading is single-page).
- No animated renditions.

## Definition of done

- [ ] Animated input takes a separate branch; the still path is byte-identical
      for single-frame input (regression test proves it).
- [ ] `autorot()` skipped for animated, with a comment.
- [ ] All three caps enforced, with the per-frame budget using frame height.
- [ ] Two new settings in `app/config.py`, `.env-example`, and the `readme.md`
      env table.
- [ ] `thumbnail_buffer(n=-1)` used for resizing, not `resize()`.
- [ ] Renditions static, generated from a separate `n=1` load.
- [ ] Loop and delay verified preserved by test, with the metadata re-set
      fallback applied if `strip=True` drops them; `strip=True` retained.
- [ ] Measurement gate met (< 2s) or escalated with numbers.
- [ ] `IMAGE_PIPELINE_VERSION` bumped.
- [ ] Two fixtures generated in Docker and committed.
- [ ] Full gate green: `pytest -v`, `ruff check .`, `ruff format --check .`,
      `mypy app`.
- [ ] The **behaviour change for existing GIF clients** documented in
      `CLAUDE.md`, `docs/FILEMANAGER_INTEGRATION.md`, and the commit message.
