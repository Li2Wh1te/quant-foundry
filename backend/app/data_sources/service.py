"""Database-owned source settings, optimistic saves, and scheduling gates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
import structlog

from app.core.config import Settings
from app.data_sources.credentials import cipher, decrypt, encrypt
from app.data_sources.models import DataSourceConfig
from app.data_sources.providers import PROVIDERS, SourceError, require_provider
from app.scheduling.models import ScheduledTask, TaskRun
if TYPE_CHECKING:
    from app.scheduling.registry import TaskRegistry

logger = structlog.get_logger(__name__)


def require_config(session: Session, key: str, *, lock=False) -> DataSourceConfig:
    statement = select(DataSourceConfig).where(DataSourceConfig.key == key)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(statement)
    if row is None:
        raise SourceError("数据源配置尚未初始化，请完成数据库迁移并重启服务。", status_code=503)
    return row


def configured(row: DataSourceConfig | None) -> bool:
    return row is not None and row.initialized and bool(row.encrypted_secrets)


def initialize_sources(session: Session, settings: Settings) -> None:
    """Import legacy values exactly once; never use them as runtime fallback."""
    for key, provider in PROVIDERS.items():
        row = require_config(session, key, lock=True)
        if row.initialized:
            # Fail closed on key loss instead of silently starting jobs that
            # cannot read their credentials. Existing ciphertext is preserved.
            decrypt(settings, key, row.encrypted_secrets)
            continue
        row.values = {field["key"]: field["default"] for field in provider.fields if "default" in field}
        if key == "tushare":
            token = settings.tushare_token
            if token is not None and token.get_secret_value().strip():
                values, secrets = provider.validate({"api_url": settings.tushare_api_url,
                    "token": token.get_secret_value()}, {})
                row.values = values
                row.encrypted_secrets = encrypt(settings, key, secrets)
            else:
                # Preserve a legacy proxy address even when its token was not
                # set. A later page save still validates the complete config.
                row.values, _ = provider.validate({"api_url": settings.tushare_api_url,
                                                   "token": "validation-only"}, {})
        row.initialized = True
        row.updated_at = datetime.now(UTC)
    session.commit()


def runtime_credentials(session: Session, settings: Settings, key: str) -> tuple[dict, dict]:
    row = require_config(session, key)
    if not configured(row):
        raise SourceError("请先在数据源页面完成连接配置。", status_code=409)
    # Already claimed runs are allowed to finish after source disable. New
    # execution admission is enforced atomically by the scheduler gate.
    return dict(row.values), decrypt(settings, key, row.encrypted_secrets)


def lock_source_gates(session: Session, registry: TaskRegistry) -> dict[str, DataSourceConfig]:
    # Enqueue, claim, and enable/disable always lock source rows before tasks
    # or runs. Sorted keys give the same ordering when more providers arrive.
    keys = sorted({item.source_key for item in registry.list() if item.source_key})
    return {key: require_config(session, key, lock=True) for key in keys}


def skip_source_queue(session: Session, task_types: list[str], reason: str) -> int:
    if not task_types:
        return 0
    result = session.execute(update(TaskRun).where(
        TaskRun.task_type.in_(task_types), TaskRun.status == "queued"
    ).values(status="skipped", error_type="DataSourceUnavailable",
             error_message=reason, finished_at=datetime.now(UTC)))
    return result.rowcount or 0


class DataSourceService:
    def __init__(self, session: Session, settings: Settings, registry: TaskRegistry):
        self.session, self.settings, self.registry = session, settings, registry

    def detail(self, key: str) -> dict:
        provider = require_provider(key)
        row = require_config(self.session, key)
        secrets = decrypt(self.settings, key, row.encrypted_secrets)
        definitions = [item for item in self.registry.list() if item.source_key == key]
        keys = [item.key for item in definitions]
        counts = dict(self.session.execute(select(ScheduledTask.task_type, func.count()).where(
            ScheduledTask.task_type.in_(keys), ScheduledTask.state != "archived"
        ).group_by(ScheduledTask.task_type)).all())
        # Rank by registration type, not by individual task: the page lists
        # capabilities and each capability can have multiple task instances.
        ranked = select(TaskRun.id, func.row_number().over(partition_by=TaskRun.task_type,
            order_by=(TaskRun.created_at.desc(), TaskRun.id.desc())).label("rank")).where(
                TaskRun.task_type.in_(keys)).subquery()
        latest = {run.task_type: run for run in self.session.scalars(select(TaskRun).join(
            ranked, ranked.c.id == TaskRun.id).where(ranked.c.rank == 1))}
        capabilities = []
        for item in definitions:
            run = latest.get(item.key)
            capabilities.append({"key": item.key, "name": item.name, "english_name": item.english_name,
                "task_count": counts.get(item.key, 0),
                "available": row.enabled and configured(row),
                "last_run": None if run is None else {"id": run.id, "task_id": run.task_id,
                    "status": run.status, "created_at": run.created_at, "started_at": run.started_at,
                    "finished_at": run.finished_at}})
        return {"key": key, "name": provider.name, "fields": provider.fields,
            "values": row.values, "secret_fields_configured": [field["key"] for field in provider.fields
                if field["type"] == "secret" and bool(secrets.get(field["key"]))],
            "configured": configured(row), "enabled": row.enabled, "version": row.version,
            "checked_at": row.checked_at, "check_status": row.check_status,
            "check_message": row.check_message, "capabilities": capabilities}

    def validate_draft(self, key: str, version: int, fields: dict) -> tuple[dict, dict]:
        provider = require_provider(key)
        row = require_config(self.session, key)
        self._check_version(row, version)
        if not row.initialized:
            raise SourceError("请重启服务完成数据源初始化。", status_code=503)
        values, secrets = provider.validate({**row.values, **fields},
            decrypt(self.settings, key, row.encrypted_secrets))
        # End the read transaction before network I/O. A subsequent save takes
        # a fresh lock and rejects a result based on any superseded revision.
        self.session.rollback()
        return values, secrets

    def test(self, key: str, version: int, fields: dict) -> dict:
        values, secrets = self.validate_draft(key, version, fields)
        result = require_provider(key).probe(values, secrets)
        return {"ok": result.ok, "status": result.status, "message": result.message,
                "checked_at": result.checked_at, "version": version, "saved": False}

    def save(self, key: str, version: int, fields: dict) -> dict:
        values, secrets = self.validate_draft(key, version, fields)
        cipher(self.settings)  # Fail before contacting the provider if storage is unavailable.
        result = require_provider(key).probe(values, secrets)
        if not result.ok:
            raise SourceError(result.message + " 原配置保持不变。")
        row = require_config(self.session, key, lock=True)
        self._check_version(row, version)
        row.values, row.encrypted_secrets = values, encrypt(self.settings, key, secrets)
        row.check_status, row.check_message, row.checked_at = result.status, result.message, result.checked_at
        row.version += 1
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        logger.info("data_source_config_saved", source=key,
                    message=f"{require_provider(key).name} 数据源连接已验证并保存，新运行将使用更新后的配置。")
        return self.detail(key)

    def set_enabled(self, key: str, version: int, enabled: bool) -> dict:
        provider = require_provider(key)
        row = require_config(self.session, key, lock=True)
        self._check_version(row, version)
        if enabled and not configured(row):
            raise SourceError("请先完成连接配置，再启用数据源。", status_code=409)
        skipped = 0
        if not enabled:
            keys = [item.key for item in self.registry.list() if item.source_key == key]
            skipped = skip_source_queue(self.session, keys, f"{provider.name} 数据源已停用，本次运行已跳过。")
        row.enabled = enabled
        row.version += 1
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        logger.info("data_source_state_changed", source=key, enabled=enabled, skipped=skipped,
            message=(f"{provider.name} 数据源已启用，后续计划正常执行，不补跑停用期间的计划。" if enabled else
                     f"{provider.name} 数据源已停用，已跳过 {skipped} 个等待运行，已开始的任务继续执行。"))
        return {**self.detail(key), "skipped_runs": skipped}

    @staticmethod
    def _check_version(row: DataSourceConfig, version: int) -> None:
        if row.version != version:
            raise SourceError("数据源配置已被更新，请刷新后重新操作。", status_code=409)
