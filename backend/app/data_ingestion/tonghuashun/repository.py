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

SERIES_KEYS = {"etf_daily": "date_ms", "stock_daily": "date_ms", "index_daily": "date_ms",
    "fund_nav": "nav_date", "stock_income": "period_end_ms", "stock_balance": "period_end_ms",
    "stock_cash_flow": "period_end_ms", "stock_indicators": "report",
    "fund_stock_history": "report_key", "fund_bond_history": "report_key"}


def materialize(session: Session, observation: Observation) -> dict:
    """Reconstruct exactly one immutable version, never the current head.

    Chains are bounded at publication time. Scope checks also prevent corrupt
    references from mixing symbols, dates or source-local datasets during reads.
    """
    chain, seen, cursor = [], set(), observation
    while True:
        if cursor.id in seen or len(chain) > 30:
            raise CollectionError("采集版本引用异常，无法还原该版本。")
        if (cursor.dataset, cursor.subject, cursor.variant) != (observation.dataset, observation.subject, observation.variant):
            raise CollectionError("采集版本引用了其他数据范围。")
        seen.add(cursor.id)
        chain.append(cursor)
        if cursor.base_observation_id is None:
            break
        cursor = session.get(Observation, cursor.base_observation_id)
        if cursor is None:
            raise CollectionError("采集版本缺少基础快照。")
    data = json.loads(chain.pop().data_json, parse_float=Decimal)
    for version in reversed(chain):
        delta = json.loads(version.data_json, parse_float=Decimal)
        key = delta["key_field"]
        rows = {row[key]: row for row in data["item"]}
        for identity in delta["removed"]:
            rows.pop(identity, None)
        rows.update({row[key]: row for row in delta["upserts"]})
        data = {**delta["metadata"], "item": [rows[k] for k in sorted(rows)]}
    if content_hash(data) != observation.content_hash:
        raise CollectionError("采集版本内容校验失败。")
    return data


def archive_data(dataset: str, data: dict, previous: Observation | None, old: dict | None):
    """Use a delta only when it is materially smaller than a full anchor."""
    full = exact_json(data)
    key = SERIES_KEYS.get(dataset)
    if key and previous and old and previous.chain_depth < 30:
        old_rows = {r[key]: r for r in old["item"]}
        new_rows = {r[key]: r for r in data["item"]}
        delta = {"key_field": key, "metadata": {k: v for k, v in data.items() if k != "item"},
            "upserts": [row for identity, row in new_rows.items() if identity not in old_rows or exact_json(row) != exact_json(old_rows[identity])],
            "removed": sorted(set(old_rows) - set(new_rows))}
        encoded = exact_json(delta)
        if len(encoded) < len(full) * 0.7:
            return encoded, previous.id, previous.chain_depth + 1
    return full, None, 0


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
            materialize(self.session, observation) if observation else {} if state and state.observation_id else None,
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
        old = materialize(self.session, previous) if previous else None
        digest = content_hash(data)
        row_count = len(data.get("item", data.get("abilities", [])))
        old_rows = old.get("item", []) if old else []
        old_counts = Counter(content_hash(row) for row in old_rows)
        new_counts = Counter(content_hash(row) for row in data.get("item", []))
        unchanged = sum((old_counts & new_counts).values())
        removed = sum((old_counts - new_counts).values())
        encoded, base_id, depth = archive_data(dataset, data, previous, old)
        version = Observation(id=uuid4(), dataset=dataset, subject=subject, variant=variant,
            observed_at=now, request_json=exact_json(requests), data_json=encoded,
            content_hash=digest, row_count=row_count, base_observation_id=base_id, chain_depth=depth)
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
