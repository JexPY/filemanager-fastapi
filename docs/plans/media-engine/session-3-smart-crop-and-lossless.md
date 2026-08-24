# Session 3 — Smart attention crop + `lossless` profile

**Depends on:** session 1 (merged). Independent of sessions 2 and 4.
**Schema change:** none.
**Read first:** `CLAUDE.md` at the repo root, and `README.md` next to this file.

Two unrelated encode-quality features, grouped because both change the bytes
the pipeline produces and both need the same measurement gate.

---

## Part A — Smart attention crop

### What changes

`_generate_materialized_renditions` (`app/services/image_vips.py:44`) currently
hardcodes `crop=pyvips.Interesting.CENTRE` for square specs. A centre crop of a
portrait photo routinely cuts off the subject's head. `Interesting.ATTENTION`
runs libvips' saliency search and keeps the focal region.

### The one structural trap

Do **not** replace `RenditionSpec.crop: bool` with a `crop_mode` string.
`crop` is read as "is this a square-cropped spec?" by two consumers outside
this module:

- `app/services/metadata/types.py:128` — `_filtered_renditions` skips width
  specs (`not spec.crop`) wider than the source image.
- `app/routers/utils.py:277` — `_filter_renditions`, the same logic for upload
  responses.

Changing the field's type or meaning breaks both silently (a truthy non-empty
string makes every width spec look like a crop spec, so the
wider-than-source filter stops firing and responses start advertising
renditions that were never encoded).

**Instead: keep `crop: bool` exactly as-is, and add a new field**

```python
# Only consulted when `crop` is True. "attention" runs libvips' saliency
# search so a portrait's subject survives a square crop; "centre" is the
# cheap geometric fallback.
crop_mode: str = "attention"
```

Consume it only inside `_generate_materialized_renditions`:

```python
interesting = (
    pyvips.Interesting.ATTENTION if spec.crop_mode == "attention" else pyvips.Interesting.CENTRE
)
```

No env var. `IMAGE_SMART_CROP` was considered and rejected in planning — two
knobs for one decision (see `README.md`).

### Mandatory measurement gate

