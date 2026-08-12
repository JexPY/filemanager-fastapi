# Filemanager-FastAPI

A distributed media-processing microservice: upload images and videos, get back
optimized WebP + on-the-fly imgproxy thumbnails for images, and an async
H.264/AAC-compressed MP4 for videos. Also generates QR codes. Storage is
pluggable (local disk, S3/R2/MinIO, or Google Cloud Storage).

## Architecture

Two process types share one codebase:

- **`api`** (FastAPI, `uvicorn app.main:app`) — handles all HTTP requests.
  Validates and stages uploads in storage, then hands off heavy work.
- **`worker`** (TaskIQ, `taskiq worker app.broker:broker app.tasks`) — runs
  FFmpeg video compression asynchronously.

```
client --(bearer auth)--> api ---+--> image: pyvips validate/strip -> storage -> imgproxy signs a fetch URL
                                  +--> qrcode: segno -> pyvips -> PNG response
                                  +--> video: storage (raw) --key only--> Redis --> worker
                                                                                      |
                                                                                      v
                                                                        ffmpeg compress -> storage
                                                                        (raw deleted after)
```

Only the storage **key** for a video ever travels through Redis — never the
file bytes. The `api` and `worker` containers share a storage volume/bucket
and a Redis instance, nothing else.

## Features

- **Image upload** (`POST /upload/image`) — format-allow-listed (PNG/JPEG/
  GIF/WEBP; SVG and anything else libvips can load is rejected), decoded and
  strip'd of all metadata (EXIF/GPS/ICC/XMP) via pyvips, re-encoded to WebP,
  uploaded to storage. Response includes signed imgproxy URLs for a 300x300
  thumbnail and an auto-format optimized version.
- **Video upload** (`POST /upload/video`) — staged in storage, then
  compressed asynchronously (H.264/AAC via FFmpeg) by a TaskIQ worker.
  Poll `GET /tasks/{task_id}` for status (owner-scoped), or pass a
  `callback_url` to get a signed webhook on completion (see **Webhooks**).
  Output duration is capped (`VIDEO_MAX_DURATION_SECONDS`, default 60s); a
  longer input is truncated and the completed result / `uploads` row report
  `duration_seconds` + `truncated`.
- **QR code generation** (`POST /generate/qrcode`) — segno → SVG → pyvips →
  PNG.
- **Pluggable storage** — local filesystem, S3-compatible (real AWS, R2,
  MinIO), or Google Cloud Storage, selected via `STORAGE_BACKEND`.
- **System-of-record (Postgres)** — every image/video upload is recorded in an
  `uploads` table, owned by the calling token, so uploads are listable and
  deletable (see `GET /files`, `DELETE /files/{id}`). QR codes are returned
  inline and not recorded. The schema is managed by **Alembic** (`migrations/`);
  a one-shot `migrate` compose service applies it before api/worker start.
- **Per-token identity** — each bearer token maps to an owner; uploads,
  listing, and deletion are all scoped to that owner (audit trail + isolation).
- **Idempotent upload (image + video)** — re-uploading identical bytes (per
  owner) returns the existing record instead of storing/processing a duplicate,
  keyed on the input's SHA-256. For video, a still-`processing` match attaches to
  the in-flight compression (202) rather than compressing the same input twice;
  a `ready` match returns 200.
- **Signed completion webhooks** — pass `callback_url` on a video upload and the
  worker POSTs a signed `video.completed`/`video.failed` payload when it's done,
  so you don't have to poll. HMAC-SHA256 signed; SSRF-guarded by a host
  allow-list; delivered on its **own** task (a slow receiver never blocks
  compression), with the outcome persisted so an exhausted delivery can be
  replayed via `POST /files/{id}/redeliver` (see **Webhooks**).
- **Video posters (on request)** — `POST /files/{id}/poster` extracts a frame
  from a ready video and stores it as its own WebP image record, linked back
  from the video via `poster_upload_id`. Reuses the exact image validate/strip
  pipeline. Optional `at_seconds` picks the frame (default ~10% in).
- **Streamable video playback + visibility** — one stable, backend-agnostic URL
  (`GET /files/{id}/download`) plays every video with working HTTP **Range**
  (seek) on all three backends: `local` via nginx `X-Accel-Redirect` (sendfile +
  Range, app out of the byte path), `s3` via a 302 to a presigned GET, `gcp` via
  a 302 to a newly-added V4 signed URL. Owner-controlled visibility (`PATCH
  /files/{id}`) decides auth: `private` (owner-only) or `public` (tokenless),
  plus an unlisted, **revocable** share link (`POST`/`DELETE /files/{id}/share`,
  served at `GET /share/{token}`). See **Playback & visibility**.
