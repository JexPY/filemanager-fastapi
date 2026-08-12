# filemanager-fastapi

## What this is

A distributed media-processing microservice: synchronous image/QR handling in
a FastAPI process, asynchronous video compression in a separate TaskIQ worker
process. It is **not** a general file host — there's no listing, no
metadata/audit store, and no delete endpoint; every object is fire-and-forget
once uploaded (see Sharp edges below).

## Architecture, in one glance

Two process types, one codebase, selected by which CMD runs (plus **nginx** as
the entry proxy — see below):

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
serves the list/get/delete/download/share routes; the `worker` updates a video's
row when compression finishes. Everything else (routes, health checks) only
exists in `api`. **nginx** is the entry reverse proxy (`:9000`; api is behind it
on `:9001`): it proxies every route to `api` and, for `local`-backend video
playback, serves the bytes itself from `media_data` via the app's
`X-Accel-Redirect` (sendfile + Range) — the app stays out of the byte path.

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
- **Video completion can push a signed webhook, and the SSRF guard lives in the
  api, not the worker.** A client may pass `callback_url` on `POST /upload/video`
  (stored in the `uploads.callback_url` column). The **api** admits it at upload
  time (`app/services/webhooks.py::validate_callback_url`): https-only unless
  `WEBHOOK_ALLOW_INSECURE_HTTP`, host must be on the explicit
  `WEBHOOK_ALLOWED_HOSTS` allow-list, and (unless `WEBHOOK_ALLOW_PRIVATE_IPS`)
  must not resolve to a private/loopback/link-local address — a bad URL is a
  `400` before anything is staged. Delivery then runs on **its own TaskIQ task**
  (`deliver_webhook_task`), not inline in the compression task: on a terminal
  state the compression task best-effort *enqueues* `(upload_id, event)` (two
  strings — key-not-bytes again) and the delivery task re-loads the record,
  POSTs its `to_public()` HMAC-SHA256-signed over `timestamp.body`
  (`X-Webhook-Signature`), with `X-Webhook-Id` = the upload id as a stable
  idempotency key across retries. This decoupling means a slow/dead receiver
  ties up a *delivery* slot, never the compression slot. `deliver_webhook` is
  **best-effort and never raises**; it returns a `WebhookDeliveryResult` that the
  delivery task persists on the row (`webhook_status` `pending`/`delivered`/
  `failed`, `webhook_attempts`, `webhook_last_error`) — so an exhausted delivery
  is a **durable dead-letter record**, not just a log line, replayable via
  `POST /files/{id}/redeliver` (owner-scoped; re-enqueues the current terminal
  event with the same `X-Webhook-Id`). **Webhooks are off unless BOTH
  `WEBHOOK_SIGNING_SECRET` and `WEBHOOK_ALLOWED_HOSTS` are set**
  (`settings.webhooks_enabled`); with them unset, any `callback_url` is a 400.
  The allow-list is the authoritative egress control; the IP check is defence in
  depth (a DNS-rebind of an allow-listed host is out of scope).
- **Video posters are on-request and are their own image record.**
  `POST /files/{id}/poster` (owner-scoped) admits a *ready* video (400 non-video,
  409 not-ready) and enqueues `generate_poster_task`; the client polls
  `GET /files/{id}` until `poster_upload_id` is set. The worker downloads the
  compressed video, `ffmpeg -ss` extracts one frame (default ~10% in via an
  ffprobe of the clip, or an explicit `at_seconds`), runs it through the **exact**
  image `validate_and_strip_image` path (pyvips → WebP), stores it under
  `posters/<uuid>.webp`, and creates a normal `kind=image` `uploads` row; then
  `set_poster` links the video → poster. If the video was DELETEd mid-generation
  `set_poster` returns None and the task discards the poster it just wrote
  (symmetric with `mark_ready`). The happy path is idempotent (an existing
  poster is returned, not regenerated); two *simultaneous* requests can each
  generate one (best-effort, like the upload-dedup races) and the loser is a
  harmless standalone image row. `DELETE /files/{video}` cascades to the poster
  (object + row, best-effort).