`ATTENTION` runs an extra analysis pass. The thumbnail rendition currently
measures **~39ms** on a real 1920×1280 photo (CLAUDE.md's latency table), on
the synchronous request path.

Measure before and after, using the same method CLAUDE.md documents: temporary
`time.perf_counter` instrumentation around the rendition loop, logged to a
dedicated logger, real upload through the running stack (`docker compose up -d
--build --force-recreate`), then `git checkout` the instrumentation away.

- Delta **< 50ms** → ship it.
- Delta **≥ 50ms** → stop, report the number, and let the owner decide.
  A plausible middle path is attention on `thumbnail` only (it is the sole
  cropped spec today anyway), which is what the default above already gives.

Record the measured before/after numbers in the commit message.

### Testing note

**Do not byte-assert thumbnails.** Attention results shift between libvips
versions. Assert structural properties instead: output is 300×300, output is a
valid WebP, and — the actual behavioural test — build a fixture with an
off-centre high-contrast subject (e.g. a small bright block in the top-left of
an otherwise flat field), crop it, and assert the mean brightness of the
attention crop differs from the centre crop of the same image. That is a real
assertion about behaviour that survives version drift.

### Pipeline version

Bump `IMAGE_PIPELINE_VERSION` (session 1's constant in
`app/services/image_vips.py`) to `3`. Cropped bytes change, so a re-upload must
not dedup onto a centre-cropped record.

---

## Part B — `lossless` optimization profile

### What changes

Add `lossless` as a fourth value of `optimization` on the image routes, for
diagrams, screenshots, logos and icons where lossy WebP artifacts around sharp
edges are unacceptable.

### Sites to touch

| Site | Current |
|---|---|
| `app/routers/upload.py:440` | `optimization: Literal["size", "balanced", "quality"] = Form(...)` on `POST /upload/image` |
| `app/routers/upload.py:599` | the same on `POST /upload/images` |
| `app/routers/upload.py:501` | a hand-written OpenAPI `"enum": [...]` block that must be kept in sync |
| `app/services/image_vips.py:69` | `_get_optimization_params` |

**Leave `POST /upload/video`'s `Literal["balanced", "quality"]`
(`app/routers/upload.py:675`) alone** — it is a separate ffmpeg profile set and
`lossless` is meaningless there.

### `_get_optimization_params` needs a real branch, not a fourth tuple

It currently returns `(q_value, max_dimension, effort)` and every caller passes
all three into `write_to_buffer(".webp", Q=…, strip=True, effort=…,
smart_subsample=True)`. In lossless mode:

- `smart_subsample` is a **chroma-subsampling** control and is meaningless —
  lossless WebP has no chroma subsampling. Passing it is harmless but
  misleading; drop it for this profile.
- `Q` is not quality in lossless mode; libwebp reuses it as a
  compression-effort-ish "level" knob. Passing `Q=95` is not "high quality",
  it is "slow".
- `lossless=True` must be passed to `write_to_buffer`.

Return a small frozen dataclass instead of a widening tuple:

```python
@dataclass(frozen=True)
class _EncodeParams:
    q: int
    max_dim: int
    effort: int
    lossless: bool = False
```

and build the kwargs for `write_to_buffer` from it.

### The hard constraint nobody remembers: WebP maxes out at 16383px

libwebp refuses any dimension above **16383**. The current profiles cap at
1280 / 1920 / 3840 so this never comes up. A "lossless means don't resize"
implementation would hit it on large scans and fail the encode with an opaque
pyvips error.

**Give `lossless` an explicit `max_dim` — recommend 4096.** Lossless is about
pixel fidelity, not resolution preservation, and 4096 is well clear of the
limit. Document the cap in the OpenAPI description for the parameter, because
it is genuinely surprising.

### Renditions stay lossy

`_generate_materialized_renditions` must **not** inherit `lossless`. A lossless
300×300 thumbnail is several times larger for no perceptible gain, and
renditions are CDN accelerators where size is the whole point. The rendition
loop reads `RenditionSpec`, not the optimization profile, so this is already
true — just add a comment saying it is deliberate so a later session does not
"fix" it.

### Size expectations

Lossless WebP on a **photo** is typically 3–10× the Q85 lossy size and much
slower to encode. That is the caller's choice and the parameter is opt-in, so
no guard is required beyond the existing `MAX_IMAGE_PIXELS` (50M) and
`MAX_IMAGE_UPLOAD_BYTES` (25MB). But document it plainly in
`docs/FILEMANAGER_INTEGRATION.md`: *lossless is for graphics with flat colour
and sharp edges; using it on photographs will produce very large files.*

Measure one photo and one screenshot at `lossless` and record both output sizes
and encode times in the commit message.

### Dedup

`optimization` is already part of the dedup signature
(`app/routers/upload.py:172`), so `lossless` is a genuinely distinct upload
from `balanced` for the same bytes. Nothing to do.

---

## Tests

- `tests/test_image_vips.py`
  - `optimization="lossless"` returns a valid WebP; assert the container magic
    (`RIFF`) and that the output differs byte-wise from the `balanced` encode
    of the same fixture.
  - A synthetic flat-colour-with-sharp-edges fixture round-trips
    **pixel-identically** at `lossless` (decode the output, `getpoint` a few
    known pixels, assert exact equality — this is the actual guarantee, and
    the only assertion that proves `lossless=True` reached libwebp).
  - The same fixture at `balanced` is **not** pixel-identical (guards against
    the flag being ignored).
  - Attention crop: the off-centre-subject behavioural test described above.
  - Renditions of a `lossless` upload are still lossy (assert the thumbnail is
    materially smaller than a lossless encode of the same 300×300 region, or
    simply assert the rendition bytes differ from a lossless re-encode).
- `tests/test_routes_image_upload.py` — `optimization=lossless` is accepted by
  both `/upload/image` and `/upload/images`; an invalid value still 422s.
- `tests/test_image_idempotency.py` — `lossless` and `balanced` on identical
  bytes produce two distinct records.

## Non-goals

- No `IMAGE_SMART_CROP` env var, no `IMAGE_RENDITION_WIDTHS` (both rejected in
  planning — see `README.md`).
- No new rendition specs.
- No change to video's `optimization`.
- No lossless for renditions.

## Definition of done

- [ ] `crop_mode` added as a **new** field; `crop: bool` untouched; both
      external consumers (`types.py:128`, `utils.py:277`) verified unaffected.
- [ ] Attention-crop delta measured on a real photo and under 50ms — or
      escalated with the number.
- [ ] `lossless` accepted on both image routes, the hand-written OpenAPI enum
      at `upload.py:501` updated, video's Literal untouched.
- [ ] `_EncodeParams` dataclass replaces the 3-tuple; `smart_subsample` not
      passed in lossless mode; `max_dim` capped at 4096 with the 16383 limit
      explained in a comment.
- [ ] Pixel-identity test proves `lossless=True` actually reached libwebp.
- [ ] `IMAGE_PIPELINE_VERSION` bumped to 3.
- [ ] Full gate green: `pytest -v`, `ruff check .`, `ruff format --check .`,
      `mypy app`.
- [ ] `docs/FILEMANAGER_INTEGRATION.md` documents the `lossless` profile,
      its 4096px cap, and its size characteristics.
- [ ] `CLAUDE.md`'s optimization-profile list updated with `lossless`, and the
      crop-mode note added to the renditions section.
