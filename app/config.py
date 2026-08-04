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
        return self

    @property
    def valid_tokens(self) -> list[str]:
        if not self.FILE_MANAGER_BEARER_TOKENS:
            return []
        return [t.strip() for t in self.FILE_MANAGER_BEARER_TOKENS.split(",") if t.strip()]


settings = Settings()
