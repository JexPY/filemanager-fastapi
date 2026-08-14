<br>
<p align="center">
  <a href="#">
    <img src="https://media2.giphy.com/media/3gWIUenLXoEgPk0BwB/source.gif" alt="Logo" width="80" height="80">
  </a>

  <h3 align="center">Filemanager-FastAPI</h3>

  <p align="center">
    Blazing fast media microservice using FastAPI.
  </p>
</p>

<p align="center">
  <a href="https://github.com/JexPY/filemanager-fastapi/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/JexPY/filemanager-fastapi/ci.yml?branch=master&label=CI" alt="CI"></a>
  <a href="https://github.com/JexPY/filemanager-fastapi/actions/workflows/codeql.yml"><img src="https://img.shields.io/github/actions/workflow/status/JexPY/filemanager-fastapi/codeql.yml?branch=master&label=CodeQL" alt="CodeQL"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=JexPY_filemanager-fastapi"><img src="https://sonarcloud.io/api/project_badges/measure?project=JexPY_filemanager-fastapi&metric=alert_status" alt="Quality Gate Status"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=JexPY_filemanager-fastapi"><img src="https://sonarcloud.io/api/project_badges/measure?project=JexPY_filemanager-fastapi&metric=security_rating" alt="Security Rating"></a>
  <a href="https://snyk.io/test/github/JexPY/filemanager-fastapi"><img src="https://snyk.io/test/github/JexPY/filemanager-fastapi/badge.svg" alt="Known Vulnerabilities"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

---

A media-processing microservice built with FastAPI. Upload images and videos, get back
optimized WebP thumbnails via imgproxy and async H.264/AAC-compressed MP4s. Also generates
QR codes. Storage is pluggable — local disk, S3/R2/MinIO, or Google Cloud Storage.

> The API self-documents interactively at `/docs` (Swagger UI) and `/redoc` (ReDoc). Architectural deep dives and deployment guidelines are available in [`docs/PRODUCTION.md`](docs/PRODUCTION.md).

---

## A note on v1

