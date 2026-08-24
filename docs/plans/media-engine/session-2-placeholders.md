# Session 2 — LQIP placeholders (`dominant_color` + `blur_data_url`)

**Depends on:** session 1 (merged). Requires `ProcessedImage` and
`IMAGE_PIPELINE_VERSION` to already exist.
**Schema change:** yes — Alembic revision `0008`.
**Read first:** `CLAUDE.md` at the repo root, and `README.md` next to this file.

## Goal

Every ready image record carries two tiny, self-contained placeholder values so
a consumer can paint something meaningful before the real bytes arrive:

- `dominant_color` — average colour as a 7-char hex string, e.g. `#1e293b`.
  For a solid background behind a loading image.
- `blur_data_url` — a 16px WebP encoded as a `data:image/webp;base64,…` URI.
  For a blurred-up preview (Next.js `placeholder="blur"` and equivalents).

This is the widest session in the plan: one migration, plus the full path from
libvips through the store, the fake, the schemas, and both response shapes.

## Design decisions already made

Do not re-open these.

1. **Both fields are exposed regardless of `visibility`.** `to_public()`
   currently gates `storage_key` / `renditions` / `thumbnail_url` /
   `poster_url` behind `visibility == "public"` (see
   `app/services/metadata/types.py:_populate_ready_urls`). These two fields
   leak nothing — no storage key, no URL, no cross-tenant data — and a private
   record needs a placeholder exactly as much as a public one. Gate on
   `status == "ready"` and `kind == "image"` only. This deliberately breaks the
   "extras are public-only" symmetry; write a comment saying so.

2. **They ride `**kwargs`, not new named parameters.** `MetadataStore.create()`
   (`app/services/metadata/store.py:31`) already carries 12 named params plus
   `**kwargs: object`, and `callback_url` / `renditions` already come through
   `kwargs`. Adding two more named params breaks the ≤13 bound (SonarQube
   `python:S107`, a CLAUDE.md invariant). Follow the existing pattern exactly:
   `dominant_color = kwargs.get("dominant_color")`.

3. **No pre-blur.** Store the raw 16px image; the consumer applies
   `filter: blur(...)` in CSS. Pre-blurring costs encode time, makes the
   payload larger (blur destroys the high-frequency detail WebP compresses
   well, but the resulting image is not smaller in practice at this size), and
   removes the consumer's control over blur radius. Document this in
   `docs/FILEMANAGER_INTEGRATION.md`.

4. **Posters get them too.** A video poster is a `kind=image` row
   (`app/tasks.py:_extract_and_store_poster`), and a blur placeholder for a
   video player's poster frame is exactly as useful. The extraction is
   unconditional in `validate_and_strip_image`, so the poster path gets the
   values for free; the work is passing them into the `store.create(...)` call
   at `app/tasks.py:357`.

## Work items

### 1. Extraction in `app/services/image_vips.py`

Extract from the **normalized** image (after session 1's `autorot()` + ICC
transform + `colourspace`), and **before** the max-dim resize — you want the
placeholder to describe the image the user will see, and the resize only
changes resolution, not colour.

```python
def _extract_placeholders(image: pyvips.Image) -> tuple[str, str]:
    """Return (dominant_color_hex, blur_data_url) for a normalized sRGB image."""
```

- **Flatten alpha first.** A transparent PNG averaged with its alpha channel
  gives a meaningless colour and a blur tile with garbage edges. Use
  `image.flatten(background=[255, 255, 255])` when `image.hasalpha()`.
- **Dominant colour:** per-band average over the flattened image —
  `image[0].avg()`, `image[1].avg()`, `image[2].avg()` — clamped to 0-255,
  rounded, formatted `f"#{r:02x}{g:02x}{b:02x}"`. Session 1's `colourspace`
  normalization guarantees ≥3 bands, so a greyscale input is already expanded;
  add a defensive guard anyway if band count < 3.
- **Blur tile:** `image.thumbnail_image(16, height=16, size=pyvips.Size.DOWN,
  crop=pyvips.Interesting.NONE)` → `write_to_buffer(".webp", Q=20, strip=True,
  effort=0)` → `base64.b64encode` → `f"data:image/webp;base64,{b64}"`.
  `effort=0` deliberately: at 16px the file-size difference between effort 0
  and 6 is a handful of bytes and the point is that this is nearly free.

Add both to the `ProcessedImage` dataclass:

```python
dominant_color: str | None = None
blur_data_url: str | None = None
```

