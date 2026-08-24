# Next-Gen Media Engine — execution plan

Four independent work packages, each sized for **one fresh session**. Hand a
single `session-N-*.md` to a new session; do not hand it more than one.

Each session file is self-contained: scope, exact files and line anchors,
design decisions already made (with rationale, so they are not re-litigated),
explicit non-goals, and a verification checklist. They are written against the
repo as of branch `feat/responsive-image-renditions`.

## Sequencing

```
Session 1  Colour & orientation correctness        no schema change   ← start here
   │       (autorot, ICC→sRGB, ProcessedImage dataclass, pipeline version)
   ▼
Session 2  LQIP placeholders                       migration 0008
   │       (dominant_color, blur_data_url, full persistence + response plumbing)
   ▼
Session 3  Smart crop + lossless profile           no schema change
   │
   ▼
Session 4  Animated GIF/WebP                       no schema change
```

**Sessions 1 and 2 are hard-ordered.** Session 1 converts
`validate_and_strip_image` to return a dataclass and introduces
`IMAGE_PIPELINE_VERSION`; session 2 adds two fields to that dataclass and bumps
that constant. Sessions 3 and 4 each depend on 1 (dataclass + pipeline version)
but are independent of 2 and of each other — either order, or in parallel on
separate branches.

Sessions 3 and 4 are the two that change encode cost on the **synchronous
request path**. Both carry a mandatory measurement gate. If either fails its
gate, ship the rest and park it — that is a normal outcome, not a failure.

## Non-negotiables (every session)

These come from `CLAUDE.md` at the repo root, which a session in this repo
loads automatically. Repeated here so a session file works standalone.

1. **Docker only.** No host venv, no `brew install libvips/ffmpeg`, no
   `uv run` on the host. Everything runs in the `test` compose service.
2. **The `--build` flag is load-bearing.** `Dockerfile.test` does `COPY . .`
   with no source bind mount, so `docker compose run` without `--build`
   silently tests the *previous* image. Never pipe the build (`| tail`) —
   that takes the exit code from `tail` and a failed build produces a
   green-looking run against stale code.
3. **`ruff format` inside the test service rewrites the image's baked copy,
   not the host tree.** To fix host files, bind-mount the repo at `/src`
   (see the command block below).
4. **`ruff format` covers Markdown, not just `.py`.** Verified this session:
   `ruff format --check .` formats Python code blocks inside **every** `.md`
   file, including these plan documents and anything added to `docs/`. A code
   snippet in a doc can fail the CI format gate exactly like a source file.
5. **Alembic owns the `uploads` schema.** New revisions only, hand-written
   SQL (no autogenerate — there is no ORM model layer), always with a
   `downgrade()`.
6. **Anti-bloat: a `.py` over ~400 lines gets refactored, not extended.**
   Note `app/routers/upload.py` is already **927 lines**. No session here is
   required to split it, but do not make it materially worse; prefer putting
   new helpers in `app/services/`.
7. **SonarQube invariants** (from CLAUDE.md's "Code Quality" section) apply:
   cognitive complexity ≤ 15, ≤ 13 parameters, one logical assertion per
   `assert`, only the throwing call inside `pytest.raises`, no duplicated
   string literals, `async` only where `await` is used.
8. **Commits: conventional-commit style, no `Co-Authored-By` / AI-attribution
   trailer.** Say what changed, why, and how it was verified.

## Verification commands

```sh
# Full gate — run all four before declaring a session done.
docker compose run --rm --build test pytest -v
docker compose run --rm --build test ruff check .
docker compose run --rm --build test ruff format --check .
docker compose run --rm --build test mypy app

# Apply ruff fixes to HOST files (the plain command above edits the baked
# image copy and changes nothing on disk):
docker compose run --rm -v "$PWD":/src -w /src test bash -c "ruff check --fix .; ruff format ."

# Confirm the image actually contains your code:
docker compose run --rm --no-deps test grep -c 'def test_' tests/test_image_vips.py
# ...and compare with the host count.
```

The `test` service declares `profiles: ["test"]`, but `docker compose run`
implicitly enables a service's own profile — `--profile test` is harmless and
unnecessary. `pytest`/`ruff`/`mypy` are on `PATH` inside the image; there is no
`uv run` wrapper.

The suite includes `pg_integration`-marked tests that need the real `db`
service (started automatically by `run`). Deselect with `-m "not pg_integration"`
only when deliberately running without Postgres.

## Design decisions already made

Do not revisit these inside a session; they were settled during planning.

| Decision | Rationale |
|---|---|
| **`IMAGE_RENDITION_WIDTHS` env var: rejected.** | `RENDITION_SPECS` feeds `_RENDITION_ALIASES` at import, and the *serving* path (`app/routers/playback.py:48`) 400s any name not in that map, while `_filtered_renditions` (`app/services/metadata/types.py:124`) and `_filter_renditions` (`app/routers/utils.py:273`) resolve stored renditions through `get_rendition_spec`. Narrowing the list after uploads exist turns already-materialized objects into hard 400s while their keys still sit in the row's jsonb. Making it safe requires the serving path to resolve from the record's stored `renditions` dict instead of the global table — real work for a knob nobody asked for. Revisit only with that refactor, and read `docs/responsive-renditions.md` first. |
| **`IMAGE_SMART_CROP` env var: rejected.** | Two knobs for one decision. The per-spec `crop_mode` field in session 3 is the single source of truth. |
| **`image.colourspace("srgb")` alone: rejected as the wide-gamut fix.** | It is a no-op for the target case. See session 1 for what actually works and why. |
| **`validate_and_strip_image` returns a dataclass, not a growing tuple.** | Already a 5-tuple; this plan would take it to 8. Session 1 converts it. |
| **`dominant_color` / `blur_data_url` are exposed regardless of visibility.** | They are self-contained data — no storage key, no URL, nothing cross-tenant. A private record needs a placeholder just as much as a public one. This deliberately breaks the existing "extras are public-only" symmetry in `to_public()`; session 2 documents it. |
| **Animated input gets its own code branch, not a flag on the existing path.** | The current pixel-count check, max-dim resize, and rendition loop all misread a multi-frame strip. Session 4 explains each. |

## Ground truth for a fresh session

Key files and their current sizes, so a session knows what it is walking into:

| File | Lines | Role |
|---|---|---|
| `app/services/image_vips.py` | 120 | The whole image pipeline. Sessions 1–4 all land here. |
| `app/services/renditions.py` | 176 | `RenditionSpec`, `RENDITION_SPECS`, aliases, key derivation. |
| `app/routers/upload.py` | 927 | `_process_single_image` (the caller), `_ProcessedImageData`, `_store_and_record_image`. |
| `app/routers/utils.py` | 353 | `_image_response`, `_filter_renditions`. |
| `app/services/metadata/types.py` | 167 | `UploadRecord` + `to_public()`. |
| `app/services/metadata/postgres.py` | 499 | `_COLUMNS`, `_JOIN_COLUMNS`, `_row_to_record`, `create()`. |
| `app/services/metadata/store.py` | 196 | The `MetadataStore` ABC. |
| `tests/fakes.py` | — | `InMemoryMetadataStore` (there is **no** `app/services/metadata/memory.py`). |
| `app/tasks.py:350` | — | The worker's poster path — the **second** caller of `validate_and_strip_image`. |
| `app/schemas.py` | 386 | `_BaseImageUploadResponse` (line 61), `FileRecord` (line 180). |
