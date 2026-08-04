from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_STORAGE_BACKENDS = {"local", "s3", "gcp"}


class Settings(BaseSettings):
    # Redis for TaskIQ
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # Storage backend selection: "local" | "s3" | "gcp"
    STORAGE_BACKEND: str = Field(default="local")
    LOCAL_STORAGE_DIR: str = Field(default="/data/media")
    PUBLIC_BASE_URL: str = Field(default="")

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

    # Imgproxy Config (must be hex encoded)
    IMGPROXY_KEY: str = Field(default="")
    IMGPROXY_SALT: str = Field(default="")

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

    # Kills a wedged ffmpeg process after this many seconds. Distinct from the
    # hardcoded `-t 60` in tasks.py, which caps *output duration* (silently
    # truncating long inputs) -- this caps *wall-clock processing time*.
    FFMPEG_TIMEOUT_SECONDS: int = Field(default=120)

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
        return self

    @property
    def valid_tokens(self) -> list[str]:
        if not self.FILE_MANAGER_BEARER_TOKENS:
            return []
        return [t.strip() for t in self.FILE_MANAGER_BEARER_TOKENS.split(",") if t.strip()]


settings = Settings()
