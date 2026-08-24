# Session 1 — Colour & orientation correctness

**Branch from:** `feat/responsive-image-renditions`
**Schema change:** none
**Blocks:** sessions 2, 3, 4 (all depend on the dataclass + pipeline version)
**Read first:** `CLAUDE.md` at the repo root, and `README.md` next to this file.

## Why this session exists

Two real defects in the image pipeline, plus one refactor that every later
session needs.

1. **Portrait photos come out sideways.** `app/services/image_vips.py` loads
   with `pyvips.Image.new_from_buffer(file_data, "")` and encodes with
   `strip=True`. A phone photo shot in portrait carries EXIF orientation 6 or
   8 — the pixels are landscape, the tag says "rotate me". Stripping the tag
   without applying it produces a permanently rotated image, and the persisted
   `width`/`height` are wrong (swapped), which then poisons `_filter_renditions`
   (`app/routers/utils.py:273`, which drops width specs wider than the source).

2. **Wide-gamut photos come out washed out.** A Display P3 JPEG from an iPhone
   or a DSLR carries an ICC profile; `strip=True` drops it, and the browser then
   interprets P3 numbers as sRGB. Colours read duller and shifted.

3. **`validate_and_strip_image` already returns a 5-tuple** and later sessions
   add two more values. Convert it to a frozen dataclass now, while the change
   is purely mechanical and reviewable in isolation.

## The critical technical correction

**`image.colourspace("srgb")` does not fix defect 2.** libvips `colourspace()`
converts between *interpretations* (`image.interpretation`). A loaded JPEG
already has `interpretation == "srgb"` regardless of what ICC profile is
embedded — the P3-ness lives in the `icc-profile-data` **metadata**, which
`colourspace()` never reads. srgb→srgb changes zero pixels. The feature would
ship, the tests would pass, and nothing would be fixed.

The correct operation is an ICC transform, and it must run **before** the
`strip=True` encode and **before** rendition generation.

Both operations are needed, for different inputs:

- `icc_transform("srgb", embedded=True)` — handles the actual wide-gamut case
  (P3 / AdobeRGB / any embedded profile). Needs libvips built with **lcms2**.
- `colourspace("srgb")` — handles CMYK and greyscale inputs, which arrive with
  a non-sRGB *interpretation* and would otherwise reach the encoder (and, in
  session 2, the dominant-colour extractor) with the wrong band count.

### Pre-flight check (do this first, it gates the whole session)

```sh
docker compose run --rm --build --no-deps test vips --vips-config | tr ',' '\n' | grep -i lcms
```

Debian's `libvips` normally has lcms2, but confirm rather than assume. If lcms
is absent, stop and report — the ICC half of this session is not implementable
without rebuilding libvips in `Dockerfile.api` / `Dockerfile.worker` /
`Dockerfile.test`, which is a separate decision for the owner.

## Work items

### 1. Convert the return type to a dataclass

In `app/services/image_vips.py`, replace the
`tuple[bytes, str, int, int, dict[str, bytes]]` return with:

```python
@dataclass(frozen=True)
class ProcessedImage:
    buffer: bytes
    content_type: str
    width: int
    height: int
    renditions: dict[str, bytes]
```

Update **both** call sites and every test unpack:

| Site | What it does now |
|---|---|
| `app/routers/upload.py:212` | `await asyncio.to_thread(validate_and_strip_image, file_data, optimization, generate_renditions=thumbnail)` into a 5-name tuple unpack. |
| `app/tasks.py:350` | The **worker's** poster path: `webp_bytes, content_type, width, height, _ = await asyncio.to_thread(validate_and_strip_image, frame_bytes, generate_renditions=False)`. Easy to miss — it is in a different process's code path and no route test covers it. `tests/test_poster.py` does. |
| `tests/test_image_vips.py` | 8 call sites, several with positional unpacks (`_, _, _, _, renditions = ...`). |

Do not rename `validate_and_strip_image` — it is referenced by name in
`CLAUDE.md` and in `docs/`.

