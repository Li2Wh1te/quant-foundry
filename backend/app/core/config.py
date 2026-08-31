from functools import lru_cache
from pathlib import Path
import re
from typing import Literal

from pydantic import (
    Field,
    IPvAnyAddress,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Configuration values are ultimately supplied by environment variables, but
# callers also construct ``Settings`` directly in tests and in command-line
# entry points.  Keep one strict parser for both paths: booleans and lossy
# numeric coercions (for example ``1.5 -> 1``) are not valid configuration.
# The 64-bit ceiling gives overflow a deterministic meaning while avoiding
# undocumented product-specific upper limits for durations and byte counts.
_MAX_BACKTEST_INTEGER = 2**63 - 1
_BACKTEST_INTEGER_FIELDS = (
    "backtest_max_workers",
    "backtest_max_queued_runs",
    "backtest_internal_max_queued_runs",
    "backtest_run_timeout_seconds",
    "backtest_cancel_grace_seconds",
    "backtest_stdout_max_bytes",
    "backtest_memory_limit_mib",
    "backtest_heartbeat_max_interval_seconds",
    "backtest_lost_heartbeat_seconds",
    "backtest_progress_persist_interval_seconds",
)


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
    # Server-only HMAC secret for opaque result cursors.  Unlike the API
    # token, this value is never presented by clients, so a holder of the
    # API token cannot forge validly signed cursors.
    cursor_signing_key: SecretStr = Field(min_length=32)
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
    backtest_max_workers: int = Field(default=1, ge=1)
    backtest_max_queued_runs: int = Field(default=32, ge=1)
    backtest_internal_max_queued_runs: int | None = Field(default=None, ge=1, lt=32)
    backtest_run_timeout_seconds: int = Field(default=7_200, ge=1)
    backtest_cancel_grace_seconds: int = Field(default=10, ge=1)
    backtest_stdout_max_bytes: int = Field(default=1_048_576, ge=1)
    backtest_memory_limit_mib: int = Field(default=1_024, ge=1)
    backtest_heartbeat_max_interval_seconds: int = Field(default=15, ge=1)
    backtest_lost_heartbeat_seconds: int = Field(default=60, ge=1)
    backtest_progress_persist_interval_seconds: int = Field(default=5, ge=1)
    tushare_token: SecretStr | None = None
    tushare_api_url: str = Field(default="http://api.tushare.pro", min_length=1)
    ingestion_request_interval_ms: int = Field(default=1_000, ge=0, le=60_000)
    database_host: str = Field(default="127.0.0.1", min_length=1)
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str = Field(default="postgres", min_length=1)
    database_password: SecretStr
    database_name: str = Field(default="quant_foundry", min_length=1)

    @field_validator("log_dir")
    @classmethod
    def resolve_log_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator(*_BACKTEST_INTEGER_FIELDS, mode="before")
    @classmethod
    def parse_backtest_integer(
        cls, value: object, info: ValidationInfo
    ) -> object:
        """Parse only exact integer values for backtest controls.

        ``BaseSettings`` exposes environment variables as strings.  Parsing
        those strings here keeps normal values such as ``"15"`` usable while
        rejecting booleans, decimal strings, empty text, and values outside
        the signed 64-bit range.  The optional internal queue deliberately
        accepts ``None`` as its disabled state; an empty environment value is
        still rejected instead of being mistaken for that state.
        """

        if value is None:
            if info.field_name == "backtest_internal_max_queued_runs":
                return None
            return value
        if isinstance(value, bool):
            raise ValueError("backtest configuration values must be integers")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            text = value.strip()
            if not text or re.fullmatch(r"[+-]?\d+", text) is None:
                raise ValueError("backtest configuration values must be integers")
            parsed = int(text, 10)
        else:
            raise ValueError("backtest configuration values must be integers")

        if abs(parsed) > _MAX_BACKTEST_INTEGER:
            raise ValueError("backtest configuration value is out of range")
        return parsed

    @model_validator(mode="after")
    def validate_backtest_relationships(self) -> "Settings":
        """Enforce safety relationships shared by API and runner settings."""

        if (
            self.backtest_lost_heartbeat_seconds
            < 3 * self.backtest_heartbeat_max_interval_seconds
        ):
            raise ValueError(
                "backtest_lost_heartbeat_seconds must be at least three times "
                "backtest_heartbeat_max_interval_seconds"
            )
        if (
            self.backtest_progress_persist_interval_seconds
            > self.backtest_heartbeat_max_interval_seconds
        ):
            raise ValueError(
                "backtest_progress_persist_interval_seconds must not exceed "
                "backtest_heartbeat_max_interval_seconds"
            )
        if (
            self.backtest_cancel_grace_seconds
            >= self.backtest_run_timeout_seconds
        ):
            raise ValueError(
                "backtest_cancel_grace_seconds must be less than "
                "backtest_run_timeout_seconds"
            )
        internal_limit = self.backtest_internal_max_queued_runs
        if internal_limit is not None:
            if internal_limit >= self.backtest_max_queued_runs:
                raise ValueError(
                    "backtest_internal_max_queued_runs must be less than "
                    "backtest_max_queued_runs"
                )
        return self

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