- **`/healthz`, `/readyz`** — liveness and dependency-readiness probes
  (`/readyz` checks Redis, storage, and the Postgres metadata store).

## Quickstart

```sh
cp .env-example .env
# generate real values for these two (the example ships placeholders):
openssl rand -hex 32   # -> IMGPROXY_KEY
openssl rand -hex 32   # -> IMGPROXY_SALT
# also set FILE_MANAGER_BEARER_TOKENS in .env, e.g.:
#   FILE_MANAGER_BEARER_TOKENS=dev-token

docker compose up --build
```

On startup the one-shot `migrate` service runs `alembic upgrade head` to create
the `uploads` schema; api and worker wait for it to finish before booting.

The API is fronted by **nginx** on `http://localhost:9000` (the entry proxy —
also what serves `local`-backend video bytes via `X-Accel-Redirect`), imgproxy
on `http://localhost:8080`. The api container is also published directly on
`:9001` for debugging, but hit `:9000` for anything real — local video playback
only works through nginx.

```sh
TOKEN=dev-token

# Image upload
curl -H "Authorization: Bearer $TOKEN" -F "file=@photo.jpg" \
  http://localhost:9000/upload/image

# QR code
curl -H "Authorization: Bearer $TOKEN" -F "content=https://example.com" \
  http://localhost:9000/generate/qrcode -o qr.png

# Video upload (returns a task_id)
curl -H "Authorization: Bearer $TOKEN" -F "file=@clip.mp4" \
  http://localhost:9000/upload/video

# Poll the result
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/tasks/<task_id>

# Request a poster for a ready video, then poll the video until poster_upload_id is set
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:9000/files/<video_id>/poster
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/files/<video_id>

# List your uploads (owner-scoped), then delete one
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/files
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:9000/files/<id>

# Health
curl http://localhost:9000/healthz
curl http://localhost:9000/readyz
```

## Endpoints

