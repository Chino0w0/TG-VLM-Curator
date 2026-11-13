from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration loaded from environment variables and secret files."""

    app_name: str = "TG VLM Curator"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_DATABASE_URL", "DATABASE_URL"),
    )
    postgres_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_POSTGRES_USER", "POSTGRES_USER"),
    )
    postgres_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
    )
    postgres_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_POSTGRES_HOST", "POSTGRES_HOST"),
    )
    postgres_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("TGCURATOR_POSTGRES_PORT", "POSTGRES_PORT"),
    )
    postgres_db: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_POSTGRES_DB", "POSTGRES_DB"),
    )
    celery_broker_url: SecretStr | None = None
    app_master_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_APP_MASTER_KEY", "APP_MASTER_KEY"),
    )
    app_master_key_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TGCURATOR_APP_MASTER_KEY_FILE",
            "APP_MASTER_KEY_FILE",
        ),
    )
    app_master_key_id: str = "primary"
    telegram_api_id: int | None = Field(default=None, gt=0)
    telegram_api_hash: SecretStr | None = None

    @field_validator("telegram_api_id", "telegram_api_hash", mode="before")
    @classmethod
    def normalize_optional_telegram_credentials(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    archive_root: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_ARCHIVE_ROOT", "ARCHIVE_ROOT"),
    )
    temp_media_dir: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("TGCURATOR_TEMP_MEDIA_DIR", "TEMP_MEDIA_DIR"),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TGCURATOR_",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @field_validator("app_name", "app_master_key_id")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url", "postgres_user", "postgres_host", "postgres_db")
    @classmethod
    def normalize_database_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("postgres_password", mode="before")
    @classmethod
    def normalize_optional_postgres_password(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("postgres_port", mode="before")
    @classmethod
    def normalize_postgres_port(cls, value: object) -> object:
        return 5432 if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def load_master_key_file(self) -> Self:
        if (
            self.database_url is None
            and self.postgres_user is not None
            and self.postgres_password is not None
            and self.postgres_db is not None
        ):
            host = self.postgres_host or "localhost"
            formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            encoded_user = quote(self.postgres_user, safe="")
            encoded_password = quote(self.postgres_password.get_secret_value(), safe="")
            encoded_db = quote(self.postgres_db, safe="")
            self.database_url = (
                f"postgresql+asyncpg://{encoded_user}:{encoded_password}"
                f"@{formatted_host}:{self.postgres_port}/{encoded_db}"
            )
        if self.app_master_key is not None and self.app_master_key_file is not None:
            raise ValueError("configure APP_MASTER_KEY or APP_MASTER_KEY_FILE, not both")
        if self.app_master_key_file is not None:
            path = self.app_master_key_file.expanduser()
            if not path.is_file():
                raise ValueError("APP_MASTER_KEY_FILE must reference a readable file")
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ValueError("APP_MASTER_KEY_FILE could not be read") from error
            if not value:
                raise ValueError("APP_MASTER_KEY_FILE must not be empty")
            self.app_master_key = SecretStr(value)
        if self.environment == "production" and self.app_master_key is None:
            raise ValueError("APP_MASTER_KEY or APP_MASTER_KEY_FILE is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
