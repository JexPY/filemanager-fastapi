# Integrating with filemanager-fastapi

Integration guide for client applications and consuming backends using `filemanager-fastapi`.

---

## 1. Authentication & Upload Flows

`filemanager-fastapi` supports two authentication mechanisms:

1. **Static Master Tokens (`FILE_MANAGER_BEARER_TOKENS`)**:
   - Used for backend-to-backend communication and administrative operations (`POST /upload/presign`, `GET /whoami`, `POST /files/batch`).
   - Must remain on your secure backend; never send master tokens to browser clients.

2. **Capability JWTs (`JWT_SECRET_KEY`)**:
   - Short-lived scoped tokens designed for browser-direct uploads.
   - Your backend authenticates the user and signs an HS256 JWT with `scopes: ["upload:image"]` (or `upload:video` / `upload:file`), `sub: "<tenant_or_user_id>"`, and a short `exp`.
   - Browser POSTs multipart file payload straight to `/upload/image` or `/upload/images` (with `Authorization: Bearer <jwt>` or `?token=<jwt>`).
   - Browser receives upload response containing `id`, `storage_key`, `renditions`, and dimensions, and sends `id`, `storage_key`, and `renditions` to your application database.

---

## 2. Image Upload Contracts

### Single Upload (`POST /upload/image`)
Supports `thumbnail=true` to generate responsive renditions and thumbnail. Every ready image response also carries LQIP placeholder fields (`dominant_color` and `blur_data_url`):

```json
{
  "status": "success",
  "id": "0f1c37b3400a4b08b52f5ef45187e101",
  "storage_key": "images/4b17c80521ad47a884819ca1e7c9bf8f.webp",
  "renditions": {
    "thumbnail": "images/4b17c80521ad47a884819ca1e7c9bf8f_t300.webp",
    "w400": "images/4b17c80521ad47a884819ca1e7c9bf8f_w400.webp",
    "w800": "images/4b17c80521ad47a884819ca1e7c9bf8f_w800.webp",
    "w1600": "images/4b17c80521ad47a884819ca1e7c9bf8f_w1600.webp"
  },
  "dimensions": { "width": 1920, "height": 1280 },
  "size_bytes": 145020,
  "size_mb": 0.14,
  "dominant_color": "#1e293b",
  "blur_data_url": "data:image/webp;base64,UklGRl...",
  "url": "https://cdn.example.com/images/4b17c80521ad47a884819ca1e7c9bf8f.webp",
  "thumbnail_url": "https://cdn.example.com/images/4b17c80521ad47a884819ca1e7c9bf8f_t300.webp"
}
```

### Bulk Upload (`POST /upload/images`)
Guarantees 1:1 positional array mapping with explicit status discriminator and counters:

```json
{
  "succeeded": 2,
  "failed": 0,
  "total": 2,
  "items": [
    {
      "status": "success",
      "id": "0f1c37b3...",
      "storage_key": "images/abc.webp",
      "renditions": { "thumbnail": "images/abc_t300.webp", "w400": "images/abc_w400.webp" },
      "dimensions": { "width": 600, "height": 400 },
      "dominant_color": "#1e293b",
      "blur_data_url": "data:image/webp;base64,UklGRl...",
      "url": "https://cdn.example.com/images/abc.webp",
      "thumbnail_url": "https://cdn.example.com/images/abc_t300.webp"
    }
  ]
}
```

### Optimization Profiles (`optimization`)