All routes except `/healthz`/`/readyz` require `Authorization: Bearer <token>`.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/upload/image` | multipart `file` | `{status, id, dimensions, imgproxy_thumbnail_url, imgproxy_optimized_url}` |
| POST | `/upload/video` | multipart `file`, optional form `callback_url` | `202 {status: "accepted", id, task_id, raw_key}`; identical bytes dedupe to `{status: "duplicate", …}` |
| GET | `/tasks/{task_id}` | — | `{status: "pending"\|"completed"\|"failed", ...}`; `404` for an unknown id **or another owner's task** |
| GET | `/files` | query `limit`,`offset`,`kind` | `{files: [{id, kind, status, storage_key, size_bytes, …}], limit, offset}` — caller's uploads, newest first |
| GET | `/files/{id}` | — | one upload record, or `404` if it isn't the caller's (never includes `share_token`) |
| PATCH | `/files/{id}` | JSON `{visibility}` | set a video's `private`/`public`; `400` non-video, `422` bad value, `404` if not the caller's |
| GET | `/files/{id}/download` | optional `Range` | the permanent playback URL — `public`: 302 to the stable URL or tokenless signed delivery; `private`: owner-only (`404` otherwise) then local X-Accel / s3 presigned / gcs signed 302; `400` non-video |
| POST | `/files/{id}/share` | — | mint/rotate the share link → `{id, share_token, share_url}` (the **only** place the token is returned); `404` if not the caller's |
| DELETE | `/files/{id}/share` | — | `204`, revokes the share token; `404` if not the caller's |
| GET | `/share/{token}` | optional `Range` | serves the video regardless of visibility, no bearer token; `404` for an unknown/revoked token |
| DELETE | `/files/{id}` | — | `204` after deleting the object + record (a video's poster is deleted too); `404` if it isn't the caller's |
| POST | `/files/{id}/poster` | optional form `at_seconds` | `202 {status: "accepted", video_id, task_id, poll}` for a ready video; `200 {status: "ready", poster}` if one already exists; `400` non-video, `409` not ready, `404` if not the caller's |
| POST | `/files/{id}/redeliver` | — | `202 {status: "accepted", id, event, task_id}` re-enqueues the terminal webhook; `400` if no `callback_url`/webhooks off, `409` while still processing, `404` if not the caller's |
| POST | `/generate/qrcode` | form `content` | `image/png` bytes |
| GET | `/healthz` | — | `{status: "ok"}` |
| GET | `/readyz` | — | `200`/`503` with a per-dependency breakdown |

## Webhooks

Instead of polling `GET /tasks/{id}`, pass a `callback_url` form field on
`POST /upload/video`. When compression reaches a terminal state the worker POSTs
a JSON payload there:

```json
{
  "id": "<upload_id>",                 // stable idempotency key (X-Webhook-Id)
  "event": "video.completed",          // or "video.failed"
  "created_at": "2026-08-08T20:06:04Z",
  "data": { /* the uploads record, same shape as GET /files/{id} */ }
}
```

Headers on every delivery:

| Header | Meaning |
|---|---|
| `X-Webhook-Id` | the upload id; **stable across retries** — dedupe on it |
| `X-Webhook-Event` | `video.completed` \| `video.failed` |
| `X-Webhook-Timestamp` | unix seconds, part of the signed material |
| `X-Webhook-Signature` | `sha256=<hex>` = HMAC-SHA256(`WEBHOOK_SIGNING_SECRET`, `"{timestamp}.{raw_body}"`) |

Verify by recomputing the HMAC over `"{X-Webhook-Timestamp}.{raw request body}"`
and comparing (constant-time) to `X-Webhook-Signature`.

**Enabling + safety.** Webhooks are off until **both** `WEBHOOK_SIGNING_SECRET`
and `WEBHOOK_ALLOWED_HOSTS` are set; until then any `callback_url` is rejected
`400`. A `callback_url` is admitted at upload time only if it is `https`
(unless `WEBHOOK_ALLOW_INSECURE_HTTP`), its host is on `WEBHOOK_ALLOWED_HOSTS`,
and — unless `WEBHOOK_ALLOW_PRIVATE_IPS` — it doesn't resolve to a
private/loopback address (SSRF egress control).

**Delivery + replay.** Delivery runs on its **own** TaskIQ task (the compression
task only enqueues it), so a slow or dead receiver never ties up a compression
worker slot. It retries with exponential backoff up to `WEBHOOK_MAX_ATTEMPTS`
and is best-effort — it never affects the compression result. The outcome of the
last cycle is persisted on the `uploads` row (`webhook_status` of
`pending`/`delivered`/`failed`, plus `webhook_attempts` and `webhook_last_error`,
all visible via `GET /files/{id}`). An exhausted delivery (`webhook_status:
"failed"`) is a durable dead-letter record you can replay any time with
`POST /files/{id}/redeliver`, which re-sends the current terminal event with the
same stable `X-Webhook-Id` so an already-processed receiver can dedupe.

## Playback & visibility

Every video has one permanent, backend-agnostic playback URL —
`GET /files/{id}/download` — that clients embed. **Visibility** (set by the owner
via `PATCH /files/{id}` with `{"visibility": "public"|"private"}`, default
`private`) decides both the auth and the URL form:

- **`private`** → owner bearer required; a non-owner or missing token gets `404`
  (existence never leaks). Resolved per backend (below).
- **`public`** → tokenless. If a `*_PUBLIC_BASE_URL` is configured for the
  backend, a `302` to the stable `public_url(key)` (permanent, identical for
  every viewer, edge-cacheable behind a CDN, app out of the read path). If not,
  it falls back to the same signed-URL delivery as private — still tokenless.

**Per-backend byte path** (mirrors imgproxy's keying off `STORAGE_BACKEND`),
always with working HTTP **Range** so a browser `<video>` can seek, and always
with the app out of the byte path:

| Backend | How bytes are served |
|---|---|
| `local` | nginx `X-Accel-Redirect` → `internal;` location, `sendfile` + native Range (dev without nginx: `LOCAL_MEDIA_SERVE_MODE=direct` → Starlette `FileResponse`) |
| `s3` | `302` to a freshly-minted presigned GET (TTL `VIDEO_PLAYBACK_URL_TTL_SECONDS`) |
| `gcp` | `302` to a V4 signed URL, signed locally from the service-account key |

**Share links.** `POST /files/{id}/share` mints (or rotates) an unlisted,
high-entropy token served at `GET /share/{token}` — a valid token plays the video
regardless of visibility, with no bearer. It's a **secret capability**: returned
only by that POST (never in `to_public()`/listings/webhooks) and **revocable** via
`DELETE /files/{id}/share` (beats a never-expiry presigned URL — it has an off
switch). Rotating revokes the previous link.

**No CDN required.** With only S3/GCP creds: private + share → signed URLs;
public with no `*_PUBLIC_BASE_URL` → the same signed-URL delivery (tokenless). No
path requires a public bucket or a CDN — adding one later is a one-env-var change
(`*_PUBLIC_BASE_URL` = the CDN domain) with zero code change.

**Honest caveat.** The `302`-to-signed-URL path gives a permanent *entry* URL,
but the player caches the *resolved* signed URL for the session; a viewer who
pauses past `VIDEO_PLAYBACK_URL_TTL_SECONDS` and then seeks can hit an expired
signature. Size the TTL to a generous session (default 6h). A fully CDN-signed-
cookie path (where the player only ever talks to the stable URL) is the
bulletproof-resume upgrade and is parked for a later pass. **HLS / adaptive
bitrate is out of scope** — progressive download + Range only.

## Configuration

Copy `.env-example` to `.env` and fill in. Fields with no default below fail
startup (`Settings()` validation) if left unset.

| Variable | Required | Notes |
|---|---|---|
| `REDIS_URL` | | TaskIQ broker + result backend |
| `DATABASE_URL` | | Postgres metadata store (defaults to the bundled `db` service) |
| `STORAGE_BACKEND` | | `local` \| `s3` \| `gcp` |
| `LOCAL_STORAGE_DIR`, `LOCAL_PUBLIC_BASE_URL` | | local backend |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_PUBLIC_BASE_URL`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `S3_BUCKET` if `STORAGE_BACKEND=s3` | `S3_ENDPOINT_URL` for R2/MinIO, blank for real AWS |
| `GCP_PROJECT`, `GCP_SERVICE_ACCOUNT_FILE`, `GCS_BUCKET`, `GCS_PUBLIC_BASE_URL` | `GCS_BUCKET` if `STORAGE_BACKEND=gcp` | |
| `IMGPROXY_KEY`, `IMGPROXY_SALT` | ✅ always | hex-encoded, must match the `imgproxy` container's own env vars exactly |
| `IMGPROXY_BASE_URL` | | prefixed onto signed URLs so they're complete/fetchable |
| `FILE_MANAGER_BEARER_TOKENS` | ✅ always | comma-separated; each entry is `secret` (owner = `tok_<hash>`) or `label:secret` (owner = label) |
| `PUBLIC_BASE_URL` | | the API's own external origin; when set, share/download responses return absolute URLs, else relative paths |
| `LOCAL_MEDIA_SERVE_MODE` | | `xaccel` (prod, nginx serves local video bytes) \| `direct` (dev without nginx, Starlette FileResponse) |
| `VIDEO_PLAYBACK_URL_TTL_SECONDS` | | TTL for the s3/gcp playback signed URL (default 6h; GCS caps at 7 days) |
| `VIDEO_MAX_DURATION_SECONDS` | | caps compressed output duration (default 60s); longer inputs are truncated and flagged `truncated: true` in the task result + `uploads` row |
| `WEBHOOK_SIGNING_SECRET`, `WEBHOOK_ALLOWED_HOSTS` | both, to enable webhooks | HMAC secret + comma-separated allow-list of callback hosts; **both** must be set or any `callback_url` is rejected `400` |
| `WEBHOOK_ALLOW_INSECURE_HTTP`, `WEBHOOK_ALLOW_PRIVATE_IPS`, `WEBHOOK_TIMEOUT_SECONDS`, `WEBHOOK_MAX_ATTEMPTS`, `WEBHOOK_RETRY_BACKOFF_SECONDS` | | webhook delivery tuning; the first two default off (https-only, private-IP-blocked) — see **Webhooks** |
| `MAX_IMAGE_UPLOAD_BYTES`, `MAX_VIDEO_UPLOAD_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_QR_CONTENT_LENGTH`, `FFMPEG_TIMEOUT_SECONDS`, `FFMPEG_INPUT_URL_TTL_SECONDS` | | sane defaults, see `app/config.py` |

