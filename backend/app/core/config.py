from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, IPvAnyAddress, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="QF_",
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    app_name: str = "Quant Foundry API"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False
    server_host: IPvAnyAddress = "127.0.0.1"
    server_port: int = Field(default=8000, ge=1, le=65535)
    api_token: SecretStr = Field(min_length=32)
    log_dir: Path = PROJECT_ROOT / "data" / "logs"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_retention_days: int = Field(default=30, ge=1, le=365)
    log_queue_size: int = Field(default=10_000, ge=100, le=100_000)
    log_query_max_files: int = Field(default=32, ge=1, le=366)
    scheduler_enabled: bool = True
    scheduler_max_workers: int = Field(default=4, ge=1, le=64)
    scheduler_dispatch_interval_ms: int = Field(default=500, ge=100, le=10_000)
    scheduler_max_queued_runs: int = Field(default=1_000, ge=1, le=100_000)
    scheduler_misfire_grace_seconds: int = Field(default=60, ge=1, le=86_400)
    database_host: str = Field(default="127.0.0.1", min_length=1)
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str = Field(default="postgres", min_length=1)
    database_password: SecretStr
    database_name: str = Field(default="quant_foundry", min_length=1)

    @field_validator("log_dir")
    @classmethod
    def resolve_log_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