- **Video playback is one stable URL; visibility picks the auth *and* the URL
  form; the backend picks the byte path; a signed URL is a hidden, disposable
  detail.** `GET /files/{id}/download` is the permanent, backend-agnostic URL
  clients embed (video-only → 400 otherwise). It does **not** use the
  `verify_token` dependency — a `public` video is fetchable with no token, so
  auth is resolved inline from an *optional* bearer (`_resolve_owner_optional`).
  `visibility` (`private`|`public`, on the `uploads` row, owner-set via
  `PATCH /files/{id}`) decides: **private** → owner bearer required, non-owner or
  missing token → **404 not 403** (existence never leaks, like the rest of the
  owner-scoping); **public** → tokenless, and if a `*_PUBLIC_BASE_URL` is set for
  the backend a 302 to the stable `public_url(key)` (never a per-request presign
  — a unique signature would defeat CDN caching), else it falls back to the same
  signed-URL delivery as private (still tokenless). The per-backend byte path
  lives in one resolver (`resolve_playback`) **keyed on `settings.STORAGE_BACKEND`,
  mirroring `imgproxy.build_source_url`**: `local` → nginx `X-Accel-Redirect`
  (dev fallback `LOCAL_MEDIA_SERVE_MODE=direct` → Starlette `FileResponse`),
  `s3` → 302 to a freshly-minted presigned GET (`VIDEO_PLAYBACK_URL_TTL_SECONDS`),
  `gcp` → 302 to a GCS V4 signed URL. Range works in every path and the app is
  **out of the byte path** (nginx or the object store moves the bytes). **HLS is
  out of scope** (progressive + Range only).
- **The nginx `internal;` location is the load-bearing security property for
  local playback.** `local`+`xaccel` returns an empty body with
  `X-Accel-Redirect: /internal-media/<storage_key>`; nginx serves the file from
  the read-only `media_data` volume with `sendfile` + native Range. The
  `/internal-media/` location in `nginx/nginx.conf` **MUST stay `internal;`** —
  it can only be entered via an upstream X-Accel-Redirect, never a direct client
  request, so the download route's visibility/ownership auth is never bypassed.
  The storage key is UUID-based and already sanitized, but the resolver still
  treats it as untrusted when interpolating (`_assert_safe_media_key`: no `..`,
  no leading `/`). **nginx is now the entry proxy on :9000** (api moved to :9001,
  debug-only); only nginx interprets X-Accel, so local video playback does not
  work hitting the api directly.
- **The share token is a secret capability — unlisted, revocable, and never in
  `to_public()`.** `POST /files/{id}/share` mints/rotates a
  `secrets.token_urlsafe(32)` (owner-scoped) and is the **only** response that
  returns it (+ the shareable URL); it is deliberately excluded from
  `to_public()` (so it never leaks via listings, `GET /files/{id}`, webhooks, or
  their logs). `GET /share/{token}` resolves it via the **unscoped**
  `get_by_share_token` (the token *is* the grant, like `get_by_id`) and serves
  the video regardless of `visibility`, tokenless; unknown/revoked → 404.
  `DELETE /files/{id}/share` clears it. The `share_token` column has a **UNIQUE
  index** (Postgres NULLs are distinct, so unlimited rows may have none while a
  minted token is guaranteed unique) — see migration `0006`.