### 2. Apply orientation and colour normalization

Order matters. Insert immediately after the successful
`pyvips.Image.new_from_buffer(...)` and **before** the `MAX_IMAGE_PIXELS`
check, the rendition generation, and the max-dim resize:

```python
image = image.autorot()

if image.get_typeof("icc-profile-data") != 0:
    try:
        image = image.icc_transform("srgb", embedded=True)
    except pyvips.Error:
        # An unconvertible/corrupt profile must not fail the upload; leaving
        # the pixels untouched is the same behaviour as before this change.
        logger.warning(...)  # or pass, matching the module's existing style

if image.interpretation != pyvips.Interpretation.SRGB:
    image = image.colourspace(pyvips.Interpretation.SRGB)
```

Rationale for the ordering, worth a comment in the code:

- `autorot()` before the pixel-count check and the resize, because it can swap
  `width`/`height`, and both the check and `_generate_materialized_renditions`
  read those.
- ICC transform before `_generate_materialized_renditions`, so renditions are
  sRGB too — otherwise the thumbnail and the main object disagree on colour.
- `autorot()` also clears the orientation tag from the header, so it composes
  correctly with `strip=True`.

`app/services/image_vips.py` has no logger today. If you add one, use the
module-level `logging.getLogger(__name__)` pattern used elsewhere in
`app/services/`. Do not raise on a failed ICC transform.

### 3. Add `IMAGE_PIPELINE_VERSION` to the dedup signature

This is the non-obvious part, and it is why this session touches
`app/routers/upload.py` beyond the call-site change.

Image upload is idempotent on
`sha256(f"{input_hash}:{optimization}:{visibility}:{thumbnail}")`
(`app/routers/upload.py:172`). Nothing in that signature describes the
*pipeline*. So after this session ships:

- existing records stay un-rotated and un-transformed (expected — there is no
  backfill, and one is not in scope), **and**
- re-uploading the same bytes hits `find_ready_by_hash` and returns the **old,
  broken** record. The fix would be silently unavailable to exactly the users
  who noticed the bug and retried.

Fix: define a module-level constant in `app/services/image_vips.py`

```python
# Bumped whenever a change to this module alters the bytes it produces for
# the same input. Folded into the upload dedup signature so a pipeline fix is
# not masked by a hit on a record produced by the previous pipeline.
IMAGE_PIPELINE_VERSION = 2
```

and fold it into the signature at `app/routers/upload.py:172`:

```python
signature = f"{input_hash}:{optimization}:{visibility}:{thumbnail}:v{IMAGE_PIPELINE_VERSION}"
```

Cost: a one-time re-encode for any image re-uploaded after deploy. That is the
point. Sessions 3 and 4 bump this constant; note that in its docstring.

Update the comment block at `app/routers/upload.py:164-172` to say why the
version is in there.

## Fixtures

`tests/fixtures/` currently holds only `corrupt.bin`, `not_a_video.txt`,
`tiny.gif`, `tiny.jpg`, `tiny.mp4`, `tiny.png`, `tiny.svg`, `tiny.webp`,
`truncated.png`. **Nothing there has an EXIF orientation tag or an ICC
profile**, so neither new test has anything to run against.

Per CLAUDE.md, fixtures are generated once via the Docker test image and
committed — not hand-crafted byte literals. Add a small generator script (put
it in `tests/fixtures/generate_colour_fixtures.py`, or extend whatever
generation script git history shows at commit `a3a9fca`), run it once inside
the test image, and commit the outputs.

Generate two files:

| Fixture | How | Must satisfy |
|---|---|---|
| `exif_orientation_6.jpg` | A deliberately **non-square** image (e.g. 40×20) written as JPEG with EXIF orientation 6 set. pyvips can set it: `image.copy().set("orientation", 6)` then `write_to_file(...)`, or `exiftool`/`PIL` if simpler inside the image. | Non-square is essential — a square fixture cannot distinguish rotated from not. |
| `display_p3.jpg` | A small solid-colour or few-block image tagged with a Display P3 ICC profile (`image.copy().set_blob("icc-profile-data", p3_bytes)`), with a known RGB triple. | The chosen colour must be **saturated** (e.g. pure red `(255,0,0)`), because a desaturated colour moves too little under P3→sRGB for an assertion to be meaningful. |

