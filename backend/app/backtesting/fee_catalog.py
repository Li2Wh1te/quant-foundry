"""Independent immutable fee versions, selectable by multiple account profiles.

Legacy account-owned snapshots remain authoritative; they are never silently
imported into the shared namespace because historical keys may collide across
accounts. New catalog versions are explicit, append-only key/version records.
"""
from datetime import datetime
from typing import Annotated, Any
from copy import deepcopy
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CheckConstraint, DateTime, Integer, JSON, String, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.exc import IntegrityError
from app.db.base import Base
from app.db.session import get_db_session
from app.backtesting.schemas import FeeScheduleRequest
from app.backtesting.service import _build_schedule, _json_value
from app.backtesting.data.reports import canonical_hash
from app.backtesting.account_profiles import _fee_schedule_payload


class FeeScheduleVersionRecord(Base):
    __tablename__ = "backtest_fee_schedule_versions"
    __table_args__ = (CheckConstraint("version > 0", name="fee_catalog_version_positive"),)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


@event.listens_for(FeeScheduleVersionRecord, "before_update")
@event.listens_for(FeeScheduleVersionRecord, "before_delete")
def _immutable(*_args):
    raise ValueError("fee catalog versions are immutable")


class FeeCatalogRepository:
    def __init__(self, session: Session):
        self.session = session

    def require(self, key: str, version: int):
        row = self.session.get(FeeScheduleVersionRecord, (key, version))
        if row is None:
            raise ValueError(f"fee catalog version not found: {key}@{version}")
        if canonical_hash(row.snapshot) != row.snapshot_hash:
            raise ValueError("fee catalog snapshot integrity failed")
        return deepcopy(row.snapshot)

    def publish(self, *, key, version, name_zh, name_en, fee_rules, metadata):
        schedule = _build_schedule(dict(key=key, fee_rules=fee_rules, metadata=metadata), version=version)
        schedule.validate_for_run()
        if not name_zh.strip() or not name_en.strip():
            raise ValueError("fee catalog requires Chinese and English display names")
        snapshot = _json_value(_fee_schedule_payload(schedule))
        digest = canonical_hash(snapshot)
        row = self.session.get(FeeScheduleVersionRecord, (key, version))
        if row is not None:
            if row.snapshot_hash != digest or (row.name_zh, row.name_en) != (name_zh, name_en):
                raise ValueError("fee catalog identity already published with different content")
            return row
        row = FeeScheduleVersionRecord(key=key, version=version, name_zh=name_zh, name_en=name_en,
                                       snapshot=snapshot, snapshot_hash=digest)
        self.session.add(row)
        self.session.flush()
        return row

    def list(self):
        return list(self.session.scalars(select(FeeScheduleVersionRecord).order_by(
            FeeScheduleVersionRecord.key, FeeScheduleVersionRecord.version.desc()).limit(500)))


def describe(row):
    return dict(key=row.key, version=row.version, name_zh=row.name_zh, name_en=row.name_en,
                display_name=f"{row.name_zh}（{row.name_en}）", snapshot=row.snapshot,
                snapshot_hash=row.snapshot_hash, parameter_schema={"type":"object", "properties":{}, "additionalProperties":False},
                capabilities={"immutable":True, "source":"fee_catalog"})


class FeeCatalogCreate(FeeScheduleRequest):
    version: int = Field(ge=1, strict=True)
    name_zh: str = Field(min_length=1)
    name_en: str = Field(min_length=1)


router = APIRouter(prefix="/api/admin/backtest-fee-schedules", tags=["backtest-fees"])


@router.get("")
def list_fee_versions(session: Annotated[Session, Depends(get_db_session)]):
    return [describe(row) for row in FeeCatalogRepository(session).list()]


@router.get("/{key}/{version}")
def get_fee_version(key: str, version: int, session: Annotated[Session, Depends(get_db_session)]):
    try:
        return FeeCatalogRepository(session).require(key, version)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.post("", status_code=201)
def publish_fee_version(payload: FeeCatalogCreate, session: Annotated[Session, Depends(get_db_session)]):
    try:
        row = FeeCatalogRepository(session).publish(**payload.model_dump())
        session.commit()
        return describe(row)
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(409, detail="费用版本发布失败：标识已存在或配置无效。") from exc
