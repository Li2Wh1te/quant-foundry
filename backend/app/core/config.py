from functools import lru_cache
from pathlib import Path
from datetime import date
from decimal import Decimal
import re
from typing import Literal

from pydantic import (
    Field,
    IPvAnyAddress,
    SecretStr,
    StrictInt,
    field_validator,
    model_validator,
)
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
    # Separate credential for the non-public Phase 2a endpoint.  Keeping it
    # distinct from the general API token prevents an ordinary API caller from
    # self-assigning operator/service capability with a request header.
    backtest_internal_token: SecretStr | None = Field(default=None, min_length=32)
    # Server-only HMAC secret for opaque result cursors.  Unlike the API
    # token, this value is never presented by clients, so a holder of the
    # API token cannot forge validly signed cursors.
    cursor_signing_key: SecretStr = Field(min_length=32)
    log_dir: Path = PROJECT_ROOT / "data" / "logs"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_retention_days: int = Field(default=30, ge=1, le=365)
    log_queue_size: int = Field(default=10_000, ge=100, le=100_000)
    # Both API and runner resolve this to the same host bind mount. The old
    # foundation settings are accepted only for existing .env compatibility.
    data_store_root: Path = PROJECT_ROOT / "data" / "current-store"
    # Explicit synthetic contract; cannot license the production E69 namespace.
    market_test_adjustment_contract: Literal['cumulative-exact-date-fixture@1'] | None = None
    market_test_post_anchor_date: date | None = None
    market_test_post_anchor_factor: str | None = Field(default=None,
        pattern=r'^(0|[1-9][0-9]*)(\.[0-9]+)?$', max_length=64)
    # Trusted D12 topology only. Never accepted from a RunConfig/strategy.
    s3_result_root: Path = PROJECT_ROOT / "data" / "s3-results"
    s3_work_root: Path | None = None
    s3_daemon_work_root: Path | None = None
    s3_worker_image: str | None = None
    s3_accept_new_runs: bool = True
    # Trusted service configuration, never a RunConfig/client quota override.
    # None preserves the existing worker/group policy.
    s3_worker_limits: dict[str, StrictInt] | None = None
    s3_group_budget: dict[str, StrictInt] | None = None
    foundation_worker_enabled: bool = False
    foundation_runtime_image_digest: str = ""
    foundation_archive_gid: int = Field(default=1000, ge=1)
    scheduler_enabled: bool = True
    scheduler_max_workers: int = Field(default=4, ge=1, le=64)
    scheduler_dispatch_interval_ms: int = Field(default=500, ge=100, le=10_000)
    scheduler_max_queued_runs: int = Field(default=1_000, ge=1, le=100_000)
    scheduler_misfire_grace_seconds: int = Field(default=60, ge=1, le=86_400)
    # Legacy input is consumed once during source initialization. All clients
    # subsequently read the encrypted database record, never these env values.
    tushare_token: SecretStr | None = None
    tushare_api_url: str = Field(default="http://api.tushare.pro", min_length=1)
    data_source_encryption_key: SecretStr | None = None
    ingestion_request_interval_ms: int = Field(default=1_000, ge=0, le=60_000)
    database_host: str = Field(default="127.0.0.1", min_length=1)
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str = Field(default="postgres", min_length=1)
    database_password: SecretStr
    database_name: str = Field(default="quant_foundry", min_length=1)

    @field_validator("data_source_encryption_key", mode="before")
    @classmethod
    def validate_source_key(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str) or re.fullmatch(r"[0-9a-fA-F]{64}", raw) is None:
            raise ValueError("data source encryption key must contain 64 hexadecimal characters")
        return value

    @model_validator(mode="after")
    def validate_market_test_contract(self):
        declared = self.market_test_adjustment_contract is not None
        has_date = self.market_test_post_anchor_date is not None
        has_factor = self.market_test_post_anchor_factor is not None
        if (declared and self.environment != 'test' or has_date != has_factor
                or (has_date or has_factor) and not declared):
            raise ValueError('market fixture contract is restricted to the test environment with a paired baseline')
        if has_factor and (Decimal(self.market_test_post_anchor_factor) <= 0
                or not date(1900, 1, 1) <= self.market_test_post_anchor_date <= date(9998, 12, 31)):
            raise ValueError('market fixture baseline must have a positive exact factor and bounded business date')
        return self

    @model_validator(mode="after")
    def validate_s3_budgets(self):
        from app.backtest_service.runtime_contracts import configured_budgets
        configured_budgets(self.s3_worker_limits, self.s3_group_budget)
        return self

    @field_validator("log_dir")
    @classmethod
    def resolve_log_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("data_store_root")
    @classmethod
    def resolve_data_store_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value
    @property
    def database_url(self) -> URL:
        unix_socket = self.database_host.startswith("/")
        return URL.create(
            "postgresql+psycopg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=None if unix_socket else self.database_host,
            port=None if unix_socket else self.database_port,
            database=self.database_name,
            # Keep the trusted Unix endpoint through Alembic's URL string
            # roundtrip; a slash in the URL authority loses its hostname.
            query={"host": self.database_host, "port": str(self.database_port)} if unix_socket else {},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