If a Display P3 profile blob is not available in the image, `lcms2` ships
standard profiles and libvips can synthesize one; failing that, generate an
AdobeRGB-tagged fixture instead and say so in the test docstring — the code
path under test is identical.

## Tests

Add to `tests/test_image_vips.py`. Follow the file's existing style: one
logical assertion per `assert`, setup outside `pytest.raises`.

1. **Orientation is applied.** Load `exif_orientation_6.jpg` (40×20 stored
   pixels), assert the returned `width == 20` and `height == 40` as **separate
   asserts**. This fails on the old code, which returns 40×20.

2. **Orientation propagates to renditions.** Same fixture with
   `generate_renditions=True`; decode the returned `thumbnail` buffer with
   pyvips and assert it is 300×300 (the crop spec) — the point is that the
   rendition was generated from the rotated image, so assert instead on a
   width spec if one is materialized for the fixture's size, or assert the
   main buffer's decoded dimensions match the returned `width`/`height`.

3. **ICC transform actually changes pixels.** This is the test that catches
   the `colourspace()` no-op. Do **not** assert "did not crash".
   - Decode the returned WebP with pyvips.
   - `getpoint(x, y)` at a known block, assert the RGB triple has moved from
     the P3 source value toward the expected sRGB value, with a tolerance
     (WebP is lossy — use `abs(actual - expected) <= 8` per channel, as three
     separate asserts).
   - Compute the expected value once by running the transform in the test
     image and hard-coding it with a comment naming the profile used.

4. **An image with no ICC profile is untouched.** `tiny.png` through the
   pipeline still produces the same dimensions and decodes; guards against the
   `get_typeof` branch misfiring.

5. **A corrupt ICC profile does not fail the upload.** Set a garbage
   `icc-profile-data` blob, assert the call returns normally (no
   `ImageValidationError`). This is the `except pyvips.Error` branch.

6. **Pipeline version is in the dedup signature.** In
   `tests/test_image_idempotency.py`, add a test that monkeypatches
   `IMAGE_PIPELINE_VERSION` (or asserts the computed `content_hash` differs
   across two values) so the fold-in cannot be silently removed.

Also check whether `tests/test_upload_records.py` or
`tests/test_image_renditions.py` assert on specific `content_hash` values —
if so, they need updating for the signature change.

## Non-goals

- No backfill of existing records. Not in scope; do not write a migration or a
  re-encode task.
- No change to `strip=True`. Metadata stripping stays total and
  non-configurable (CLAUDE.md invariant).
- No new config settings. `IMAGE_PIPELINE_VERSION` is a code constant, not an
  env var — an operator has no reason to change it.
- Do not touch `RENDITION_SPECS` or crop behaviour (that is session 3).

## Definition of done

- [ ] lcms confirmed present in the image (or the session stopped and reported).
- [ ] `ProcessedImage` dataclass returned; both call sites and all 8 test
      unpacks updated; `app/tasks.py:350` specifically verified.
- [ ] `autorot()` + ICC transform + interpretation normalization land before
      the pixel check, the resize, and rendition generation.
- [ ] `IMAGE_PIPELINE_VERSION` folded into the dedup signature.
- [ ] Two new fixtures generated inside Docker and committed.
- [ ] Six tests added; each verified to **fail on the pre-change code**
      (`git stash` the source change, confirm red, restore).
- [ ] Full gate green: `pytest -v`, `ruff check .`, `ruff format --check .`,
      `mypy app`.
- [ ] `CLAUDE.md` updated: add an invariant note that the pipeline normalizes
      orientation and colour before stripping, and that
      `IMAGE_PIPELINE_VERSION` participates in image dedup.