Images can be uploaded with four optimization profiles:
- `balanced` (default): Q=85, max dimension 1920px. Standard profile for photos and general web assets.
- `size`: Q=65, max dimension 1280px. Aggressive compression for size-critical bandwidth constraints.
- `quality`: Q=95, max dimension 3840px. High-fidelity encoding for hero assets and photography portfolios.
- `lossless`: Lossless WebP encoding with max dimension capped at 4096px (protecting against libwebp's 16383px ceiling).

> [!NOTE]
> `lossless` is designed for graphics with flat colours, sharp edges, diagrams, screenshots, and logos where lossy compression artifacts are unacceptable.
>
> **Do not use it on photographs.** Measured on real iPhone photos, `lossless` is roughly **20–30× larger and 7–11× slower** than `balanced` — a 4284×5712 HEIC (2.66 MB in) produces a **9.3 MB** object in about **6 seconds**, versus 0.35 MB in 0.8 s at `balanced`. Note the output is larger than the *input*: a lossy-compressed source re-encoded losslessly grows. Six seconds is also long enough to matter for client and CDN read timeouts.
>
> On the content it is meant for, it wins decisively in both directions: a 1920×1080 flat-colour graphic came out **16.7× smaller and 5.4× faster** than `balanced`.
>
> Materialized renditions (`thumbnail`, `w400`, etc.) always remain lossy even for lossless uploads to ensure responsive delivery remains compact.

---

## 3. Video Upload & Lifecycle

1. `POST /upload/video` accepts the upload (`202 Accepted`) and enqueues FFmpeg compression.
2. Poll `GET /files/{id}` until `status == "ready"`.
3. To generate a poster frame, call `POST /files/{id}/poster` and poll `GET /files/{id}` until `poster_upload_id` is populated.

---

## 4. Deleting Files

Call `DELETE /files/{id}` using an owner-scoped static token or `manage:files` JWT.
- Automatically cascades deletion to all materialized rendition objects (`thumbnail`, `w400`, `w800`, `w1600`).
- For video records, automatically cascades deletion to the linked poster image and its objects.

---

## 5. Storing Media in Consumer Databases: The Resolve-Once Model

> [!IMPORTANT]
> **Store `id`, `storage_key`, and `renditions`. Never call the API on every page render.**

### The Problem with Querying on Every Render
Earlier guidance suggested storing only the record `id` and calling `POST /files/batch` on every page render to resolve URLs. For public immutable images, that adds an unnecessary network round trip per page render and introduces a hard dependency on `filemanager-fastapi` being up in order to render images hosted on a CDN.

### The Resolve-Once Architecture
Media objects in `filemanager-fastapi` are immutable: a storage key always points to the exact same bytes.

> [!NOTE]
> `storage_key` and `renditions` are exposed **only on public, ready records**. Private records withhold both keys to prevent direct, unauthenticated bucket access. For private media, store the record `id` and fetch media through `GET /files/{id}/download` (or `?rendition=w400`).

When a public upload completes, store:
1. `id` — needed for deletion (`DELETE /files/{id}`) and administrative operations.
2. `storage_key` — the relative object path for the primary asset (e.g. `images/4b17c80521ad47a884819ca1e7c9bf8f.webp`).
3. `renditions` — a JSON map of rendition names to their relative storage keys (e.g. `{"thumbnail": "images/..._t300.webp", "w400": "images/..._w400.webp"}`).
4. `width` & `height` — dimensions for layout calculation and aspect-ratio preservation.
5. `dominant_color` & `blur_data_url` — LQIP placeholders for instant rendering while loading.

### Using LQIP Placeholders on the Consumer
Every ready image record carries:
- `dominant_color` (e.g. `#1e293b`): Use as `background-color` on the image wrapper for a solid background before any bytes load.
- `blur_data_url` (16px WebP base64 URI): Use as an inline blur preview (e.g. Next.js `<Image placeholder="blur" blurDataURL={photo.blur_data_url} />` or an underlying `<img>` with CSS `filter: blur(20px)`). The service intentionally does **not** pre-blur the 16px tile to minimize payload size and preserve consumer control over blur radius.

> [!NOTE]
> Unlike accelerator URLs (`thumbnail_url`, `poster_url`) and `storage_key`, `dominant_color` and `blur_data_url` are exposed on **both public and private records** (gated on `kind == "image"` and `status == "ready"`), since they leak no storage keys or object URLs.

### Constructing URLs on the Consumer
Consumers configure `MEDIA_BASE_URL` (e.g. `https://cdn.example.com`) and build URLs via simple string concatenation:

```typescript
// Primary URL
const imageUrl = `${MEDIA_BASE_URL}/${photo.storage_key}`;

// Responsive srcset
const srcSet = Object.entries(photo.renditions)
  .filter(([name]) => name.startsWith('w'))
  .map(([name, key]) => `${MEDIA_BASE_URL}/${key} ${name.slice(1)}w`)
  .join(', ');
```

### Why Keys Rather Than Absolute URLs
1. **Zero Data Migrations on CDN / Domain Changes**: Changing your CDN domain requires only updating `MEDIA_BASE_URL` on your consumer application, with no database updates needed.
2. **Decoupled Key Naming**: The consumer does not hardcode internal naming patterns (`_t300`, `_w400`), remaining insulated from backend naming implementation details.

### Why Storage Key Must Be Stored (Cannot Be Computed from ID)
The record `id` and the `storage_key` UUID are two independent `uuid4()` values generated during upload. You cannot derive the storage key from the record id.

### Behaviour in `on_demand` Mode
When `IMAGE_RENDITION_MODE=on_demand` is configured on the service, `renditions` is empty (`{}`) in upload responses. In that mode, consumers fall back to the record's `url` / `thumbnail_url` which route through `imgproxy`.

### What `POST /files/batch` is For Now
`POST /files/batch` remains the authoritative API for:
- Administrative tooling, catalog reconciliation, and backfill scripts.
- Orphan detection and cleanup jobs.
- Repairing metadata on out-of-sync rows.
It is explicitly **not** meant for the hot page-rendering path.