**Bump `IMAGE_PIPELINE_VERSION`** — no, do **not** bump it here. This session
does not change the bytes of the stored objects, only adds new metadata. A bump
would force a needless re-encode of every re-uploaded image. Existing records
simply carry `NULL` for both fields; that is the documented backward-compatible
outcome. (This is the one place in the plan where *not* bumping is the correct
call — say so in the commit message so a reviewer does not flag it.)

**Measure the cost.** Instrument with `time.perf_counter` around
`_extract_placeholders` and run a real 1920×1280 upload through the stack.
Expected < 5ms against a ~404ms `balanced` total (see the latency table in
CLAUDE.md). If it exceeds ~15ms, stop and report before continuing — the
synchronous request path is the constraint that shaped this whole subsystem.

### 2. Migration `migrations/versions/0008_uploads_blur_and_color.py`

Copy the style of `0007_uploads_renditions.py` exactly (module docstring
explaining the columns, `revision`/`down_revision`/`branch_labels`/`depends_on`
module-level annotations, raw `op.execute`).

```python
revision: str = "0008_uploads_blur_and_color"
down_revision: str | None = "0007_uploads_renditions"


def upgrade() -> None:
    op.execute("ALTER TABLE uploads ADD COLUMN dominant_color varchar(7)")
    op.execute("ALTER TABLE uploads ADD COLUMN blur_data_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS dominant_color")
    op.execute("ALTER TABLE uploads DROP COLUMN IF EXISTS blur_data_url")
```

Both nullable, no default, no backfill. Existing rows get `NULL`.

Verify against the real DB:
```sh
docker compose run --rm migrate
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic downgrade -1 && docker compose run --rm migrate
```

### 3. Persistence — `app/services/metadata/`

All four of these must land in the **same commit** or every read breaks:

- `postgres.py:29` `_COLUMNS` — append `dominant_color, blur_data_url`
  (order is positional-by-name via `_row_to_record`, but keep it consistent:
  add after `renditions`, before `created_at, updated_at`).
- `postgres.py:36` `_JOIN_COLUMNS` — the same two, prefixed `u.`.
  **Careful:** the join alias `p.storage_key AS poster_storage_key` must stay
  last, and the new columns must be on `u.` not `p.`.
- `postgres.py:68` `_row_to_record` — two new `row[...]` reads.
- `postgres.py:176` `create()` — pull from `kwargs`, extend the `INSERT`
  column list, the `VALUES ($1…$17)` placeholders, and the positional argument
  list. Count the placeholders carefully; this is the classic off-by-one site.

`types.py`: add the two fields to the `UploadRecord` dataclass. It is
`@dataclass(frozen=True)` with defaulted trailing fields
(`poster_storage_key`, `renditions`) — add the new ones **after** those, with
`= None` defaults, so no positional construction breaks.

`store.py`: the ABC's `create()` signature is unchanged (the fields ride
`**kwargs`), but update its docstring to mention them alongside `callback_url`
and `renditions`.

`tests/fakes.py:149` `InMemoryMetadataStore.create` — mirror the Postgres
behaviour: `dominant_color = kwargs.get("dominant_color")` and pass into the
`UploadRecord(...)` construction. There is no `app/services/metadata/memory.py`;
this file is the only in-memory implementation.

### 4. `to_public()` — `app/services/metadata/types.py`

Add to the base `data` dict (not inside the public-only branch), guarded on
`kind == KIND_IMAGE`:

```python
if self.kind == KIND_IMAGE and self.status == STATUS_READY:
    data["dominant_color"] = self.dominant_color
    data["blur_data_url"] = self.blur_data_url
```

Keep `to_public()`'s cognitive complexity ≤ 15 — it is already close. If adding
this branch pushes it over, extract a `_populate_placeholders(data)` helper
alongside the existing `_populate_thumbnail_url` / `_populate_poster_url`.

### 5. Response plumbing — routers and schemas

- `app/routers/upload.py:81` `_ProcessedImageData` — add both fields.
- `app/routers/upload.py:233` — populate them from the `ProcessedImage` result.
- `app/routers/upload.py:311` `_create_image_record` — pass both
  into `store.create(...)` as kwargs.