- **Two credential shapes, one owner: static master tokens *and* capability
  JWTs.** `app/routers/auth.py::resolve_principal` is the single dual-auth path
  and is pure (trivially mockable). A bearer is first matched, constant-time,
  against `FILE_MANAGER_BEARER_TOKENS` (the legacy path → a `Principal` with
  `scopes=None`, i.e. **unrestricted**); an unmatched one is then decoded as an
  HS256 capability JWT signed with `JWT_SECRET_KEY` (`sub`=owner, `exp`
  enforced, `scopes` must include at least one of `upload:image`/`upload:video`
  or it isn't a principal). Either way the resolved *owner* flows through
  `verify_token` unchanged, so every existing owner-scoped route is unaffected.
  The token may ride the `Authorization: Bearer` header **or** a `?token=` query
  param — the presigned-URL fallback for header-less clients (`<form>` POSTs, a
  `<video src>`); identical validation for both. **Scopes gate only the upload
  verbs**: `/upload/image` and `/upload/video` depend on `require_scopes(...)`,
  so a JWT must carry the matching scope (a 403 otherwise) while a static token
  bypasses it (`scopes=None`). `POST /upload/presign` is **static-token only**
  (via `verify_static_token`, so a leaked JWT can never mint *more* JWTs) and
  returns `<PUBLIC_BASE_URL>/upload/<kind>?token=<jwt>` for a frontend to upload
  straight to this service, bytes never touching the main backend. JWT auth is
  **off unless `JWT_SECRET_KEY` is set** — blank means only static tokens work
  (backward compatible) and `/upload/presign` is a `503`. **Do not `str(e)` a
  JWT error to the client** — a bad/expired/mis-signed token is a flat `401`, no
  oracle for *why*.
- **GCS can now sign private playback URLs.** `GCSStorage.presigned_get_url`
  does V4 signing **locally** from the service-account key already loaded
  (gcloud-aio-storage's `Blob.get_signed_url`) — no CDN, no extra Google product,
  no network round-trip for signing (clamped to GCS's 7-day cap). Before this,
  GCS inherited the base `presigned_get_url` returning `None`, so private GCS
  objects had no fetchable URL at all. Verified by mock only (no live GCP here).
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
  (`app/broker.py`), so both processes release pooled clients cleanly. New
  streaming interfaces include `StorageBackend.local_path` (for zero-copy
  local FFmpeg access) and `StorageBackend.upload_from_path` (for streaming).
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
  `pyvips`, which is one reason `Dockerfile.worker` installs libvips. The other,
  now, is direct: `generate_poster_task` in `tasks.py` calls
  `image_vips.validate_and_strip_image` itself (frame → WebP). Either way the
  worker needs libvips; forgetting this is exactly how the worker ended up
  unable to boot at all during an earlier pass (see git history) — if you add a
  new import to `app/routers/*.py` **or `app/tasks.py`** that needs a new system
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
- **`GET /tasks/{id}` is owner-scoped and 404s a bogus/typo'd id, via the
  uploads record — not a Redis marker.** The route resolves the task through
  `get_by_task_id(task_id, owner)`: no matching row (unknown id *or* another
  owner's task) → 404, so existence never leaks across tenants. The row is also
  the durable proof the task was issued, which distinguishes "still running"
  (row exists, result not ready → `pending`) from "never existed" (no row →
  404) — `is_result_ready()` alone cannot. This replaced the old short-lived
  Redis issuance marker (`app/services/task_status.py`, now removed); the record
  is created before enqueue, so it's always present and it's durable (no TTL).
- **`docker-compose.override.yml` (checked in, dev-only) silently wins over
  `Dockerfile.api`'s `CMD`.** Compose auto-merges it into any bare
  `docker compose ...` run from this directory, and it unconditionally
  overrides the `api` service's `command` to a single-process
  `uvicorn --reload` (plus a `./app:/app/app` live-reload bind mount) — it
  does not defer to whatever `Dockerfile.api`'s `CMD` says. A prod-facing
  `CMD` change (e.g. adding `--workers N`) is real in the built image
  (`docker inspect <image> --format '{{json .Config.Cmd}}'` shows it) but
  **invisible under a default local `docker compose up`**, so don't trust
  `docker compose config`/`build` alone to confirm it took effect — check the
  actual running container (`docker compose exec api ps aux`) or run
  `docker compose -f docker-compose.yml up` to exclude the override. The
  override applies the same override pattern to `worker`'s command/volumes.

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
`FILE_MANAGER_BEARER_TOKENS`, a missing/invalid-hex `IMGPROXY_KEY` or
`IMGPROXY_SALT`, and an invalid `LOCAL_MEDIA_SERVE_MODE` (must be
`xaccel`|`direct`). See `.env-example` for the full variable list with comments;
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
  `videos/<uuid>_compressed.mp4`, `posters/<uuid>.webp`. Video extensions are sanitized
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

## What the visibility/playback pass added (was backlog, now done)

The "stream-and-share" pass gave video a real URL story: one stable,
backend-agnostic `GET /files/{id}/download` (+ `GET /share/{token}`) with working
HTTP Range on **all three** backends (local via nginx X-Accel, s3 via presigned
302, gcp via a newly-added V4 signed URL), an owner-controlled visibility model
(`private`/`public` + an unlisted, revocable share token; migration `0006`), and
nginx re-introduced as the entry proxy. The load-bearing details are the four
invariants above (the download-resolution switch, the `internal;` security
property, visibility = auth-form + byte-path split, share-token secrecy). Parked
by design: HLS/ABR, streaming *upload*, and CDN signed-cookies for
private-at-scale playback (see backlog).

## Known sharp edges & backlog

Honest list, not hidden anywhere else:

- **Webhook delivery is decoupled + dead-lettered, but replay is manual and the
  delivery task still blocks on its own retry budget.** Delivery moved to its own
  `deliver_webhook_task` (a slow receiver no longer ties up the *compression*
  slot) and an exhausted delivery is now a durable dead-letter record on the row
  (`webhook_status='failed'`, replayable via `POST /files/{id}/redeliver`).
  Remaining gaps: the delivery task still `await`s the full retry budget inline
  (so a dead receiver holds a *delivery* worker slot for that long — no separate
  low-priority delivery queue), and redelivery is owner-triggered, not automatic
  (no scheduled re-attempt of dead-lettered rows). A dedicated deliveries table
  (vs. row-state) would also be needed if a single upload ever needed multiple
  callbacks/events.
- **Scopes are coarse** — per-token *identity* exists and scopes uploads,
  listing, deletion, **and `GET /tasks/{id}`** to the owner. Capability JWTs add
  `upload:image`/`upload:video` scopes, but **those gate only the two upload
  verbs**; a JWT authenticates as its `sub` for every *other* owner-scoped route
  the same as a static token would (blast radius is still just that owner's own
  resources). There are still no read-only vs. read-write or admin roles, and no
  scope taxonomy beyond the two upload verbs.
- **GCS backend is unit-tested only** (mocked client) — no live GCP
  project/credentials in this environment. This now includes the V4
  signed-playback-URL path (`GCSStorage.presigned_get_url`), asserted by mock.
  The GCS delete method is also verified by mock to safely handle 404s (idempotent).
  `local` and `s3` (against real MinIO) are verified end to end — including
  **video playback with Range** (local via nginx X-Accel → 206; s3 via
  302→presigned→Range → 206) and the full visibility/share matrix; the Postgres
  metadata store is verified against real PostgreSQL 17.
- **Private-at-scale playback + resume-safety is parked.** The `private` path is
  a 302 to a signed URL; the player caches the *resolved* signed URL, so a seek
  after `VIDEO_PLAYBACK_URL_TTL_SECONDS` can hit an expired signature (sized-TTL
  is the mitigation). The bulletproof upgrade — CDN **signed cookies** / a fully
  CDN-fronted private path where the player only ever talks to the stable URL —
  is a later pass, warranted only when high-read private media or a hard
  never-break-on-resume promise appears. CDN wiring itself is a deploy concern:
  `*_PUBLIC_BASE_URL` = the CDN domain, zero code change.
- **No streaming I/O for images** — image uploads are fully buffered in memory
  by design. (Video upload and worker processing now stream to/from disk safely, 
  offering bounded O(1) memory usage across all backends).
- **No presigned direct-to-storage uploads** — every byte proxies through
  `api`. Would remove the API from the upload path entirely for large media.
- **No rate limiting / concurrency caps** beyond the upload size limits.
- **No retries/circuit breakers** around network hops (Redis, S3/GCS, imgproxy,
  Postgres) — behavior is fail-closed and visible (e.g. `StorageError`/
  `MetadataError` → generic 502), which is correct; this would be a reliability
  layer on top.
- **TLS termination is not shipped** — nginx is back as a real entry proxy
  (`nginx/nginx.conf`: proxy_pass + the `internal;` X-Accel location for local
  playback), but it terminates plain HTTP only. Add a TLS terminator
  (nginx `listen 443 ssl`, or an upstream LB/ingress) for production.

None of the above are silently broken — each is a deliberate scope boundary.
Start a fresh planning pass against the actual code before picking one up,
rather than assuming this list is still accurate by the time you read it.

### Additional Potential Improvements for Future Sessions
- **Multipart/Chunked Uploads:** For extremely large video files, bypassing the memory buffer via chunked uploads or direct-to-s3 multipart presigned URLs.
- **Bulk Operations:** Endpoints to delete or fetch status for multiple IDs at once, reducing API overhead for batch operations.
- **Tenant Quotas and Analytics:** Storage usage tracking and configurable limits per token.
- **Automated Webhook Retry with Backoff:** Instead of just manual replay, implement an automatic exponential backoff for failed webhook deliveries using TaskIQ's scheduling/retry capabilities.
- **Automated CI/CD Pipeline:** Adding GitHub Actions (or similar) to run the `test` docker compose service automatically on push/PR, since it's fully containerized.

## Production Guidelines & Architecture
The project is built around an "Origin Shield" architecture where NGINX acts as a mandatory entry proxy for both video streaming and `imgproxy` caching (preventing cache stampedes). A detailed guide on this architecture, the Backend-to-Backend security model for `FILE_MANAGER_BEARER_TOKENS`, and cache invalidation strategies is located in `docs/PRODUCTION.md`. 

## Architectural Rules

- **Single Responsibility & Anti-Bloat:** Do not dump every new endpoint or helper into a generic file like `files.py` or `utils.py`. If a `.py` file exceeds ~400 lines, it must be proactively refactored into modular, domain-specific files (e.g. `upload.py`, `playback.py`, `management.py`). Keep the codebase lightweight and highly cohesive.
