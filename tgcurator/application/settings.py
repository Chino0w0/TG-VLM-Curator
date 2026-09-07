from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-only application configuration. Persisted configuration belongs in PostgreSQL."""

    app_name: str = "TG VLM Curator"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str | None = None
    celery_broker_url: SecretStr | None = None
    app_master_key: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TGCURATOR_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_production_secrets(self) -> Settings:
        if self.environment == "production" and self.app_master_key is None:
            raise ValueError("TGCURATOR_APP_MASTER_KEY is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
