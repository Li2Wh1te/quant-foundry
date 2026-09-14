"""Atomic source-local publication and optimistic protection against stale jobs."""

from dataclasses import dataclass
from collections import Counter
from datetime import datetime
import json
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.tonghuashun import (
    TonghuashunCollectionState as State, TonghuashunObservation as Observation,
    TonghuashunTicker as Ticker,
)
from app.data_ingestion.tonghuashun.contracts import CollectionError, content_hash, exact_json


@dataclass(frozen=True)
class Previous:
    revision: int
    data: dict | None
    succeeded_at: datetime | None
    status: str
    reconciled_at: datetime | None = None


class CollectionRepository:
    def __init__(self, session: Session):
        self.session = session

    def read(self, dataset: str, subject: str, variant: str, *, with_data: bool = True) -> Previous:
        state = self.session.get(State, (dataset, subject, variant))
        observation = self.session.get(Observation, state.observation_id) if with_data and state and state.observation_id else None
        return Previous(state.revision if state else 0,
            json.loads(observation.data_json, parse_float=Decimal) if observation else {} if state and state.observation_id else None,
            state.succeeded_at if state else None, state.status if state else "pending",
            state.reconciled_at if state else None)

    def _lock(self, dataset: str, subject: str, variant: str, expected: int, now: datetime):
        insert = pg_insert if self.session.bind.dialect.name == "postgresql" else sqlite_insert
        self.session.execute(insert(State).values(dataset=dataset, subject=subject, variant=variant,
            revision=0, status="pending", attempted_at=now).on_conflict_do_nothing())
        state = self.session.scalar(select(State).where(State.dataset == dataset,
            State.subject == subject, State.variant == variant).with_for_update().execution_options(populate_existing=True))
        if state.revision != expected:
            raise CollectionError("同一采集范围已被其他运行更新，本次结果未覆盖较新版本。")
        return state

    def publish(self, dataset: str, subject: str, variant: str, *, expected: int,
                data: dict, requests: list[dict], now: datetime, ticker_rows: list[dict] | None = None,
                reconcile: bool = False):
        state = self._lock(dataset, subject, variant, expected, now)
        previous = self.session.get(Observation, state.observation_id) if state.observation_id else None
        digest = content_hash(data)
        row_count = len(data.get("item", data.get("abilities", [])))
        old_rows = json.loads(previous.data_json, parse_float=Decimal).get("item", []) if previous else []
        old_counts = Counter(content_hash(row) for row in old_rows)
        new_counts = Counter(content_hash(row) for row in data.get("item", []))
        unchanged = sum((old_counts & new_counts).values())
        removed = sum((old_counts - new_counts).values())
        version = Observation(id=uuid4(), dataset=dataset, subject=subject, variant=variant,
            observed_at=now, request_json=exact_json(requests), data_json=exact_json(data),
            content_hash=digest, row_count=row_count)
        self.session.add(version)
        self.session.flush()
        if ticker_rows is not None:
            # Missing rows are intentionally retained. Absence from today's
            # catalogue cannot establish delisting or identifier retirement.
            for row in ticker_rows:
                ticker = self.session.get(Ticker, row["thscode"])
                if ticker is None:
                    ticker = Ticker(thscode=row["thscode"], first_seen_at=now)
                    self.session.add(ticker)
                elif ticker.asset_type != row["asset_type"]:
                    raise CollectionError("已有标的的资产类型发生冲突，需要核对原始数据。")
                ticker.asset_type, ticker.name = row["asset_type"], row.get("name")
                ticker.exchange, ticker.raw_json = row.get("exchange"), exact_json(row)
                ticker.last_seen_at = now
        incomplete = bool(data.get("failed_requests"))
        state.observation_id = version.id
        state.status = "partial" if incomplete else "succeeded" if row_count else "empty"
        state.attempted_at = now
        if not incomplete:
            state.succeeded_at = now
        if reconcile and not incomplete:
            state.reconciled_at = now
        state.error_kind, state.revision = "partial_reports" if incomplete else None, state.revision + 1
        return {"version_id": str(version.id), "received": row_count,
                "changed": row_count - unchanged, "unchanged": unchanged, "removed": removed}

    def fail(self, dataset: str, subject: str, variant: str, *, expected: int,
             kind: str, now: datetime):
        state = self._lock(dataset, subject, variant, expected, now)
        state.status, state.attempted_at, state.error_kind = "failed", now, kind
        state.revision += 1
        # Last-good observation and succeeded_at remain untouched, so a failed
        # refresh never becomes an empty collection or a successful checkpoint.

    def subjects(self, assets: tuple[str, ...]) -> list[str]:
        return list(self.session.scalars(select(Ticker.thscode).where(
            Ticker.asset_type.in_(assets)).order_by(Ticker.thscode)))

    def related(self, field: str) -> list[str]:
        observations = self.session.scalars(select(Observation).join(State,
            State.observation_id == Observation.id).where(State.dataset == "fund_profile"))
        identifiers = set()
        for observation in observations:
            for row in json.loads(observation.data_json).get("item", []):
                values = ([row.get("company_id")] if field == "company_id" else
                          [m.get("manager_id") for m in row.get("manager_info", []) if isinstance(m, dict)])
                for value in values:
                    if isinstance(value, str) and value and len(value) <= 64:
                        identifiers.add(value)
        return sorted(identifiers)
