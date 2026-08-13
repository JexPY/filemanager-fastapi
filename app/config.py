import hashlib

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_STORAGE_BACKENDS = {"local", "s3", "gcp"}


def _derive_owner(secret: str) -> str:
    """Stable, non-reversible owner id for a bare (unlabeled) token, so audit
    records never store or expose the secret itself."""
    return "tok_" + hashlib.sha256(secret.encode()).hexdigest()[:12]


class Settings(BaseSettings):
    # Redis for TaskIQ
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # Postgres metadata store (system-of-record for every uploaded object).
    # Both the api and worker processes connect to this; the worker updates a
    # video's record on compression completion.
    DATABASE_URL: str = Field(default="")

    # Storage backend selection: "local" | "s3" | "gcp"
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

    # Imgproxy Config (must be hex encoded)
    IMGPROXY_KEY: str = Field(default="")
    IMGPROXY_SALT: str = Field(default="")
    # Scheme+host imgproxy is reachable at, prefixed onto the signed paths
    # returned to clients (e.g. http://localhost:9000/imgproxy). Without this, the
    # imgproxy_*_url response fields are just paths, not fetchable URLs.
    IMGPROXY_BASE_URL: str = Field(default="")
    # Whether NGINX should enable the proxy_cache_lock (origin shield) for imgproxy
    ENABLE_IMGPROXY_CACHE: str = Field(default="true")

    # The API's own externally-reachable origin (scheme+host, e.g.
    # https://media.example.com), used to build absolute share/download URLs in
    # responses. Blank -> those responses return relative paths and the client
    # prefixes its own origin.
    PUBLIC_BASE_URL: str = Field(default="")

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
    MAX_VIDEO_UPLOAD_BYTES: int = Field(default=2000 * 1024 * 1024)
    # Decompression-bomb guard: reject images decoding to more than this
    # many total pixels (width * height), before the full-resolution encode.
    MAX_IMAGE_PIXELS: int = Field(default=50_000_000)
    # QR codes have a hard capacity limit (~2953 bytes at version 40/level L);
    # this just rejects absurd input before it ever reaches segno.
    MAX_QR_CONTENT_LENGTH: int = Field(default=2000)

    # Kills a wedged ffmpeg process after this many seconds. Distinct from
    # VIDEO_MAX_DURATION_SECONDS below, which caps *output duration* -- this caps
    # *wall-clock processing time*.
    FFMPEG_TIMEOUT_SECONDS: int = Field(default=120)

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

    @model_validator(mode="after")
    def _validate_backend_requirements(self) -> Settings:
        # Fail fast at startup rather than surfacing a confusing error on first upload.
        if self.STORAGE_BACKEND == "s3" and not self.S3_BUCKET:
            raise ValueError("STORAGE_BACKEND='s3' requires S3_BUCKET to be set")
        if self.STORAGE_BACKEND == "gcp" and not self.GCS_BUCKET:
            raise ValueError("STORAGE_BACKEND='gcp' requires GCS_BUCKET to be set")
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

    @model_validator(mode="before")
    @classmethod
    def _set_default_database_url(cls, data: dict) -> dict:
        if isinstance(data, dict) and not data.get("DATABASE_URL"):
            data["DATABASE_URL"] = "postgresql://filemanager:filemanager@db:5432/filemanager"
        return data

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
    def webhook_allowed_hosts(self) -> frozenset[str]:
        """Lower-cased set of hostnames permitted as webhook callback targets."""
        return frozenset(
            h.strip().lower() for h in self.WEBHOOK_ALLOWED_HOSTS.split(",") if h.strip()
        )

    @property
    def webhooks_enabled(self) -> bool:
        """Callbacks require BOTH a signing secret and a non-empty host allow-list."""
        return bool(self.WEBHOOK_SIGNING_SECRET) and bool(self.webhook_allowed_hosts)


settings = Settings()
