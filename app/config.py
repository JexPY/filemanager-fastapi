import hashlib
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_STORAGE_BACKENDS = {"local", "s3", "gcp", "b2"}

# Which setting holds the "a client can fetch this object directly" base URL, per
# backend. Local development uses LOCAL_PUBLIC_BASE_URL (served directly by nginx
# for public asset prefixes while denying private/ and raw/). Adding a future object
# store means adding one line here rather than a third arm to two separate if/elif
# chains (this map backs both `public_images_unservable` below and
# `storage.has_public_base_url()`).
PUBLIC_BASE_URL_FIELDS = {
    "local": "LOCAL_PUBLIC_BASE_URL",
    "s3": "S3_PUBLIC_BASE_URL",
    "gcp": "GCS_PUBLIC_BASE_URL",
    "b2": "B2_PUBLIC_BASE_URL",
}

# Settings that must be non-empty for a given STORAGE_BACKEND, checked at import
# so a misconfiguration kills the process instead of 502-ing the first upload.
# `b2` lists its credentials as well as its bucket because B2 has no ambient
# credential chain to fall back on -- see the B2_* field comments below.
REQUIRED_BACKEND_FIELDS: dict[str, tuple[str, ...]] = {
    "s3": ("S3_BUCKET",),
    "gcp": ("GCS_BUCKET",),
    "b2": ("B2_BUCKET", "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_REGION"),
}


def _derive_owner(secret: str) -> str:
    """Stable, non-reversible owner id for a bare (unlabeled) token, so audit
    records never store or expose the secret itself."""
    return "tok_" + hashlib.sha256(secret.encode()).hexdigest()[:12]


