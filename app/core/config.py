from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, IPvAnyAddress, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/quant_foundry"
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+psycopg":
            raise ValueError("database_url must use the postgresql+psycopg scheme")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
