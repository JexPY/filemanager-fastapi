# filemanager-fastapi

## What this is

A distributed media-processing microservice: synchronous image/QR handling in
a FastAPI process, asynchronous video compression in a separate TaskIQ worker
process. It is **not** a general file host — there's no listing, no
metadata/audit store, and no delete endpoint; every object is fire-and-forget
once uploaded (see Sharp edges below).

## Architecture, in one glance

Two process types, one codebase, selected by which CMD runs:

- **`api`** — `uvicorn app.main:app`. Handles all HTTP requests.
- **`worker`** — `taskiq worker app.broker:broker app.tasks`. Runs FFmpeg.

```
client --(bearer auth)--> api ---+--> image: pyvips validate/strip -> storage -> imgproxy signs a fetch URL
                                  +--> qrcode: segno -> pyvips -> PNG response
                                  +--> video: storage (raw) --key only--> Redis --> worker
                                                                                      |
                                                                                      v
                                                                        ffmpeg compress -> storage
                                                                        (raw deleted after, success or failure)
```

Both processes import the same `app/` package and share a Redis instance, a
storage backend (volume or bucket), and a Postgres metadata store — that's the
entire coupling between them. `api` writes an `uploads` row on every upload and
serves the list/get/delete routes; the `worker` updates a video's row when
compression finishes. Everything else (routes, health checks) only exists in
`api`.

## Non-obvious invariants

Things that will bite you if you don't know them:

- **Only the storage *key* travels through Redis for video, never bytes.**
  `POST /upload/video` stages the raw upload in storage first, then enqueues
  `compress_video_task(raw_storage_key, original_filename, upload_id)` — three
  strings (the `upload_id` lets the worker update the right `uploads` row). The
  worker downloads the bytes itself. Don't "optimize" this by passing bytes
  through the task payload.
- **Every upload is recorded, and the video record's lifecycle is a state
  machine.** `POST /upload/image` writes one `uploads` row (`status='ready'`).
  `POST /upload/video` writes the row `status='processing'` **before** it
  enqueues the task — that ordering is load-bearing: the worker marks the row
  ready *by id*, so the row must exist first (never enqueue-then-create). On
  success the worker's `mark_ready` swaps `storage_key` from the raw key to the
  compressed key and sets `status='ready'`; on any failure it best-effort
  `mark_failed`s and re-raises (so `GET /tasks/{id}` still reports failed too).
  If `mark_ready` finds no row (the owner `DELETE`d the upload mid-compression)
  the worker discards the compressed object it just wrote instead of orphaning
  it. QR codes are returned inline and never recorded.
- **Image uploads are idempotent per owner, keyed on the input's sha256.**
  `POST /upload/image` hashes the *input* bytes (in a threadpool — it's CPU
  work) and, on a `(owner, content_hash)` hit against a `ready` row, returns the
  existing record without re-decoding, re-encoding, or re-storing. The hash is
  of the input, not the stored WebP; dedup is owner-scoped so hashes never
  collide or leak across tenants. The record stores `width`/`height` precisely
  so the deduplicated response stays fully shaped (dimensions included) without
  touching the object.
