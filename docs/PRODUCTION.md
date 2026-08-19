# Production Deployment Guidelines

When transitioning this project from local development to production, several key architectural and security considerations must be addressed. 

## 1. The "Golden Architecture" (CDN + Origin Shield)

This project is built to handle massive scale efficiently by heavily leveraging caching, both globally (via a CDN) and locally (via NGINX Origin Shielding).

### Recommended Stack
1. **Edge/CDN Layer (e.g., Cloudflare, AWS CloudFront):**
   - The internet-facing entry point.
   - Responsible for terminating SSL/TLS, preventing DDoS attacks, and caching static responses globally.
   - **Crucial for Images:** With the built-in `.webp` extension generation, Cloudflare Free tier will cache resized images automatically.
2. **Origin Shield (NGINX):**
   - The entry point to your server (VPS/Cloud).
   - `nginx.conf.template` uses `proxy_cache_lock on;` for `imgproxy`. If the CDN drops its cache and 10,000 users request an image simultaneously, NGINX acts as a shield. It holds back 9,999 requests and allows only *one* request to hit `imgproxy`, protecting your CPU from a "Cache Stampede".
3. **Application Layer (FastAPI, imgproxy):**
   - FastAPI handles authentication and database sync.
   - `imgproxy` handles the heavy CPU lifting of image resizing. It is hidden behind NGINX.
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

## 2. Security: The Backend-to-Backend Pattern

The single most critical security component of this microservice is the `FILE_MANAGER_BEARER_TOKENS`.

> [!CAUTION]
> **Never leak a Bearer Token to a frontend client.**
> Whoever possesses a Bearer Token has complete read, write, and delete access to every file uploaded under that token's namespace. 

### How to use it safely
This service is designed as an internal microservice, meant to be consumed by your **Main Application Backend** (e.g., Node.js, Django, Laravel), *not* directly by your frontend (React, iOS).

1. **Upload Flow:**
   - The end-user uploads a file to your Main Backend.
   - Your Main Backend authenticates the user, then makes a server-to-server request to the File Manager using the secret `FILE_MANAGER_BEARER_TOKEN`.
   - The File Manager returns a `file_id`.
   - Your Main Backend saves that `file_id` to its own database.
2. **Download/View Flow:**
   - To display an image, your Main Backend returns the pre-signed `imgproxy` URL to the frontend. The frontend loads the image directly from the CDN/File Manager without needing any Bearer Token.

---

## 3. Cache Invalidation

Because files in this system are largely immutable (a specific UUID always points to the exact same bytes), you rarely need to drop the cache. 

If you must invalidate an image cache (e.g., removing a violating image):
1. Delete the image via the API (`DELETE /files/{id}`).
2. Invalidate the URL in your CDN (e.g., Cloudflare Dashboard -> Purge Cache -> Custom Purge -> Paste the full `imgproxy` URL).
3. (Optional) If you need to clear the local NGINX Origin Shield cache, you can restart the NGINX container or manually delete the `/tmp/nginx_cache` directory inside the container.

---

## 4. Environment Configuration Checklist

Before deploying, ensure you have configured:
- `IMGPROXY_KEY` and `IMGPROXY_SALT` (must be generated securely, e.g., via `openssl rand -hex 32`).
- `FILE_MANAGER_BEARER_TOKENS` (use strong, random secrets).
- `ENABLE_IMGPROXY_CACHE=true` (ensures Origin Shield is active).
- `PUBLIC_BASE_URL` and `IMGPROXY_BASE_URL` (should point to your actual production domain name).