- `app/routers/utils.py:304` `_image_response` — this function already takes
  **12 parameters**. Do **not** add two more; that hits the ≤13 bound and it is
  already unwieldy. Instead pass them through the existing
  `_ProcessedImageData` / a small `_Placeholders` dataclass, or (simpler) add a
  single `placeholders: tuple[str | None, str | None] | None = None` param.
  Whichever you pick, note it in the commit message — a reviewer will look here.
  Emit both keys unconditionally when present (not gated on `visibility`, per
  decision 1).
- **The dedup return path**: `app/routers/upload.py:187` returns
  `_image_response(existing.id, …)` for an idempotency hit. Pass
  `existing.dominant_color` / `existing.blur_data_url` there too, or a deduped
  upload silently drops the placeholders that a fresh upload returns.
- `app/schemas.py:61` `_BaseImageUploadResponse` and `app/schemas.py:180`
  `FileRecord` — add both as `str | None = Field(default=None, description=…)`.
  Both routes use `response_model_exclude_unset=True`, so absent stays absent.
  Follow the file's convention of module-level `_*_DESC` constants for
  descriptions used in more than one model (SonarQube `python:S1192`).
- `app/tasks.py:357` — pass both into the poster's `store.create(...)`.

## Payload budget

`blur_data_url` rides **every** response that serializes a record: `GET /files`
(paginated), `POST /files/batch` (up to 200 ids), `GET /files/{id}`, both
upload responses, **and every webhook body** — which means it is inside the
HMAC-signed payload and any log of it.

At 16px WebP the base64 should land around 200–600 bytes. Budget check: 200
records × 600 bytes ≈ 120KB added to one batch response. Acceptable, but only
if the tile actually stays small.

**Add a test asserting `len(blur_data_url) <= 1200`** for a real fixture. If a
future change (a larger tile, a higher Q, pre-blurring) inflates it, that test
is the tripwire. If the measured size on real photos runs materially higher
than expected, stop and report rather than shipping — the fallback design is a
`?include=blur` opt-in on list endpoints, which is a bigger contract change and
should be the owner's call.

## Tests

- `tests/test_image_vips.py` — `dominant_color` matches `^#[0-9a-f]{6}$`; a
  known solid-colour fixture yields approximately that colour (tolerance per
  channel, separate asserts); `blur_data_url` starts with
  `data:image/webp;base64,`; the decoded payload is a valid WebP (`RIFF` magic)
  and ≤ 16px on its long edge; the length bound above; a transparent PNG does
  not produce a black/garbage dominant colour (the flatten path).
- `tests/test_metadata_store_pg.py` (`pg_integration`) — full create→get
  round-trip preserving both fields, including `NULL` for a record created
  without them.
- `tests/test_metadata_store.py` — same round-trip against
  `InMemoryMetadataStore`, so the fake cannot drift from Postgres.
- `tests/test_upload_records.py` / `tests/test_routes_image_upload.py` — the
  upload response carries both fields; a **private** upload also carries them
  (this is the decision-1 regression guard); a **deduped** upload carries them.
- `tests/test_files_listing.py` / `tests/test_files_batch.py` — present in the
  serialized record.
- `tests/test_poster.py` — a generated poster record carries them.

## Non-goals

- No backfill of existing rows. `NULL` is the contract for pre-existing records
  and consumers must handle it.
- Do not bump `IMAGE_PIPELINE_VERSION` (see work item 1).
- No new config settings. Extraction is unconditional.
- Do not touch crop mode, `optimization` profiles, or animation.

## Definition of done

- [ ] Migration `0008` applies, `downgrade -1` works, re-applies cleanly.
- [ ] `_COLUMNS`, `_JOIN_COLUMNS`, `_row_to_record`, `create()` all updated in
      one commit; placeholder count in the `INSERT` verified.
- [ ] `tests/fakes.py` mirrors Postgres; round-trip tests pass on both.
- [ ] Both fields present for private *and* public records, fresh *and*
      deduped uploads, and posters.
- [ ] `blur_data_url` size bound asserted.
- [ ] Extraction measured at < 15ms on a real 1920×1280 photo; number recorded
      in the commit message.
- [ ] Full gate green: `pytest -v`, `ruff check .`, `ruff format --check .`,
      `mypy app`.
- [ ] `docs/FILEMANAGER_INTEGRATION.md` documents both fields under the image
      upload contracts (section 2) and the resolve-once model (section 5),
      including that the consumer applies its own CSS blur.
- [ ] `readme.md` env/response tables updated if they enumerate record fields.
- [ ] `CLAUDE.md` updated with the placeholder invariant and the
      exposed-regardless-of-visibility rationale.