class Settings(BaseSettings):
    # Redis for TaskIQ
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # --- TaskIQ retention: both of these bound Redis memory. ---
    #
    # taskiq-redis leaves both unbounded by default, and *neither* default key
    # carries a TTL. That matters especially when Redis is shared with another
    # service under `maxmemory-policy volatile-lru`: that policy can only evict
    # keys that have an expiry, so these would grow until the instance refuses
    # writes — taking the co-tenant's cache down with it, not just this service.
    #
    # Result payloads. Without an expiry `RedisAsyncResultBackend` issues a plain
    # SET (see its `result_ex_time` branch), so every task result is kept forever.
    # A compression finishes in minutes, so a week is far longer than any client
    # will poll `GET /tasks/{task_id}` for.
    TASKIQ_RESULT_TTL_SECONDS: int = Field(default=7 * 24 * 3600)
    #
    # Stream length. `XACK` only clears the pending-entries list — it does NOT
    # delete the entry — and taskiq never issues `XDEL`, so without `maxlen` the
    # stream grows by every job ever processed.
    #
    # This is a trim, so it must stay comfortably above the worst-case *pending*
    # backlog: entries beyond it are dropped, and a dropped entry is a lost job.
    # 10k queued videos would already be a much larger incident, but raise this if
    # your backlog can legitimately exceed it.
    TASKIQ_STREAM_MAXLEN: int = Field(default=10_000)

    # Postgres metadata store (system-of-record for every uploaded object).
    # Both the api and worker processes connect to this; the worker updates a
    # video's record on compression completion.
    DATABASE_URL: str = Field(default="")

    # Storage backend selection: "local" | "s3" | "gcp" | "b2"
    STORAGE_BACKEND: str = Field(default="local")
    LOCAL_STORAGE_DIR: str = Field(default="/data/media")
    # Base URL prepended to object keys returned by the local backend.
    LOCAL_PUBLIC_BASE_URL: str = Field(default="")

    # Storage S3/R2
    S3_BUCKET: str = Field(default="")
    S3_ENDPOINT_URL: str = Field(default="")
    # Optional CDN / custom domain in front of the bucket (e.g. CloudFront).
    S3_PUBLIC_BASE_URL: str = Field(default="")
    AWS_REGION: str = Field(default="")
    # Blank => fall back to boto's default credential chain (IAM roles, env, ...).
    AWS_ACCESS_KEY_ID: str = Field(default="")
    AWS_SECRET_ACCESS_KEY: str = Field(default="")

    # Storage GCP (optional if using S3)
    GCP_PROJECT: str | None = Field(default=None)
    GCP_SERVICE_ACCOUNT_FILE: str | None = Field(default=None)
    GCS_BUCKET: str = Field(default="")
    # Optional CDN / custom domain in front of the bucket. Independent of
    # LOCAL_PUBLIC_BASE_URL -- these used to be the same field, so switching
    # STORAGE_BACKEND from local to gcp without also touching env vars would
    # silently reuse whatever base URL had been set for local dev.
    GCS_PUBLIC_BASE_URL: str = Field(default="")

    # Storage Backblaze B2 (used when STORAGE_BACKEND=b2). Reached through B2's
    # S3-compatible API, so the wire protocol is SigV4 exactly like S3 -- but the
    # credentials live in their own namespace rather than sharing AWS_*, so
    # switching STORAGE_BACKEND cannot silently reuse the other store's keys.
    B2_BUCKET: str = Field(default="")
    # From the B2 console's "Application Keys": keyID -> access key id,
    # applicationKey -> secret. Both REQUIRED for this backend: B2 has no
    # equivalent of boto's ambient credential chain, so a blank value is a
    # misconfiguration, not "resolve it from the environment".
    B2_KEY_ID: str = Field(default="")
    B2_APPLICATION_KEY: str = Field(default="")
    # e.g. "us-west-004" -- shown in the console as part of the bucket's S3
    # endpoint. Required: it is both how the endpoint below is derived and what
    # SigV4 binds into the credential scope, so a wrong value fails to sign.
    B2_REGION: str = Field(default="")
    # Optional explicit override; derived as https://s3.{B2_REGION}.backblazeb2.com
    # when blank. Set it only to point at something else (a proxy, a test double).
    B2_ENDPOINT_URL: str = Field(default="")
    # Optional CDN / custom domain in front of the bucket (fronting B2 with a CDN
    # is the usual shape, since B2's own egress to one is free).
    B2_PUBLIC_BASE_URL: str = Field(default="")

    # Imgproxy Config (must be hex encoded)
    IMGPROXY_KEY: str = Field(default="")
    IMGPROXY_SALT: str = Field(default="")
    # Scheme+host imgproxy is reachable at, prefixed onto the signed paths
    # returned to clients (e.g. http://localhost:9000/imgproxy). Without this, the
    # imgproxy_*_url response fields are just paths, not fetchable URLs.
    IMGPROXY_BASE_URL: str = Field(default="")
    # Whether NGINX should enable the proxy_cache_lock (origin shield) for imgproxy
    ENABLE_IMGPROXY_CACHE: str = Field(default="true")

    # Image rendition generation mode: "materialize" (default) or "on_demand".
    # materialize: extra widths are encoded at upload and stored for direct CDN delivery.
    # on_demand: widths are produced on-demand by imgproxy at request time.
    IMAGE_RENDITION_MODE: Literal["materialize", "on_demand"] = "materialize"

    # The API's own externally-reachable origin (scheme+host, e.g.
    # https://media.example.com), used to build absolute share/download URLs in
    # responses. Blank -> those responses return relative paths and the client
    # prefixes its own origin.
    PUBLIC_BASE_URL: str = Field(default="")

    # Browser origins allowed to call this service cross-origin, comma-separated
    # (e.g. "https://fixcar.ge,https://www.fixcar.ge"). Required for the
    # direct-to-service upload pattern: a consuming site's JavaScript POSTs the
    # file here and must be able to *read the response* to learn the record id.
    # Note the failure mode this exists to prevent -- a multipart POST carrying
    # its credential as `?token=` is a CORS-*simple* request, so without this the
    # upload still succeeds and stores the object, but the browser blocks the
    # response and the caller never learns the id: a silent orphan on every
    # upload. Blank => no CORS middleware at all (pure backend-to-backend
    # deployments are unaffected). No wildcard support on purpose.
    CORS_ALLOWED_ORIGINS: str = Field(default="")

    # Auth
    FILE_MANAGER_BEARER_TOKENS: str = Field(default="")

    # --- JWT capability tokens (optional) --------------------------------
    # Shared secret for HS256 "capability" JWTs. Lets a trusted backend (or this
    # service's own POST /upload/presign) mint short-lived, scoped upload tokens
    # that an untrusted frontend can send *directly* to this service, so the
    # bytes never round-trip the backend. Blank => JWT auth is disabled and only
    # the static FILE_MANAGER_BEARER_TOKENS are accepted (backward compatible).
    # Independent of WEBHOOK_SIGNING_SECRET / IMGPROXY_KEY -- do not reuse those.
    JWT_SECRET_KEY: str = Field(default="")
    # Signing algorithm for the above (any HMAC alg PyJWT supports). HS256 by
    # default; the shared-secret model here does not use asymmetric keys.
    JWT_ALGORITHM: str = Field(default="HS256")

    # Upload limits (bytes)
    MAX_IMAGE_UPLOAD_BYTES: int = Field(default=25 * 1024 * 1024)
    MAX_BULK_UPLOAD_TOTAL_BYTES: int = Field(default=50 * 1024 * 1024)
    MAX_VIDEO_UPLOAD_BYTES: int = Field(default=2000 * 1024 * 1024)
    MAX_FILE_UPLOAD_BYTES: int = Field(default=100 * 1024 * 1024)
    # Decompression-bomb guard: reject images decoding to more than this
    # many total pixels (width * height), before the full-resolution encode.
    MAX_IMAGE_PIXELS: int = Field(default=50_000_000)
    # `optimization=lossless` guards. Lossless cost tracks content entropy, NOT
    # pixel count -- measured, a 14.7MP flat-colour screenshot encodes in 262ms
    # to 5KB while a 12.2MP photograph takes 5563ms and produces 6.1MB. A pixel
    # cap would therefore block the intended use and permit the abusive one, so
    # the guard is a cheap entropy probe instead: lossless-encode a 512px
    # thumbnail and measure bytes-per-pixel. Screenshots land at 0.012-0.018,
    # photographs at 0.66-1.27, so anything in 0.05-0.3 separates them with a
    # wide margin. The probe costs ~32ms and avoids a ~5.5s encode.
    LOSSLESS_MAX_PROBE_BYTES_PER_PIXEL: float = Field(default=0.25)
    # Backstop for content the probe misjudges: reject a lossless result whose
    # encoded size exceeds this. Bounds stored-object size even when the CPU
    # has already been spent.
    LOSSLESS_MAX_OUTPUT_BYTES: int = Field(default=8 * 1024 * 1024)
    # QR codes have a hard capacity limit (~2953 bytes at version 40/level L);
    # this just rejects absurd input before it ever reaches segno.
    MAX_QR_CONTENT_LENGTH: int = Field(default=2000)
    # File size cap for optional logo overlay uploads (bytes)
    MAX_QR_LOGO_BYTES: int = Field(default=5 * 1024 * 1024)

    # Kills a wedged ffmpeg process after this many seconds. Distinct from
    # VIDEO_MAX_DURATION_SECONDS below, which caps *output duration* -- this caps
    # *wall-clock processing time*.
    FFMPEG_TIMEOUT_SECONDS: int = Field(default=120)

    # Timeout for the ffprobe metadata probe step. Much shorter than FFMPEG_TIMEOUT_SECONDS
    # since probing is fast; a hung probe is a bad input, not a slow encode.
    FFPROBE_TIMEOUT_SECONDS: int = Field(default=15)

    # TTL (seconds) for the presigned GET URL the worker hands ffmpeg as its
    # input on the s3/gcp backends: the worker reads the object in place (over
    # HTTPS, with range requests) instead of downloading it into RAM first. Must
    # comfortably exceed FFMPEG_TIMEOUT_SECONDS since a single ffmpeg run may keep
    # reading across the whole window. Ignored on `local` (ffmpeg reads the shared
    # media volume directly, no URL involved).
    FFMPEG_INPUT_URL_TTL_SECONDS: int = Field(default=3600)

    # --- Video playback -------------------------------------------------
    # TTL (seconds) for the freshly-minted signed GET URL the /files/{id}/download
    # endpoint 302s to on the s3/gcp backends. Size it to a generous viewing
    # session: the player caches the *resolved* signed URL, so a seek after this
    # elapses can hit an expired signature (see readme "Playback & visibility").
    # Default 6h. GCS clamps its own V4 cap at 7 days.
    VIDEO_PLAYBACK_URL_TTL_SECONDS: int = Field(default=21600)
    # How the `local` backend serves video bytes: `xaccel` (prod -- return an
    # X-Accel-Redirect that nginx serves with sendfile + native Range, app out of
    # the byte path) or `direct` (dev without nginx -- Starlette FileResponse,
    # which also honours Range for on-disk files).
    LOCAL_MEDIA_SERVE_MODE: str = Field(default="xaccel")

    # How PRIVATE media is served on the object-store backends (s3/gcp). `local`
    # is unaffected -- it always streams through nginx's internal location.
    #
    #   stream   -> nginx proxies the bytes from an internal location the client
    #               cannot address. No signed URL ever reaches the client, so
    #               there is no leakage window, no TTL to size, and no expiry to
    #               break a seek. Costs bandwidth through this host.
    #   redirect -> 302 to a short-lived signed URL (the pre-existing behaviour).
    #               Keeps bytes off this host, but the URL is a bearer token for
    #               its lifetime and a seek after it expires fails.
    #
    # `stream` is the default because it is the only hole-free option; `redirect`
    # exists for high-volume private media where the bandwidth actually matters.
    PRIVATE_MEDIA_SERVE_MODE: str = Field(default="stream")

    # Caps compressed *output* duration (ffmpeg `-t`). An input longer than this
    # is truncated; the worker ffprobes the input and reports `truncated`/
    # `duration_seconds` in the task result and on the uploads row, so the caller
    # is told rather than silently losing footage. Distinct from
    # FFMPEG_TIMEOUT_SECONDS (wall-clock kill).
    VIDEO_MAX_DURATION_SECONDS: int = Field(default=60)

    # --- Webhooks (video-completion push delivery) -----------------------
    # Callbacks are OFF unless BOTH a signing secret and an allow-list of target
    # hosts are set. A client passes `callback_url` on POST /upload/video; the
    # worker POSTs a signed payload there when compression finishes.
    #
    # HMAC-SHA256 secret the worker signs each delivery with (X-Webhook-Signature
    # header). Receivers recompute it to verify authenticity. No default -- a
    # blank value disables webhooks.
    WEBHOOK_SIGNING_SECRET: str = Field(default="")
    # Comma-separated exact hostnames permitted as callback targets (SSRF egress
    # control). Blank disables webhooks. A callback_url whose host isn't listed is
    # rejected at upload time with 400.
    WEBHOOK_ALLOWED_HOSTS: str = Field(default="")
    # Permit http:// callbacks. Default https-only.
    WEBHOOK_ALLOW_INSECURE_HTTP: bool = Field(default=False)
    # Permit callback hosts that resolve to private/loopback/link-local IPs. Off
    # by default (SSRF guard); turn on only for in-network receivers in dev/test.
    WEBHOOK_ALLOW_PRIVATE_IPS: bool = Field(default=False)
    # Per-attempt HTTP timeout, total delivery attempts, and the exponential
    # backoff base between attempts (delay = base * 2**(attempt-1), capped).
    WEBHOOK_TIMEOUT_SECONDS: float = Field(default=10.0)
    WEBHOOK_MAX_ATTEMPTS: int = Field(default=4)
    WEBHOOK_RETRY_BACKOFF_SECONDS: float = Field(default=1.0)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("STORAGE_BACKEND")
    @classmethod
    def _validate_storage_backend(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ALLOWED_STORAGE_BACKENDS:
            raise ValueError(
                f"STORAGE_BACKEND must be one of {sorted(ALLOWED_STORAGE_BACKENDS)}, got {v!r}"
            )
        return normalized

    @field_validator("LOCAL_MEDIA_SERVE_MODE")
    @classmethod
    def _validate_local_media_serve_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"xaccel", "direct"}:
            raise ValueError(f"LOCAL_MEDIA_SERVE_MODE must be 'xaccel' or 'direct', got {v!r}")
        return normalized

    @field_validator("PRIVATE_MEDIA_SERVE_MODE")
    @classmethod
    def _validate_private_media_serve_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"stream", "redirect"}:
            raise ValueError(f"PRIVATE_MEDIA_SERVE_MODE must be 'stream' or 'redirect', got {v!r}")
        return normalized

    @field_validator("IMAGE_RENDITION_MODE")
    @classmethod
    def _validate_image_rendition_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"materialize", "on_demand"}:
            raise ValueError(
                f"IMAGE_RENDITION_MODE must be 'materialize' or 'on_demand', got {v!r}"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_backend_requirements(self) -> Settings:
        # Fail fast at startup rather than surfacing a confusing error on first upload.
        for field_name in REQUIRED_BACKEND_FIELDS.get(self.STORAGE_BACKEND, ()):
            if not getattr(self, field_name):
                raise ValueError(
                    f"STORAGE_BACKEND={self.STORAGE_BACKEND!r} requires {field_name} to be set"
                )
        if not self.valid_tokens:
            # Otherwise the service boots successfully and then silently 401s
            # every single request forever -- fail at startup instead.
            raise ValueError("FILE_MANAGER_BEARER_TOKENS must be set to at least one token")
        # Every /upload/image call signs a URL with these; a missing/invalid
        # value previously only surfaced as a runtime ValueError on the first
        # such call, *after* the file had already been written to storage.
        for field_name in ("IMGPROXY_KEY", "IMGPROXY_SALT"):
            value = getattr(self, field_name)
            if not value:
                raise ValueError(f"{field_name} must be set (hex-encoded)")
            try:
                bytes.fromhex(value)
            except ValueError as exc:
                raise ValueError(f"{field_name} must be valid hex, got {value!r}") from exc
        return self

    @property
    def token_identities(self) -> dict[str, str]:
        """Maps each accepted bearer secret to its owner identity.

        Entries in FILE_MANAGER_BEARER_TOKENS are comma-separated and may be
        either a bare `secret` (owner derived as tok_<hash>, backward compatible
        with the old token-list format) or `label:secret` (owner = label, for a
        human-readable audit trail). The client always sends just `secret` as
        the bearer token; the label is server-side only. A secret that itself
        contains a colon must therefore be given an explicit label.
        """
        identities: dict[str, str] = {}
        if not self.FILE_MANAGER_BEARER_TOKENS:
            return identities
        for raw in self.FILE_MANAGER_BEARER_TOKENS.split(","):
            entry = raw.strip()
            if not entry:
                continue
            if ":" in entry:
                label, secret = (part.strip() for part in entry.split(":", 1))
            else:
                label, secret = "", entry
            if not secret:
                continue
            identities[secret] = label or _derive_owner(secret)
        return identities

    @property
    def valid_tokens(self) -> list[str]:
        return list(self.token_identities)

    @property
    def parsed_webhook_allowed_hosts(self) -> frozenset[str]:
        """Lower-cased set of hostnames permitted as webhook callback targets."""
        return frozenset(
            h.strip().lower() for h in self.WEBHOOK_ALLOWED_HOSTS.split(",") if h.strip()
        )

    @property
    def parsed_cors_origins(self) -> list[str]:
        """Browser origins permitted to call this service cross-origin.

        A list rather than a set: Starlette's CORSMiddleware stores what it is
        given and matches an incoming Origin against it, so a stable, ordered
        value keeps the configuration reproducible. Origins are compared
        verbatim by the browser and by Starlette, so a trailing slash is
        stripped but case is otherwise left alone.
        """
        return [o.strip().rstrip("/") for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def active_public_base_url(self) -> str:
        """The active backend's directly-fetchable base URL, or "" when it has none.

        The single source of truth for "can a client fetch this object without
        going through us" -- `storage.has_public_base_url()` is just this, coerced
        to bool.
        """
        field_name = PUBLIC_BASE_URL_FIELDS.get(self.STORAGE_BACKEND)
        return getattr(self, field_name) if field_name else ""

    @property
    def public_images_unservable(self) -> bool:
        """True when imgproxy has no fetchable address for a *public* record.

        imgproxy resolves an image's source by URL, so on the object stores it
        needs a public bucket or CDN domain (`*_PUBLIC_BASE_URL`). Without one it
        falls back to the raw endpoint URL, which a private bucket answers with a
        403 -- a broken thumbnail, after the upload already succeeded.

        Deliberately a startup *warning* rather than a hard failure, unlike the
        other checks here. A deployment that only ever stores `private` media is
        perfectly valid without a public base URL, and requiring one would make
        the documented Garage s3-dev flow unbootable -- Garage has no anonymous
        access at all, so no public base URL can exist for it.

        For `local`, imgproxy reads via `local://` mount, so this is never flagged
        unservable.
        """
        if self.STORAGE_BACKEND == "local" or self.STORAGE_BACKEND not in PUBLIC_BASE_URL_FIELDS:
            return False
        return not self.active_public_base_url

    @property
    def webhooks_enabled(self) -> bool:
        """Callbacks require BOTH a signing secret and a non-empty host allow-list."""
        return bool(self.WEBHOOK_SIGNING_SECRET) and bool(self.parsed_webhook_allowed_hosts)


settings = Settings()