## Development

Everything — running the app, running tests, linting, type-checking — happens
inside Docker; there's no supported local (host) Python environment.

```sh
# Run the full stack
docker compose up --build

# Run one worker only, for debugging
docker compose up worker

# Run the test suite
docker compose run --rm test pytest -v

# Lint / format / type-check
docker compose run --rm test ruff check .
docker compose run --rm test ruff format .
docker compose run --rm test mypy app

# Test against a local S3-compatible backend (MinIO) instead of the default local storage
docker compose --profile s3-dev up -d minio minio-init
# then set STORAGE_BACKEND=s3 and the S3_* MinIO values from .env-example in .env
```

**Backend verification status**: `local` and `s3` (against real MinIO) have
both been exercised end to end — upload, imgproxy thumbnail resolution,
video compression, cleanup, and now **video playback with Range** (local via
nginx X-Accel → `206`; s3 via a `302` to a presigned URL → follow → Range →
`206`) all confirmed working against a live stack, along with the full visibility
matrix (private 404-without-token / owner-only / 404-for-another-owner; public
tokenless; share link then revoke → 404). The Postgres metadata store (listing,
deletion, idempotency, video record lifecycle, visibility + share tokens) is
verified end to end against real PostgreSQL 17. `gcp` is covered by unit tests
against a mocked client only (no live GCP project/credentials available) —
including the new V4 signed-URL path; treat it as implemented-but-not-live-
verified.

See [`CLAUDE.md`](CLAUDE.md) for the full architectural rundown, non-obvious
invariants, and known sharp edges.

## License

Distributed under the MIT License. See `LICENSE.txt` for details.
