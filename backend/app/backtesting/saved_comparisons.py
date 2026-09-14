"""Owned comparison definitions; financial results remain on immutable runs."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.backtesting.models import BacktestRunRecord
from app.core.auth import AuthenticatedPrincipal
from app.db.base import Base
from app.db.session import get_db_session


class SavedComparison(Base):
    __tablename__ = "backtest_saved_comparisons"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_backtest_saved_comparisons_owner_updated", "owner_scope", "updated_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    baseline_run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class SavedComparisonMember(Base):
    __tablename__ = "backtest_saved_comparison_members"
    __table_args__ = (
        UniqueConstraint("comparison_id", "position"),
        CheckConstraint("position >= 0 AND position < 10", name="position_range"),
    )
    comparison_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("backtest_saved_comparisons.id", ondelete="CASCADE"), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("backtest_runs.id", ondelete="RESTRICT"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ComparisonDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    run_ids: list[UUID] = Field(min_length=2, max_length=10)
    baseline_run_id: UUID

    @model_validator(mode="after")
    def valid_members(self):
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("对比运行不能重复")
        if self.baseline_run_id not in self.run_ids:
            raise ValueError("比较基准必须属于已选运行")
        return self


class CreateComparison(ComparisonDefinition):
    # Client-generated identity makes retries after a lost response idempotent.
    id: UUID = Field(default_factory=uuid4)


class UpdateComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    name: Name | None = None
    run_ids: list[UUID] | None = Field(default=None, min_length=2, max_length=10)
    baseline_run_id: UUID | None = None

    @model_validator(mode="after")
    def reject_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set - {"version"}):
            raise ValueError("更新字段不能为 null")
        return self


class ComparisonView(ComparisonDefinition):
    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class ComparisonPage(BaseModel):
    items: list[ComparisonView]
    total: int
    offset: int
    limit: int
    has_more: bool


class ComparisonStore:
    """Mutations flush in the caller transaction; ownership is never optional."""
    def __init__(self, session: Session, owner: str):
        self.session, self.owner = session, owner

    def get(self, identity: UUID, *, lock=False) -> SavedComparison:
        query = select(SavedComparison).where(SavedComparison.id == identity, SavedComparison.owner_scope == self.owner)
        if lock:
            query = query.with_for_update()
        row = self.session.scalar(query)
        if row is None:
            raise HTTPException(404, "保存的对比不存在")
        return row

    def view(self, row: SavedComparison) -> ComparisonView:
        ids = list(self.session.scalars(select(SavedComparisonMember.run_id).where(SavedComparisonMember.comparison_id == row.id).order_by(SavedComparisonMember.position)))
        return ComparisonView(id=row.id, name=row.name, run_ids=ids, baseline_run_id=row.baseline_run_id, version=row.version, created_at=row.created_at, updated_at=row.updated_at)

    def check_runs(self, ids: list[UUID]):
        # Lock in deterministic order. Only completed formal runs owned by the
        # caller may enter a definition; no internal/foreign identities leak.
        rows = list(self.session.scalars(select(BacktestRunRecord).where(BacktestRunRecord.id.in_(ids), BacktestRunRecord.idempotency_scope == self.owner).order_by(BacktestRunRecord.id).with_for_update()))
        if len(rows) != len(ids) or any(row.run_kind != "backtest_run" or row.profile != "formal@1" for row in rows):
            raise HTTPException(404, "所选正式回测不存在或不可访问")
        if any(row.status != "succeeded" or row.terminal_status != "succeeded" for row in rows):
            raise HTTPException(409, "仅可保存已完成运行的对比")

    def members(self, row: SavedComparison, ids: list[UUID]):
        self.session.execute(delete(SavedComparisonMember).where(SavedComparisonMember.comparison_id == row.id))
        self.session.flush()
        self.session.add_all([SavedComparisonMember(comparison_id=row.id, run_id=rid, position=index) for index, rid in enumerate(ids)])
        self.session.flush()

    def create(self, payload: CreateComparison) -> ComparisonView:
        existing = self.session.get(SavedComparison, payload.id)
        if existing is not None:
            row = self.get(payload.id)
            view = self.view(row)
            if (view.name, view.run_ids, view.baseline_run_id) != (payload.name, payload.run_ids, payload.baseline_run_id):
                raise HTTPException(409, "此保存请求已被使用，请刷新后重试")
            return view
        self.check_runs(payload.run_ids)
        # Recheck after the run locks serialize simultaneous retries for the
        # same definition, avoiding a uniqueness failure on repeated POSTs.
        existing = self.session.get(SavedComparison, payload.id, populate_existing=True)
        if existing is not None:
            return self.create(payload)
        row = SavedComparison(id=payload.id, owner_scope=self.owner, name=payload.name, baseline_run_id=payload.baseline_run_id)
        self.session.add(row); self.session.flush()
        self.members(row, payload.run_ids)
        return self.view(row)

    def update(self, identity: UUID, payload: UpdateComparison) -> ComparisonView:
        row = self.get(identity, lock=True)
        if row.version != payload.version:
            raise HTTPException(409, "对比已被修改，请刷新后重试")
        previous = self.view(row)
        values = {"name": previous.name, "run_ids": previous.run_ids, "baseline_run_id": previous.baseline_run_id}
        values.update(payload.model_dump(exclude_unset=True, exclude={"version"}))
        try:
            definition = ComparisonDefinition(**values)
        except ValueError as exc:
            raise HTTPException(422, "运行列表与比较基准不匹配") from exc
        self.check_runs(definition.run_ids)
        row.name, row.baseline_run_id = definition.name, definition.baseline_run_id
        row.version += 1; row.updated_at = datetime.now(timezone.utc)
        self.members(row, definition.run_ids)
        return self.view(row)

    def remove(self, identity: UUID, version: int):
        row = self.get(identity, lock=True)
        if row.version != version:
            raise HTTPException(409, "对比已被修改，请刷新后重试")
        self.session.execute(delete(SavedComparisonMember).where(SavedComparisonMember.comparison_id == identity))
        self.session.delete(row); self.session.flush()

    def page(self, limit: int, offset: int) -> ComparisonPage:
        where = SavedComparison.owner_scope == self.owner
        total = self.session.scalar(select(func.count()).select_from(SavedComparison).where(where)) or 0
        rows = self.session.scalars(select(SavedComparison).where(where).order_by(SavedComparison.updated_at.desc(), SavedComparison.id).limit(limit).offset(offset))
        return ComparisonPage(items=[self.view(row) for row in rows], total=total, limit=limit, offset=offset, has_more=offset + limit < total)


def store(request: Request, session: Session = Depends(get_db_session)) -> ComparisonStore:
    principal = getattr(request.state, "authenticated_principal", None)
    if not isinstance(principal, AuthenticatedPrincipal):
        raise HTTPException(401, "请先登录")
    return ComparisonStore(session, principal.owner_scope)


router = APIRouter(prefix="/api/admin/backtest-comparisons", tags=["backtest-comparisons"])


@router.get("", response_model=ComparisonPage)
def list_saved_comparisons(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), repository: ComparisonStore = Depends(store)):
    return repository.page(limit, offset)


@router.post("", response_model=ComparisonView, status_code=201)
def create_saved_comparison(payload: CreateComparison, repository: ComparisonStore = Depends(store)):
    try:
        result = repository.create(payload)
        repository.session.commit()
        return result
    except IntegrityError:
        # A reused client identity with different, non-overlapping run sets
        # may race outside the shared run locks. Recover only that identity
        # collision; other integrity failures must retain their real cause.
        repository.session.rollback()
        if repository.session.get(SavedComparison, payload.id) is None:
            raise
        result = repository.create(payload)
        repository.session.commit()
        return result


@router.get("/{identity}", response_model=ComparisonView)
def get_saved_comparison(identity: UUID, repository: ComparisonStore = Depends(store)):
    return repository.view(repository.get(identity))


@router.patch("/{identity}", response_model=ComparisonView)
def update_saved_comparison(identity: UUID, payload: UpdateComparison, repository: ComparisonStore = Depends(store)):
    result = repository.update(identity, payload)
    repository.session.commit()
    return result


@router.delete("/{identity}", status_code=204)
def delete_saved_comparison(identity: UUID, version: int = Query(..., ge=1), repository: ComparisonStore = Depends(store)):
    repository.remove(identity, version)
    repository.session.commit()
    return Response(status_code=204)