- **Video uploads are also idempotent per owner, keyed on the raw input's
  sha256** — but the match set differs from images. `POST /upload/video` hashes
  the raw bytes and looks for this owner's latest *video* row that is `ready`
  **or** still `processing` (`find_active_video_by_hash`): a `ready` hit returns
  `200 {status:"duplicate"}` (already available), a `processing` hit returns
  `202 {status:"duplicate", record_status:"processing"}` (attach to the
  in-flight job — don't compress the same bytes twice). `failed` rows are
  excluded so a bad input can be retried. Dedup is best-effort like images: two
  *simultaneous* identical uploads can still both slip through (no unique
  constraint), which is acceptable. The hash keys the *input* (the compressed
  output is nondeterministic), stored in the same `content_hash` column images
  use. The `uploads` schema is owned by **Alembic** (see `migrations/`); further
  changes are new revisions, never in-place DDL edits.
- **The storage singleton is per-process, not shared state.** `app/services/
  storage.py`'s `_storage` module-level variable is built lazily and
  independently in each OS process (api and worker each get their own). It's
  closed via `close_storage()` — wired into `api`'s FastAPI lifespan
  (`app/main.py`) and into the worker's `TaskiqEvents.WORKER_SHUTDOWN` event
  (`app/broker.py`), so both processes release pooled clients cleanly.
- **The metadata store mirrors that singleton pattern exactly.** `app/services/
  metadata.py`'s `_store` (an asyncpg-pool-backed `PostgresMetadataStore`) is
  built lazily per process and closed via `close_metadata_store()`, wired into
  the same two lifecycle hooks as storage. The `uploads` **schema is owned by
  Alembic**, applied by a dedicated one-shot `migrate` compose service
  (`alembic upgrade head`) that api and worker wait on
  (`service_completed_successfully`) — the store no longer self-creates it, it
  just opens the pool. `api`'s lifespan still connects eagerly at startup so a
  bad `DATABASE_URL` fails fast. **Postgres, not SQLite, on purpose** — two OS
  processes write this store, and SQLite over a shared volume reintroduces the
  cross-process locking fragility this whole per-process-singleton design
  avoids. Unit/route tests never touch a live DB: `tests/fakes.py`'s
  `InMemoryMetadataStore` is seeded into `_store` by the `fake_metadata` fixture,
  the same way `fake_storage` seeds storage. Real-Postgres coverage lives in the
  `pg_integration`-marked `tests/test_metadata_store_pg.py`.
- **The worker imports the entire FastAPI app at startup**, not just
  `app.tasks`. `app/broker.py` calls `taskiq_fastapi.init(broker,
  "app.main:app")`, which makes the worker resolve and import `app.main` (for
  FastAPI-style dependency injection in tasks, even though no task uses it
  today). This transitively imports `app.routers.files` → `image_vips` →
  `pyvips`, which is why `Dockerfile.worker` installs libvips even though
  `tasks.py` never calls it directly. Forgetting this is exactly how the
  worker ended up unable to boot at all during this pass (see git history) —
  if you add a new import to `app/routers/*.py` that needs a new system
  library, the worker needs it too.
- **`taskiq worker` is run *without* `--fs-discover`.** That flag recursively
  scans the working directory for task modules, and inside the container that
  includes `.venv/site-packages` — it previously found and crashed on
  `s3transfer.tasks`, an unrelated module in a dependency. `app.tasks` is
  already passed explicitly; don't re-add `--fs-discover`.
- **imgproxy fetches sources *by URL/URI*, so whatever storage returns has
  to actually be reachable from the imgproxy container.** For `s3`/`gcp`,
  that's a presigned or public object URL. For `local`, there's no HTTP path
  at all — imgproxy instead reads via its `local://` source scheme from the
  `media_data` volume, mounted read-only into the `imgproxy` service with
  `IMGPROXY_LOCAL_FILESYSTEM_ROOT=/data/media` set. `app/services/imgproxy.py`'s
  `build_source_url()` is what switches between the two; it keys off
  `settings.STORAGE_BACKEND`, not off whether presigning happened to be used.
- **`IMGPROXY_KEY`/`IMGPROXY_SALT` must be valid, even-length hex, and must
  match the `imgproxy` container's own env vars exactly** — `config.py`
  validates this at startup (fails fast with a clear message) specifically
  because a mismatch here used to only surface as a confusing 403 from
  imgproxy on the first real request, after the file was already uploaded.
- **Three independent `*_PUBLIC_BASE_URL` settings** — `LOCAL_PUBLIC_BASE_URL`,
  `S3_PUBLIC_BASE_URL`, `GCS_PUBLIC_BASE_URL`. They used to be one shared
  field; switching `STORAGE_BACKEND` without touching env vars will not
  silently reuse the wrong URL anymore, because there's no longer a shared
  field to reuse.
- **`pyvips` `strip=True` drops *all* metadata** (EXIF/GPS, ICC profile, XMP)
  on every image re-encode. This is intentional and not configurable per
  request.
- **Two different time limits in the video pipeline, not one:**
  `VIDEO_MAX_DURATION_SECONDS` (default 60) is the ffmpeg `-t` cap on *output
  duration*. An input longer than it **is** truncated, but no longer silently:
  the worker `ffprobe`s the input up front and reports `duration_seconds` +
  `truncated` in the task result **and** on the `uploads` row (`mark_ready`
  persists both). `FFMPEG_TIMEOUT_SECONDS` (default 120) is a wall-clock
  `asyncio.wait_for` around `process.communicate()` that kills a wedged ffmpeg
  process. Changing one does not affect the other. The ffprobe step is
  best-effort — if it can't read a duration, `truncated` is reported `false`
  (unknown) rather than failing the compression.
- **A bogus/typo'd `task_id` returns 404, not "pending" forever** — this only
  works because `POST /upload/video` sets a short-lived Redis marker
  (`app/services/task_status.py`) when it enqueues the task.
  `is_result_ready()` alone can't tell "still running" apart from "never
  existed."

## Commands (all verified this session, against the real stack)

```sh
# First-time setup
cp .env-example .env
openssl rand -hex 32   # -> IMGPROXY_KEY in .env
openssl rand -hex 32   # -> IMGPROXY_SALT in .env (must differ from the key)
# also set FILE_MANAGER_BEARER_TOKENS in .env

# Run the full stack (the one-shot `migrate` service applies Alembic migrations
# before api/worker start -- they wait on it via service_completed_successfully)
docker compose up --build

# Run a single worker only
docker compose up worker

# Database migrations (Alembic owns the `uploads` schema; migrations/ holds the
# revisions). The `migrate` service runs `alembic upgrade head` automatically on
# `up`, but you can also drive it directly:
docker compose run --rm migrate                         # upgrade head (default cmd)
docker compose run --rm migrate alembic downgrade -1    # roll back one revision
docker compose run --rm migrate alembic current         # show applied revision
# Author a new revision after building the migrate image (hand-write the SQL --
# there is no ORM model layer, so autogenerate is not used):
docker compose run --rm migrate alembic revision -m "add whatever column"

# Test suite / lint / format / types (all run inside Docker -- there is no
# supported local Python environment for this project; see below).
# NOTE the --build: the test service bakes the source in at image-build time
# (Dockerfile.test's `COPY . .`), it is NOT bind-mounted, so `run` without
# --build silently re-runs your OLD code. Always rebuild after editing.
docker compose run --rm --build test pytest -v
docker compose run --rm --build test ruff check .
docker compose run --rm --build test ruff format .
docker compose run --rm --build test mypy app
# The test service depends on redis + db, so `run` starts both; the
# pg_integration tests exercise the real Postgres `db`. Deselect them with
# `-m "not pg_integration"` if running without it.

# Local S3-compatible backend for testing (MinIO)
docker compose --profile s3-dev up -d minio minio-init
# then in .env: STORAGE_BACKEND=s3, S3_ENDPOINT_URL=http://minio:9000,
# S3_BUCKET=filemanager-test, AWS_ACCESS_KEY_ID/SECRET=minioadmin
```

**Do not set up a local host Python venv or brew-install libvips/ffmpeg for
this project.** Everything — running the app, tests, lint, type-checking —
runs inside Docker on purpose, matching the production server exactly. If
your editor shows unresolved-import warnings on `fastapi`/`pyvips`/etc, that's
expected noise, not a real problem.

## Configuration

`app/config.py`'s `Settings()` validates at import time and **fails fast** on:
missing `S3_BUCKET`/`GCS_BUCKET` for the matching `STORAGE_BACKEND`, an empty
`FILE_MANAGER_BEARER_TOKENS`, and a missing/invalid-hex `IMGPROXY_KEY` or
`IMGPROXY_SALT`. See `.env-example` for the full variable list with comments;
`readme.md` has the same as a table.

## Conventions

- **Async-only in request/worker paths.** No blocking calls on the event
  loop. CPU-bound work (pyvips decode/encode, QR SVG rasterization) is
  offloaded via `asyncio.to_thread` in the router layer, not inside the
  service functions themselves — `app/services/image_vips.py` and
  `qr_generator.py` are plain sync functions by design; the async boundary is
  the caller's job.
- **Errors surfaced to clients are always sanitized.** `StorageError` →
  generic 502; `ImageValidationError` → generic 400; a failed video task →
  generic "Video processing failed". The real exception is always logged
  server-side (`logger.warning`/`.error`) first. Never add a route that does
  `detail=str(e)` on an arbitrary exception.
- **Storage keys**: `images/<uuid>.webp`, `raw/videos/<uuid>.<ext>`,
  `videos/<uuid>_compressed.mp4`. Video extensions are sanitized
  (`_sanitize_extension` in `files.py`) to `[a-z0-9]`, ≤8 chars, before
  reaching the key — never interpolate a client-supplied filename into a key
  unsanitized.
- **Test layout**: `tests/conftest.py` has the shared fixtures
  (`fake_storage` pre-seeds the module-level storage singleton;
  `fake_result_backend`/`fake_enqueue` stand in for TaskIQ so tests never
  need a live broker round-trip; `client` wraps `httpx.AsyncClient` over
  `ASGITransport`, which does **not** run the app's lifespan — tests that
  need real lifespan behavior use `fastapi.testclient.TestClient` directly,
  as a one-off, the way the `populate_dependency_context` bug was caught).
  `tests/fakes.py` has `InMemoryStorageBackend`. Real ffmpeg/pyvips fixtures
  live in `tests/fixtures/`, generated once via the Docker test image (see
  git history for the generation script) rather than hand-crafted bytes.
- **Commit messages**: conventional-commit style, matching the existing
  history — each says what changed, why, and how it was verified. **Do not add
  a `Co-Authored-By:` / AI-attribution trailer** to commits or PR bodies.

## What the metadata pass added (was backlog, now done)

The "raise-the-ceiling" pass built the system-of-record cluster on top of the
hardening baseline: a Postgres `uploads` table, per-token identity, owner-scoped
`GET /files` + `GET /files/{id}` + `DELETE /files/{id}`, and idempotent image
upload. Every piece was verified end to end against a clean stack, not just unit
tests. The invariants above (metadata singleton, video record state machine,
idempotency) are the load-bearing details.

## Known sharp edges & backlog

Honest list, not hidden anywhere else:

- **No webhooks/callbacks** — video-compression completion is poll-only
  (`GET /tasks/{id}`). Now *easy* to add: the `uploads` row already carries
  `owner` + `task_id`, so a completion notification has something to fire at.
- **Scopes are coarse** — per-token *identity* exists and scopes uploads/list/
  delete, but any valid token can still poll any `task_id` (`GET /tasks/{id}`
  isn't owner-scoped — the task result predates the record). No roles/scopes
  beyond "owns its own uploads".
- **GCS backend is unit-tested only** (mocked client) — no live GCP
  project/credentials in this environment. `local` and `s3` (against real
  MinIO) are verified end to end; the Postgres metadata store is verified
  against real PostgreSQL 17.
- **No streaming I/O** — uploads are fully buffered in memory (bounded by
  `MAX_IMAGE_UPLOAD_BYTES`/`MAX_VIDEO_UPLOAD_BYTES`, but still buffered, not
  streamed to storage). The single biggest scalability lever if this needs
  to handle much larger files or higher concurrency.
- **No presigned direct-to-storage uploads** — every byte proxies through
  `api`. Would remove the API from the upload path entirely for large media.
- **No rate limiting / concurrency caps** beyond the upload size limits.
- **No retries/circuit breakers** around network hops (Redis, S3/GCS, imgproxy,
  Postgres) — behavior is fail-closed and visible (e.g. `StorageError`/
  `MetadataError` → generic 502), which is correct; this would be a reliability
  layer on top.
- **Production reverse-proxy/TLS is not shipped** — the old `nginx` service
  was removed (it had no config and did nothing); pick a reverse proxy /
  TLS terminator appropriate to your deployment target.

None of the above are silently broken — each is a deliberate scope boundary.
Start a fresh planning pass against the actual code before picking one up,
rather than assuming this list is still accurate by the time you read it.
