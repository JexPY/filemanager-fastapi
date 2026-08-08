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
  Poll `GET /tasks/{task_id}` for status.
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
- **Idempotent image upload** — re-uploading identical bytes (per owner)
  returns the existing record instead of storing a duplicate, keyed on the
  input's SHA-256.
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
| POST | `/upload/video` | multipart `file` | `202 {status: "accepted", id, task_id, raw_key}` |
| GET | `/tasks/{task_id}` | — | `{status: "pending"\|"completed"\|"failed", ...}` or `404` for an unknown id |
| GET | `/files` | query `limit`,`offset`,`kind` | `{files: [{id, kind, status, storage_key, size_bytes, …}], limit, offset}` — caller's uploads, newest first |
| GET | `/files/{id}` | — | one upload record, or `404` if it isn't the caller's |
| DELETE | `/files/{id}` | — | `204` after deleting the object + record; `404` if it isn't the caller's |
| POST | `/generate/qrcode` | form `content` | `image/png` bytes |
| GET | `/healthz` | — | `{status: "ok"}` |
| GET | `/readyz` | — | `200`/`503` with a per-dependency breakdown |

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
| `MAX_IMAGE_UPLOAD_BYTES`, `MAX_VIDEO_UPLOAD_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_QR_CONTENT_LENGTH`, `FFMPEG_TIMEOUT_SECONDS`, `TASK_STATUS_TTL_SECONDS` | | sane defaults, see `app/config.py` |

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
