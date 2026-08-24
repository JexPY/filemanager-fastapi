# Responsive image widths & resolve-once serving

Status: **Decided and implemented (2026-08-22).** Resolves the fork previously documented here.

## Decision Summary

1. **Switchable mode, materialized by default:**
   ```ini
   IMAGE_RENDITION_MODE=materialize   # materialize | on_demand
   ```
   - `materialize` (**default**): extra responsive width renditions are encoded at upload via libvips, stored as distinct objects in storage, and served directly from CDN. `imgproxy` is off the request path.
   - `on_demand`: no extra width objects are stored at upload; widths are generated dynamically by `imgproxy` at request time.

2. **Aspect-preserving width specs:**
   Alongside the square `thumbnail` (300×300, `crop=True`, suffix `t300`), three aspect-preserving width specs are registered in `app/services/renditions.py`:
   - `w400` (width 400, suffix `w400`, `crop=False`)
   - `w800` (width 800, suffix `w800`, `crop=False`)
   - `w1600` (width 1600, suffix `w1600`, `crop=False`)

3. **Width filtering rule (`≤ source width`):**
   Only encode (in `materialize` mode) and only emit (in both modes) width specs that are `≤` the source image width. A 1600 entry from a 1280 source is neither encoded nor returned in `renditions`.

4. **Public keys exposed for resolve-once rendering:**
   `UploadRecord.to_public()` and image upload responses return `storage_key` and `renditions` dict containing object keys (not absolute URLs). Consumers store `id`, `storage_key`, and `renditions`, and construct URLs as `{MEDIA_BASE_URL}/{key}` without querying the API on page renders.

---

## Rationale & Design Context

### Why Materialize by Default

Images in this service are written once and read thousands of times. Synchronously paying encode CPU once at upload beats paying CPU on every request and avoids cold-cache stampedes on CDN purges. Serving directly from CDN object URLs keeps `imgproxy` off the hot path for all public image views.

`on_demand` remains fully supported via `IMAGE_RENDITION_MODE=on_demand`. Switching between modes is reversible with no migration: existing records keep resolving through their stored keys or imgproxy fallbacks.

### Width Specs Selection

Derived from real frontend responsive breakpoints and `sizes` attributes:

| Surface | `sizes` | CSS px @1440 | @2× DPR | Selected Spec |
|---|---|---|---|---|
| Card (16:9) | `100vw / 50vw / 25vw` | ~360 | ~720 | `w400`, `w800` |
| Gallery cover (4:3) | `100vw / 66vw` | ~950 | ~1900 | `w800`, `w1600` |
| Gallery thumbs (square) | `33vw / 20vw` | ~288 | ~576 | `thumbnail` (300×300) |
| Lightbox / Hero | `100vw` | ~1024 | ~2048 | `w1600`, primary original |

The primary optimized WebP object (1280 or 1920 on the long edge depending on `optimization`) forms the top of the `srcset`.

### Resolution Fallback Architecture

`_derive_rendition_public_url` in `app/services/renditions.py` handles resolution:
- When a rendition object exists in `renditions` and `*_PUBLIC_BASE_URL` is set: returns `{PUBLIC_BASE_URL}/{key}` directly.
- When `renditions` is empty (e.g. `on_demand` mode or pre-existing legacy records): resolves to a signed `imgproxy` transform with matching resize options.
- Private media routes through authenticated `/files/{id}/download?rendition=w400`.

---

## Local Development vs. S3 Profile for the Key Model

### Why `STORAGE_BACKEND=local` Returns 404 on Direct Key Reads
In local development with `STORAGE_BACKEND=local`, NGINX configures only `internal;` locations (`/internal-media/` and `/internal-object/`) for zero-copy `X-Accel-Redirect` streaming. There is intentionally **no public static route** for stored objects. Consequently, making a direct HTTP request to `http://localhost:9000/images/<key>.webp` yields a `404 Not Found`.

> [!CAUTION]
> **Do not add a public static NGINX route for local object directories.**
> Exposing the local media directory directly to HTTP clients would allow anyone with a key to bypass authentication for private records, defeating the access control guarantees.

### Exercising the Direct-CDN Key Model Locally (Garage Profile)
To test and exercise the resolve-once direct-CDN key model locally (joining a public base URL to a stored key like `http://filemanager-test.localhost:3902/images/<key>.webp` without authentication), use the `s3-dev` compose profile powered by Garage:

1. **Start the S3 dev services:**
   ```bash
   docker compose --profile s3-dev up -d garage garage-init
   ```

2. **Configure `.env` for local S3 testing:**
   ```ini
   STORAGE_BACKEND=s3
   S3_BUCKET=filemanager-test
   S3_ENDPOINT_URL=http://garage:3900
   S3_PUBLIC_BASE_URL=http://localhost:9002/filemanager-test
   AWS_REGION=garage
   AWS_ACCESS_KEY_ID=garageadmin
   AWS_SECRET_ACCESS_KEY=garageadminsecretkey
   ```

3. **Restart the API:**
   ```bash
   docker compose up -d api worker nginx
   ```

With this profile, public uploads return `storage_key` and `renditions` that resolve directly against `{S3_PUBLIC_BASE_URL}/{key}`.
