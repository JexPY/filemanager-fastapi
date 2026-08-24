# Production Deployment Guidelines

When transitioning this project from local development to production, several key architectural and security considerations must be addressed.

## 1. The "Golden Architecture" (CDN + Origin Shield)

This project is built to handle massive scale efficiently by heavily leveraging caching, both globally (via a CDN) and locally (via NGINX Origin Shielding).

### Recommended Stack
1. **Edge/CDN Layer (e.g., Cloudflare, AWS CloudFront):**
   - The internet-facing entry point.
   - Responsible for terminating SSL/TLS, preventing DDoS attacks, and caching static responses globally.
   - **Crucial for Images:** derived image URLs carry a real `.webp` extension, which most CDNs cache by default. Do not *rely* on that default — set an explicit Cache Rule for your media path so a change in the CDN's default extension list cannot silently stop caching your images.
2. **Origin Shield (NGINX):**
   - The entry point to your server (VPS/Cloud).
   - `nginx.conf.template` uses `proxy_cache_lock on;` for `imgproxy`. If the CDN drops its cache and 10,000 users request an image simultaneously, NGINX acts as a shield. It holds back 9,999 requests and allows only *one* request to hit `imgproxy`, protecting your CPU from a "Cache Stampede".
3. **Application Layer (FastAPI, imgproxy):**
   - FastAPI handles authentication and database sync.
   - `imgproxy` handles on-demand image resizing. It is hidden behind NGINX.
4. **Worker Layer (TaskIQ):**
   - Runs asynchronously to process (compress) videos via FFmpeg without blocking the web API.
5. **Storage Layer (AWS S3, GCP, Backblaze B2, etc.):**
   - The absolute source of truth. Decoupled from your server's disk space.

### Mandatory NGINX in Production
No matter which storage backend you use (`local`, `s3`, `gcp`, `b2`), **NGINX is absolutely mandatory in production.**
- If using `local` storage, NGINX is required to stream video bytes via `X-Accel-Redirect`.
- If using `s3`, `gcp` or `b2`, NGINX is required to act as the Origin Shield for `imgproxy`.

Do not expose the API (`port 9001`) or `imgproxy` (`port 8080`) directly to the internet. Route all traffic through NGINX (`port 9000` internally, usually bound to `80` or `443` externally).

---

## 2. Security: Credentials and who may hold them

There are two kinds of credential, and the difference decides who is allowed to hold each one.

### Static master tokens (`FILE_MANAGER_BEARER_TOKENS`)

> [!CAUTION]
> **Never let a static token reach a browser.**
> Whoever holds one has complete read, write, and delete access to every file under that token's owner.

This is the backend-to-backend secret. It stays on your server.

**Always give it an explicit label** — write `FILE_MANAGER_BEARER_TOKENS=myapp:the-secret`, not `the-secret` on its own. The label before the colon becomes the `owner` recorded on every upload. An unlabelled entry falls back to a derived `tok_<hash>` owner, which still works but is opaque, and will not match the `sub` your backend signs into its capability JWTs. When those two disagree the symptom is silent: uploads succeed, ids come back, and then every lookup returns nothing — indistinguishable from the service being down. The app logs a warning at startup if it finds an unlabelled entry.

Verify the pairing with `GET /whoami`, which returns the owner your credential actually resolves to.

### Capability JWTs (signed with `JWT_SECRET_KEY`)

These are short-lived, narrow tokens **designed to be handed to a browser**. Your backend signs one itself (the secret is shared — no round trip needed) or mints one via `POST /upload/presign`.

The scopes form a ladder, and each rung goes to a less trusted party than the one above:

| Credential | Held by | May do |
|---|---|---|
| static master token | your backend only | everything |
| `manage:files` JWT | your backend | every owner-scoped route: list, get, batch, patch, delete, share |
| `upload:image` / `upload:video` / `upload:file` JWT | **the browser** | only the matching `/upload/*` verb |
| `read:file` JWT (+ `file` claim) | **the browser** | the bytes of exactly one named record |

**The bottom two rungs grant no owner access at all.** An upload token calling `GET /files`, `DELETE /files/{id}`, or `POST /files/batch` gets a `403`. That is what makes it safe to hand out widely, and it is why an upload token must never be reused as a read credential or vice versa.

### The upload flow (bytes never touch your backend)

1. The browser asks your backend for permission to upload.
2. Your backend authenticates the user, then signs a capability JWT with a short `exp` and a single scope.
3. The browser POSTs the file **straight to this service** — `POST /upload/image?token=<jwt>`, or via an `Authorization: Bearer` header.
4. The service returns the record, including its `id` and ready-to-use URLs.
5. The browser sends the `id` back to your backend, which saves it.

