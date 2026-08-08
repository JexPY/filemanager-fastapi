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
    DATABASE_URL: str = Field(default="postgresql://filemanager:filemanager@db:5432/filemanager")

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
    # returned to clients (e.g. http://localhost:8080). Without this, the
    # imgproxy_*_url response fields are just paths, not fetchable URLs.
    IMGPROXY_BASE_URL: str = Field(default="")

    # Auth
    FILE_MANAGER_BEARER_TOKENS: str = Field(default="")

    # Upload limits (bytes)
    MAX_IMAGE_UPLOAD_BYTES: int = Field(default=25 * 1024 * 1024)
    MAX_VIDEO_UPLOAD_BYTES: int = Field(default=500 * 1024 * 1024)
    # Decompression-bomb guard: reject images decoding to more than this
    # many total pixels (width * height), before the full-resolution encode.
    MAX_IMAGE_PIXELS: int = Field(default=40_000_000)
    # QR codes have a hard capacity limit (~2953 bytes at version 40/level L);
    # this just rejects absurd input before it ever reaches segno.
    MAX_QR_CONTENT_LENGTH: int = Field(default=2000)

    # Kills a wedged ffmpeg process after this many seconds. Distinct from
    # VIDEO_MAX_DURATION_SECONDS below, which caps *output duration* -- this caps
    # *wall-clock processing time*.
    FFMPEG_TIMEOUT_SECONDS: int = Field(default=120)

    # Caps compressed *output* duration (ffmpeg `-t`). An input longer than this
    # is truncated; the worker ffprobes the input and reports `truncated`/
    # `duration_seconds` in the task result and on the uploads row, so the caller
    # is told rather than silently losing footage. Distinct from
    # FFMPEG_TIMEOUT_SECONDS (wall-clock kill).
    VIDEO_MAX_DURATION_SECONDS: int = Field(default=60)

    # How long a "this task id was actually issued" marker lives in Redis, so
    # GET /tasks/{id} can 404 a bogus/typo'd id instead of reporting "pending"
    # forever. Generous enough to cover realistic queue backlog + processing.
    TASK_STATUS_TTL_SECONDS: int = Field(default=3600)

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


settings = Settings()
