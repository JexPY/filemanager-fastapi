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
  allow-list (see **Webhooks**).
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

The API listens on `http://localhost:9000`, imgproxy on `http://localhost:8080`.

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
| POST | `/upload/image` | multipart `file` | `{status, id, dimensions, raw_url, imgproxy_thumbnail_url, imgproxy_optimized_url}` |
| POST | `/upload/video` | multipart `file`, optional form `callback_url` | `202 {status: "accepted", id, task_id, raw_key}`; identical bytes dedupe to `{status: "duplicate", …}` |
| GET | `/tasks/{task_id}` | — | `{status: "pending"\|"completed"\|"failed", ...}`; `404` for an unknown id **or another owner's task** |
| GET | `/files` | query `limit`,`offset`,`kind` | `{files: [{id, kind, status, storage_key, size_bytes, …}], limit, offset}` — caller's uploads, newest first |
| GET | `/files/{id}` | — | one upload record, or `404` if it isn't the caller's |
| DELETE | `/files/{id}` | — | `204` after deleting the object + record; `404` if it isn't the caller's |
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
private/loopback address (SSRF egress control). Delivery retries with
exponential backoff up to `WEBHOOK_MAX_ATTEMPTS` and is best-effort: an
exhausted delivery is logged, never retried durably, and never affects the
compression result.

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
| `VIDEO_MAX_DURATION_SECONDS` | | caps compressed output duration (default 60s); longer inputs are truncated and flagged `truncated: true` in the task result + `uploads` row |
| `WEBHOOK_SIGNING_SECRET`, `WEBHOOK_ALLOWED_HOSTS` | both, to enable webhooks | HMAC secret + comma-separated allow-list of callback hosts; **both** must be set or any `callback_url` is rejected `400` |
| `WEBHOOK_ALLOW_INSECURE_HTTP`, `WEBHOOK_ALLOW_PRIVATE_IPS`, `WEBHOOK_TIMEOUT_SECONDS`, `WEBHOOK_MAX_ATTEMPTS`, `WEBHOOK_RETRY_BACKOFF_SECONDS` | | webhook delivery tuning; the first two default off (https-only, private-IP-blocked) — see **Webhooks** |
| `MAX_IMAGE_UPLOAD_BYTES`, `MAX_VIDEO_UPLOAD_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_QR_CONTENT_LENGTH`, `FFMPEG_TIMEOUT_SECONDS` | | sane defaults, see `app/config.py` |

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
video compression, and cleanup all confirmed working against a live stack. The
Postgres metadata store (listing, deletion, idempotency, video record
lifecycle) is verified end to end against real PostgreSQL 17. `gcp` is covered
by unit tests against a mocked client only (no live GCP project/credentials
available); treat it as implemented-but-not-live-verified.

See [`CLAUDE.md`](CLAUDE.md) for the full architectural rundown, non-obvious
invariants, and known sharp edges.

## License

Distributed under the MIT License. See `LICENSE.txt` for details.
