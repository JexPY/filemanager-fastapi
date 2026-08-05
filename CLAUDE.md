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
  touching the object. Video is *not* deduplicated (async, nondeterministic
  output) — backlog. The `uploads` schema is a single greenfield
  `CREATE TABLE IF NOT EXISTS` (columns added across this session's commits, all
  pre-release); once it's deployed, further changes need real migrations
  (Alembic) rather than editing the DDL in place.
- **The storage singleton is per-process, not shared state.** `app/services/
  storage.py`'s `_storage` module-level variable is built lazily and
  independently in each OS process (api and worker each get their own). It's
  closed via `close_storage()` — wired into `api`'s FastAPI lifespan
  (`app/main.py`) and into the worker's `TaskiqEvents.WORKER_SHUTDOWN` event
  (`app/broker.py`), so both processes release pooled clients cleanly.
- **The metadata store mirrors that singleton pattern exactly.** `app/services/
  metadata.py`'s `_store` (an asyncpg-pool-backed `PostgresMetadataStore`) is
  built lazily per process and closed via `close_metadata_store()`, wired into
  the same two lifecycle hooks as storage. The `uploads` **table is created on
  first pool use** (`CREATE TABLE IF NOT EXISTS`, safe from whichever process
  gets there first); `api`'s lifespan also connects eagerly at startup so a bad
  `DATABASE_URL` fails fast. **Postgres, not SQLite, on purpose** — two OS
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
- **Two different 60-ish-second things in the video pipeline, not one:**
  `-t 60` in the ffmpeg command (`tasks.py`) caps *output duration*, silently
  truncating any input longer than 60s — the caller is never told this
  happened (backlog item). `FFMPEG_TIMEOUT_SECONDS` (default 120) is a
  wall-clock `asyncio.wait_for` around `process.communicate()` that kills a
  wedged ffmpeg process. Changing one does not affect the other.
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

# Run the full stack
docker compose up --build

# Run a single worker only
docker compose up worker

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

## Known sharp edges & backlog

Honest list, not hidden anywhere else:

- **The `-t 60` output-duration cap silently truncates** anything longer with
  no signal to the caller. Fixing this properly means either surfacing
  truncation in the task result or making the cap configurable — a
  behavior/response-shape change, deliberately out of scope for this pass.
- **GCS backend is unit-tested only** (mocked client) — no live GCP
  project/credentials in this environment. `local` and `s3` (against real
  MinIO) are both verified end to end.
- **No streaming I/O** — uploads are fully buffered in memory (bounded by
  `MAX_IMAGE_UPLOAD_BYTES`/`MAX_VIDEO_UPLOAD_BYTES`, but still buffered, not
  streamed to storage). The single biggest scalability lever if this needs
  to handle much larger files or higher concurrency.
- **No presigned direct-to-storage uploads** — every byte proxies through
  `api`. Would remove the API from the upload path entirely for large media.
- **No metadata/system-of-record** — uploads aren't listable, auditable, or
  deletable by the API's own design; `delete_file()` is only ever called
  internally (raw-video cleanup after compression), never from a route.
- **No webhooks** — video-compression completion is poll-only
  (`GET /tasks/{id}`).
- **No per-token identity/scopes** — any valid bearer token can call any
  route, including polling any task_id.
- **No rate limiting / concurrency caps** beyond the upload size limits.
- **Production reverse-proxy/TLS is not shipped** — the old `nginx` service
  was removed (it had no config and did nothing); pick a reverse proxy /
  TLS terminator appropriate to your deployment target.

None of the above are silently broken — each is a deliberate scope boundary
of the hardening pass that produced the current state of this file. Start a
fresh planning pass against the actual code before picking one up, rather
than assuming this list is still accurate by the time you read it.