Your backend never buffers or proxies media bytes.

> [!IMPORTANT]
> **`CORS_ALLOWED_ORIGINS` must list your frontend's origins, or this flow breaks silently.**
> A multipart POST carrying its credential as `?token=` is a CORS-*simple* request. Without the origin configured the upload **succeeds and the file is stored**, but the browser withholds the response — so your backend never learns the `id`, and every upload becomes an orphan in storage. Blank config disables the CORS middleware entirely.

---

## 3. Serving images: resolve once, then serve from the CDN

The record returned on upload already contains the URLs you need (`url`, `thumbnail_url`, and `custom_url` when custom dimensions were requested). **Save what you need at upload time and serve from the CDN from then on.**

Do **not** call the API on every page render to rebuild a URL. `GET /files/{id}` and `POST /files/batch` exist for admin work — backfills, repairs, auditing what exists — not for the hot path of a page load. Putting them there adds a network round trip to every render and makes your pages depend on this service being up in order to show an image that lives on a CDN.

On an object-store backend with a `*_PUBLIC_BASE_URL` configured, `url` and `thumbnail_url` are **direct CDN object URLs** — imgproxy is not on the request path at all. Without a public base URL (including all `local` storage setups), they fall back to signed imgproxy URLs or the canonical `/files/{id}/download`.

### Public vs Private Media & Key Exposure

> [!IMPORTANT]
> **The resolve-once key model applies exclusively to public media.**
> `storage_key` and `renditions` are exposed **only** when a record is both `public` and `ready`. Private records withhold raw storage keys and rendition maps from all API responses to prevent unauthenticated direct reads from public CDN buckets.

Private media must always be fetched through `GET /files/{id}/download` (or `?rendition=w400` / `?rendition=thumbnail`) using the owner's master token or a short-lived capability JWT with `scopes: ["read:file"]` and matching `file` claim.

### Storage Layout & Defense-in-Depth (`private/` Prefix)

Media is partitioned at the storage layer by visibility:
- **Public:** `images/<uuid>.webp`, `images/<uuid>_t300.webp`, `videos/<uuid>_compressed.mp4`, `posters/<uuid>.webp`, `files/<uuid>.<ext>`
- **Private:** `private/images/<uuid>.webp`, `private/images/<uuid>_t300.webp`, `private/videos/<uuid>_compressed.mp4`, `private/posters/<uuid>.webp`, `private/files/<uuid>.<ext>`

This layout provides two independent layers of security:
1. **Application Layer:** Private records withhold `storage_key` and `renditions` in all JSON views (`to_public()`, upload responses).
2. **Infrastructure / CDN Layer:** Bucket policies and edge WAF rules (e.g. Cloudflare / CloudFront) should be configured with a blanket block on `/private/*`. Even if an unguessable private key were inadvertently shared, public requests to `{CDN_BASE_URL}/private/*` will be rejected at the edge.

### Visibility Transitions & Key Rotation

- **Public $\rightarrow$ Private:** Copies the object and all renditions to fresh UUIDs under `private/`, re-points the database record, and deletes the old public objects. This immediately invalidates all previously cached CDN URLs and embedded links.
- **Private $\rightarrow$ Public:** Copies the object and all renditions to fresh UUIDs under the public prefix (e.g. `images/`), re-points the database record, deletes the old `private/` objects, and begins emitting `storage_key` and `renditions` in API responses.

Two properties make storing public keys safe:
- Files are **immutable**. A given storage key always points to the same bytes.
- imgproxy signatures **never expire**, deliberately, so a derived URL stays valid and keeps a stable CDN cache key.

`GET /files/{id}/download` is the exception that is always safe to embed: it is permanent and backend-agnostic, which makes it the right URL for a `<video src>`.

---

## 4. Cache Invalidation

Because files in this system are largely immutable (a specific UUID always points to the exact same bytes), you rarely need to drop the cache.

> [!WARNING]
> **One image has several cached URLs. Purging one is not enough.**
> A single upload can be reachable through its primary object URL, its `_t300` thumbnail, any `custom_url` variant that was generated, and — for video — its poster. If you are removing a violating image, purging only the primary URL leaves it publicly visible through its thumbnail. Purge every derived URL, or purge by path prefix.

To remove an image:
1. Delete it via the API (`DELETE /files/{id}`). This cascades to the record's renditions and, for video, its poster.
2. Purge **all** of its URLs in your CDN — prefer a prefix purge over pasting a single URL.
3. (Optional) To clear the local NGINX Origin Shield cache, restart the NGINX container or delete `/tmp/nginx_cache` inside it.

---

## 5. Operations

### There is no garbage collection