This project started as a handwritten side project — no LLM assistance, just raw curiosity and
a need for a file management microservice that actually did what I wanted. That original version
lives on the [`before_llm`](https://github.com/JexPY/filemanager-fastapi/tree/before_llm)
branch as a snapshot of where it all began: Pillow-SIMD, libcloud, a rougher but honest
codebase. Worth a look if you're curious about the evolution.

The current version is a full rewrite with a hardened architecture, async-first internals, and
a production-grade feature set — but the spirit is the same.

---

## Architecture & How It Works

Filemanager-FastAPI is built around an **Origin Shield** and **Async Worker** architecture designed for low latency, zero-copy streaming, and resilient media processing.

```mermaid
%%{init: {'theme': 'dark', 'themeVariables': { 'primaryColor': '#1e293b', 'primaryTextColor': '#f8fafc', 'primaryBorderColor': '#3b82f6', 'lineColor': '#60a5fa', 'secondaryColor': '#0f172a', 'tertiaryColor': '#1e1e2e'}}}%%
flowchart TD
    subgraph Ingress ["Ingress & Edge Layer"]
        Client(["Client / App / Browser"])
        Nginx["NGINX Entry Proxy (:9000)<br/><i>Rate Limiting • Cache Shield • Range/Seek</i>"]
    end

    subgraph AppPlane ["API Control Plane (FastAPI)"]
        API["FastAPI Application (:9001)<br/><i>Auth • Validation • Dedup • Routing</i>"]
        AuthModule{"Auth & Scope Guard<br/><i>Static Tokens | HS256 JWTs</i>"}
        VIPS["libvips Pipeline<br/><i>Strip EXIF/GPS • WebP Encode</i>"]
        Segno["Segno Engine<br/><i>QR Codes • Logo Overlays</i>"]
        DB[(PostgreSQL 17<br/><i>Metadata & System of Record</i>)]
    end

    subgraph AsyncPlane ["Async Worker Plane (TaskIQ)"]
        Redis[("Redis Broker & Queue")]
        Worker["TaskIQ Worker<br/><i>FFmpeg Engine + libvips</i>"]
        PosterTask["Poster Frame Extraction"]
        WebhookTask["HMAC-Signed Webhook Dispatcher"]
    end

    subgraph StoragePlane ["Storage & Serving Tier"]
        Storage[("Storage Backend<br/><i>Local Disk / S3 / GCS</i>")]
        Imgproxy["imgproxy<br/><i>On-Demand Signed Transforms</i>"]
    end

    %% Ingress Connections
    Client -->|HTTP Requests| Nginx
    Nginx -->|Proxy Pass| API
    Nginx -.->|X-Accel-Redirect / Native Range| Storage

    %% API Internal Flow
    API --> AuthModule
    AuthModule -->|POST /upload/image| VIPS
    AuthModule -->|POST /generate/qrcode/*| Segno
    AuthModule -->|POST /upload/video| API
    
    VIPS -->|Stream WebP| Storage
    API -->|Stage Raw Video Key| Storage
    API -->|Enqueue Task| Redis
    API <-->|Idempotency & State| DB
    Segno -->|PNG Response| Client

    %% Worker Flow
    Redis --> Worker
    Worker -->|Fetch Raw Key| Storage
    Worker -->|Transcode & Compress| Storage
    Worker --> PosterTask
    PosterTask -->|Generate Poster WebP| Storage
    Worker -->|Update State| DB
    Worker --> WebhookTask
    WebhookTask -.->|Signed POST + SSRF Guard| Client

    %% Playback & Serving Flow
    Imgproxy <--> Storage
    Nginx <-->|Origin Cached Shield| Imgproxy
```

### Core Architecture Highlights

- **Origin Shield NGINX Reverse Proxy (`:9000`)**  
  Terminates external traffic, enforces burst-safe rate limiting on upload routes, isolates `imgproxy` behind cache locks to prevent thundering-herd stampedes, and delivers local video via zero-copy **`X-Accel-Redirect`** (the Python process never touches video playback bytes).
  
- **Synchronous Image Engine (libvips)**  
  High-speed image validation, decompression-bomb protection, EXIF/GPS/ICC metadata stripping, and instant WebP encoding. Serves dynamically resized and cropped thumbnails via HMAC-SHA256 signed `imgproxy` URLs.

- **Async Video Pipeline (TaskIQ + FFmpeg)**  
  Offloads video encoding (H.264/AAC, WebM VP9/AV1) to dedicated worker processes. Supports automated duration capping, thumbnail poster frame extraction, and real-time task status polling.

- **Versatile QR Code Generator (Segno + pyvips)**  
  Stateless, fast QR generation for plain URLs, vCards, MeCards, Wi-Fi networks, Geo-coordinates, and SEPA EPC bank transfers with high-res logo overlays and byte-capped security.

- **PostgreSQL System of Record & Alembic**  
  Every media asset is tracked with immutable owner-scoping, content-hash deduplication (SHA-256), and playback visibility (`private` vs. `public`).

- **Capability-Based Security & Dual Auth**  
  Supports static backend master tokens for internal services and scoped HS256 capability JWTs (`upload:image`, `upload:video`) for direct client uploads without leaking master credentials.

- **Resilient Webhook Delivery with SSRF Guard**  
  Pushes HMAC-SHA256 signed event notifications upon video processing completion, complete with strict DNS/IP validation, dead-letter recording, and manual redelivery replay.

---

## Quickstart

```sh
cp .env-example .env
# Fill in IMGPROXY_KEY, IMGPROXY_SALT, and FILE_MANAGER_BEARER_TOKENS at minimum.
# See .env-example for all options.

docker compose up --build
```

The API is available at `http://localhost:9000`. Swagger UI at `http://localhost:9000/docs`.

```sh
TOKEN=your-token-here

# Upload an image
curl -H "Authorization: Bearer $TOKEN" -F "file=@photo.jpg" \
  http://localhost:9000/upload/image

# Upload a video (async — returns a task_id)
curl -H "Authorization: Bearer $TOKEN" -F "file=@clip.mp4" \
  http://localhost:9000/upload/video

# Poll the task
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/tasks/<task_id>

# Generate a plain text / URL QR code
curl -H "Authorization: Bearer $TOKEN" -F "content=https://example.com" \
  http://localhost:9000/generate/qrcode -o qr.png

# Generate a Wi-Fi QR code with logo
curl -H "Authorization: Bearer $TOKEN" -F "ssid=MyHomeWiFi" -F "password=secret" \
  -F "logo=@logo.png" http://localhost:9000/generate/qrcode/wifi -o wifi_qr.png

# Generate a vCard contact QR code
curl -H "Authorization: Bearer $TOKEN" -F "name=Doe;John" -F "phone=+1234567890" \
  http://localhost:9000/generate/qrcode/vcard -o vcard_qr.png

# List your uploads
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/files
```

---

## Development

Everything runs inside Docker — no local Python environment needed.

```sh
# Full stack
docker compose up --build

# Run tests
docker compose run --rm test pytest -v

# Lint + format + type-check
docker compose run --rm test ruff check .
docker compose run --rm test ruff format .
docker compose run --rm test mypy app
```

---

## License

MIT — see [`LICENSE.txt`](LICENSE.txt).

---

*PRs welcome. If something is broken or confusing, open an issue.*