Nothing walks your application's foreign keys. If a user abandons a form after the file was uploaded, or your backend fails between the upload and the database write, the object stays in storage forever with nothing referencing it. Deleting a parent record in your application **must** issue `DELETE /files/{id}` for each of its media ids.

Plan a periodic reconciliation job that lists files and deletes any id your database does not know about. `POST /files/batch` is well suited to this — it is exactly the kind of admin work it was built for.

### Sharing Redis with another service

If this Redis instance is shared, note that **both** TaskIQ keyspaces are unbounded and
non-expiring by default, and set the two retention settings:

- `TASKIQ_RESULT_TTL_SECONDS` — without it `RedisAsyncResultBackend` issues a plain `SET`, so
  every task result is kept forever.
- `TASKIQ_STREAM_MAXLEN` — `XACK` only clears the pending-entries list; it does **not** delete
  the entry, and taskiq never issues `XDEL`. Without a trim the stream grows by every job ever
  processed. Keep it well above the worst-case *pending* backlog: entries beyond the trim are
  dropped, and a dropped entry is a lost job.

This matters most under `maxmemory-policy volatile-lru`, the usual choice for a shared cache.
That policy can only evict keys **that have an expiry** — so non-expiring TaskIQ keys are
exactly the ones it can never reclaim. They grow until the instance refuses writes, which takes
down the co-tenant's cache and rate limiter as well as this service's queue.

Separate logical DB indices (`/0`, `/1`) partition the *keyspace*, not the *memory* — the
`maxmemory` budget is per instance. Isolation of names is not isolation of capacity.

### There are no per-tenant quotas

With one shared owner there is no per-user storage limit. Enforce any such limit in your own application.

### Health checks

Point your orchestrator at the right endpoint:

| Endpoint | Checks | Use for |
|---|---|---|
| `GET /healthz` | the process is serving; no dependencies | **liveness** — a database blip must not restart a recoverable process |
| `GET /readyz` | Redis, storage, and the metadata store | **readiness** — stop routing traffic to a crippled instance |

### Video duration is truncated, not rejected

`VIDEO_MAX_DURATION_SECONDS` defaults to 60. Longer clips are silently cut to that length; the verdict is recorded on the row (`duration_seconds`, `truncated`) and visible via `GET /files/{id}`. Raise it deliberately if your use case needs longer video.

---

## 6. Environment Configuration Checklist

Before deploying, ensure you have configured:

**Secrets**
- `IMGPROXY_KEY` and `IMGPROXY_SALT` — generate securely, e.g. `openssl rand -hex 32`.
- `FILE_MANAGER_BEARER_TOKENS` — strong random secrets, **each with an explicit `label:secret` prefix**.
- `JWT_SECRET_KEY` — required for browser-direct uploads. Must match the value your consuming backend signs with. Without it, capability JWTs are rejected and only static tokens work.

**Networking**
- `PUBLIC_BASE_URL` and `IMGPROXY_BASE_URL` — your real production domain.
- The `*_PUBLIC_BASE_URL` for your storage backend (`S3_PUBLIC_BASE_URL`, `GCS_PUBLIC_BASE_URL`, `B2_PUBLIC_BASE_URL`). Without it, images are served through this service instead of straight from the CDN.
- `CORS_ALLOWED_ORIGINS` — every browser origin that will upload. See the warning in §2; getting this wrong fails silently.

**Rate limiting behind a proxy**
- `NGINX_TRUSTED_PROXY_CIDR` — defaults to `127.0.0.1/32`, i.e. effectively off. Behind a CDN or load balancer without it, every request appears to come from the proxy and the per-IP upload throttle collapses into **one global bucket shared by all users**, so they throttle each other. Set it to your proxy's address or subnet. Never set it to `0.0.0.0/0`: `X-Forwarded-For` is client-controlled, and trusting it from an untrusted source is a rate-limit bypass.
- `NGINX_UPLOAD_RATE` / `NGINX_UPLOAD_BURST` — tune to your traffic.
- `NGINX_MAX_BODY_SIZE` — the video ceiling. Image and file endpoints have their own tighter per-location limits.

**Caching**
- `ENABLE_IMGPROXY_CACHE=true` — ensures the Origin Shield is active.

**Webhooks (only if you use video `callback_url`)**
- `WEBHOOK_SIGNING_SECRET` — signs the HMAC-SHA256 callback so the receiver can verify it.
- `WEBHOOK_ALLOWED_HOSTS` — restricts where callbacks may be delivered.

### Verify after deploying

- `GET /readyz` returns `200` with every dependency `ok`.
- `GET /whoami` with your static token returns the **label you configured**, not a `tok_<hash>`.
- A browser upload from your real frontend origin returns a response the browser can read (check the Network tab for a CORS error, not just a `200`).
- The startup log contains no unlabelled-token warning.
