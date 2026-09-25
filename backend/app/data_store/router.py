"""Authenticated, current-only data catalog and bounded preview API."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import re
from threading import Lock
from typing import Annotated, Iterator

from anyio import from_thread
from anyio.lowlevel import current_token
from fastapi import APIRouter, Depends, HTTPException, Query as ApiQuery, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db_session, get_engine

from .adapters.registry import ENTRIES, Entry
from .availability import read_availability, require_ready
from .errors import DataStoreError
from .readers import Query
from .storage import CurrentStore


router = APIRouter(prefix="/api/admin/data-store", tags=["admin-data-store"])
legacy_router = APIRouter(prefix="/api/admin/data-foundation", tags=["retired-data-foundation"])
BUSINESS = {entry.spec.name: entry for entry in ENTRIES if entry.business}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:$|:)")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _entry(dataset: str) -> Entry:
    entry = BUSINESS.get(dataset)
    if entry is None:
        raise HTTPException(404, detail={"code": "DATASET_UNKNOWN", "message": "未找到该数据集。"})
    return entry


def _frequency(entry: Entry) -> str:
    native = entry.native
    if native == "tick":
        return "tick"
    if native == "intraday_bar":
        return "minute"
    if "daily" in native or native in {"calendar", "exchange_calendar"}:
        return "daily"
    if entry.projection[0] in {"reports", "group"}:
        return "report"
    return "object"


def _title(entry: Entry) -> str:
    category = {
        "market": "市场",
        "fund": "基金",
        "instrument": "标的",
        "operations": "运维",
    }.get(entry.domain.split(".")[0], "数据")
    return f"{category} · {entry.native}"


def _http_error(error: DataStoreError) -> HTTPException:
    status = (409 if error.code in {"DATA_CHANGED", "REBUILD_REQUIRED", "DATA_RESTRICTED"}
              else 503 if error.code in {"DATA_STORE_REBUILDING", "DATA_STORE_NOT_INITIALIZED",
                                          "CATALOG_UNAVAILABLE", "STORAGE_UNAVAILABLE", "LOCK_TIMEOUT"}
              else 422)
    return HTTPException(status, detail={"code": error.code, "message": str(error)})


@contextmanager
def _store(settings: Settings, fresh: bool) -> Iterator[CurrentStore]:
    if not settings.data_store_root.exists() and not fresh:
        raise DataStoreError("DATA_STORE_NOT_INITIALIZED")
    with CurrentStore(
        get_engine(),
        settings.data_store_root,
        cursor_key=settings.cursor_signing_key.get_secret_value().encode(),
        initialize=fresh,
    ) as store:
        yield store


def _decode_strings(raw: bytes, count: int = 3) -> list[str] | None:
    """Decode only leading string keys from the kernel's prefix-free codec."""
    values: list[str] = []
    current = bytearray()
    index = 0
    while index + 1 < len(raw) and len(values) < count:
        if raw[index] == 0 and raw[index + 1] == 0:
            try:
                values.append(current.decode("utf-8"))
            except UnicodeError:
                return None
            current.clear()
            index += 2
        elif raw[index] == 0 and raw[index + 1] == 255:
            current.append(0)
            index += 2
        else:
            current.append(raw[index])
            index += 1
    return values if len(values) == count else None


def _partition_info(store: CurrentStore, dataset: str) -> dict:
    with store.catalog.transaction() as connection:
        partitions = connection.execute(text(
            "SELECT DISTINCT ON (partition_key) partition_key, key_min AS first_key "
            "FROM data_store_files WHERE dataset=:dataset "
            "ORDER BY partition_key, key_min LIMIT 65"
        ), {"dataset": dataset}).mappings().all()
    listed = [row["partition_key"] for row in partitions[:64]]
    first = _decode_strings(partitions[0]["first_key"]) if partitions else None
    return {
        "partitions": listed,
        "partitions_truncated": len(partitions) > 64,
        "partition_range": {
            "from": listed[0] if listed else None,
            "to": listed[-1] if listed and len(partitions) <= 64 else None,
            "precision": "partition",
        },
        "preview_key": ({
            "representation": first[0], "subject": first[1],
            "object_key": first[2], "partition": listed[0],
        } if first else None),
    }


def _legacy_restrictions(session: Session, dataset: str) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM data_store_legacy_restrictions "
        "WHERE dataset=:dataset OR dataset IS NULL"
    ), {"dataset": dataset}).scalar_one())


def _describe(entry: Entry, store: CurrentStore | None, phase: str, session: Session) -> dict:
    capability = entry.describe_capability()
    result = {
        "dataset": entry.spec.name,
        "name": _title(entry),
        "domain": entry.domain,
        "source": entry.source,
        "frequency": _frequency(entry),
        "representation": entry.spec.semantics["row_layout"],
        "schema_id": entry.spec.schema_id,
        "rule": entry.spec.rule,
        "fields": capability["field_contracts"],
        "limitations": capability["limitations"],
        "business_key": capability["business_key"],
        "status": "rebuilding" if phase != "ready" else "not_checked",
        "row_count": None,
        "generation": None,
        "updated_at": None,
        "issues": None,
        "last_update": None,
        "partitions": [],
        "partitions_truncated": False,
        "partition_range": {"from": None, "to": None, "precision": "partition"},
        "preview_key": None,
    }
    if store is None:
        return result
    try:
        current = store.describe_capability(entry.spec)
    except DataStoreError as error:
        if error.code == "DATASET_MISSING":
            return result
        raise
    result.update(
        status=current["status"],
        row_count=current["row_count"],
        generation=current["generation"],
        updated_at=current["updated_at"],
        issues=current["issues"],
    )
    result.update(_partition_info(store, entry.spec.name))
    status = session.execute(text(
        "SELECT summary_json,updated_at FROM data_store_entry_status WHERE entry_id=:id"
    ), {"id": entry.id}).mappings().first()
    if status:
        summary = json.loads(status["summary_json"])
        result["last_update"] = {
            "state": summary.get("state", "not_checked"),
            "complete": summary.get("complete", False),
            "qualified": summary.get("qualified", False),
            "reason": summary.get("reason"),
            "source_rows": summary.get("source_rows"),
            "committed_partitions": summary.get("committed_partitions"),
            "updated_at": status["updated_at"].isoformat(),
        }
    legacy_count = _legacy_restrictions(session, entry.spec.name)
    result["legacy_restrictions"] = legacy_count
    if legacy_count:
        result["status"] = "restricted"
    return result


@router.get("/datasets")
def list_datasets(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, ApiQuery(ge=1, le=100)] = 50,
    offset: Annotated[int, ApiQuery(ge=0)] = 0,
) -> dict:
    try:
        availability = read_availability(session)
        entries = list(BUSINESS.values())
        selected = entries[offset:offset + limit]
        if not availability.ready:
            items = [_describe(entry, None, availability.phase, session) for entry in selected]
        else:
            with _store(_settings(request), availability.fresh_install) as store:
                items = [_describe(entry, store, availability.phase, session) for entry in selected]
        return {"items": items, "total": len(entries), "phase": availability.phase,
                "next_offset": offset + limit if offset + limit < len(entries) else None}
    except DataStoreError as error:
        raise _http_error(error) from None


@router.get("/datasets/{dataset:path}")
def get_dataset(
    dataset: str, request: Request, session: Annotated[Session, Depends(get_db_session)]
) -> dict:
    entry = _entry(dataset)
    try:
        availability = read_availability(session)
        if not availability.ready:
            return _describe(entry, None, availability.phase, session)
        with _store(_settings(request), availability.fresh_install) as store:
            return _describe(entry, store, availability.phase, session)
    except DataStoreError as error:
        raise _http_error(error) from None


@router.get("/status")
def get_status(
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, ApiQuery(ge=1, le=100)] = 50,
    offset: Annotated[int, ApiQuery(ge=0)] = 0,
    state: str | None = None,
) -> dict:
    availability = read_availability(session)
    rows = session.execute(text(
        "SELECT entry_id,summary_json,updated_at FROM data_store_entry_status "
        "WHERE entry_id = ANY(:entry_ids)"
    ), {"entry_ids": [entry.id for entry in ENTRIES]}).mappings().all()
    by_id = {row["entry_id"]: row for row in rows}
    items = []
    for entry in ENTRIES:
        row = by_id.get(entry.id)
        summary = json.loads(row["summary_json"]) if row else {
            "entry_id": entry.id, "state": "not_checked", "complete": False
        }
        if state and summary.get("state") != state:
            continue
        items.append({
            "entry_id": entry.id, "dataset": entry.spec.name if entry.business else None,
            "name": _title(entry), "classification": entry.disposition,
            "state": summary.get("state", "not_checked"),
            "complete": summary.get("complete", False),
            "qualified": summary.get("qualified", False),
            "reason": summary.get("reason"),
            "source_rows": summary.get("source_rows"),
            "committed_partitions": summary.get("committed_partitions"),
            "updated_at": row["updated_at"].isoformat() if row else None,
        })
    return {
        "phase": availability.phase,
        "code": "READY" if availability.ready else "DATA_STORE_REBUILDING",
        "fresh_install": availability.fresh_install,
        "items": items[offset:offset + limit], "total": len(items),
        "next_offset": offset + limit if offset + limit < len(items) else None,
    }


@router.get("/issues")
def get_issues(
    session: Annotated[Session, Depends(get_db_session)],
    dataset: str | None = None,
    limit: Annotated[int, ApiQuery(ge=1, le=100)] = 50,
    offset: Annotated[int, ApiQuery(ge=0)] = 0,
) -> dict:
    if dataset is not None:
        _entry(dataset)
    params = {"dataset": dataset, "limit": limit, "offset": offset}
    where = "WHERE (:dataset IS NULL OR dataset=:dataset OR dataset IS NULL)"
    union = (
        "SELECT dataset,scope_key,reason,last_seen AS updated_at,'current' AS kind "
        "FROM data_store_issues " + where + " UNION ALL "
        "SELECT dataset,scope_key,'LEGACY_RESTRICTION' AS reason,"
        "captured_at AS updated_at,'legacy' AS kind "
        "FROM data_store_legacy_restrictions " + where
    )
    total = session.execute(text("SELECT count(*) FROM (" + union + ") AS issues"), params).scalar_one()
    rows = session.execute(text(
        "SELECT * FROM (" + union + ") AS issues "
        "ORDER BY updated_at DESC,dataset,scope_key LIMIT :limit OFFSET :offset"
    ), params).mappings().all()
    return {
        "items": [{**dict(row), "updated_at": row["updated_at"].isoformat()} for row in rows],
        "total": total,
        "next_offset": offset + limit if offset + limit < total else None,
    }


class CurrentQuery(BaseModel):
    # Reject retired release IDs and unknown controls instead of silently
    # reading today's value under a historical-looking request.
    model_config = ConfigDict(extra="forbid")

    dataset: str
    frequency: str
    representation: str = Field(min_length=1, max_length=256)
    subject: str = Field(min_length=1, max_length=256)
    from_key: str = Field(min_length=1, max_length=256)
    to_key: str = Field(min_length=1, max_length=256)
    columns: list[str] = Field(default_factory=list, max_length=32)
    partitions: list[str] | None = Field(default=None, max_length=64)
    page_size: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=8192)
    allow_partial: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> "CurrentQuery":
        if self.from_key > self.to_key:
            raise ValueError("起始业务键不得晚于结束业务键。")
        return self


def _matching_partitions(store: CurrentStore, entry: Entry, query: CurrentQuery) -> list[str]:
    with store.catalog.transaction() as connection:
        rows = connection.execute(text(
            "SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:dataset "
            "ORDER BY partition_key LIMIT 10001"
        ), {"dataset": entry.spec.name}).scalars().all()
    if len(rows) > 10000:
        raise DataStoreError("QUERY_BUDGET_EXCEEDED")
    if entry.native != "tick" and _DATE.match(query.from_key) and _DATE.match(query.to_key):
        begin, end = query.from_key[:7], query.to_key[:7]
        # The static partitioner binds a month and a stable
        # representation/subject bucket. Other subjects in that month are not
        # part of this request and must not make a complete request partial.
        expected = set()
        year, month = map(int, begin.split("-"))
        while f"{year:04d}-{month:02d}" <= end:
            marker = f"{year:04d}-{month:02d}-01"
            expected.add(entry.spec.partitioner((
                query.representation, query.subject, marker, "root"
            )))
            if len(expected) > store.limits.query_partitions:
                raise DataStoreError("QUERY_BUDGET_EXCEEDED")
            month += 1
            if month == 13:
                year, month = year + 1, 1
        rows = [part for part in rows if part in expected]
    elif query.from_key == query.to_key:
        expected = entry.spec.partitioner((
            query.representation, query.subject, query.from_key, "root"
        ))
        rows = [part for part in rows if part == expected]
    if query.partitions is not None:
        selected = list(dict.fromkeys(query.partitions))
        if len(selected) != len(query.partitions) or not set(selected) <= set(rows):
            raise DataStoreError("INVALID_VALUE")
        if not query.allow_partial and set(selected) != set(rows):
            raise DataStoreError("PARTIAL_SCOPE_REQUIRED")
        rows = selected
    if len(rows) > store.limits.query_partitions:
        raise DataStoreError("QUERY_BUDGET_EXCEEDED")
    return rows


@router.post("/query")
def query_current(
    payload: CurrentQuery, request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> dict:
    if request.query_params:
        raise _http_error(DataStoreError("HISTORY_UNSUPPORTED"))
    entry = _entry(payload.dataset)
    if payload.frequency != _frequency(entry):
        raise HTTPException(422, detail={"code": "FREQUENCY_UNSUPPORTED",
                                         "message": "所选频率与数据集契约不一致。"})
    try:
        availability = require_ready(session)
        if _legacy_restrictions(session, entry.spec.name):
            raise DataStoreError("DATA_RESTRICTED")
        with _store(_settings(request), availability.fresh_install) as store:
            partitions = _matching_partitions(store, entry, payload)
            if not partitions:
                try:
                    descriptor = store.describe_capability(entry.spec)
                except DataStoreError as error:
                    if error.code != "DATASET_MISSING":
                        raise
                    descriptor = None
                issues = descriptor["issues"] if descriptor else 0
                if issues and not payload.allow_partial:
                    raise DataStoreError("DATA_RESTRICTED")
                return {
                    "dataset": entry.spec.name, "rows": [], "next_cursor": None,
                    "status": "restricted" if issues else "empty",
                    "request_satisfied": False,
                    "business_date_coverage_verified": False,
                    "partial_requested": payload.allow_partial,
                    "actual_range": {"from": None, "to": None},
                    "selected_partitions": [],
                    "generation": descriptor["generation"] if descriptor else None,
                    "schema_id": entry.spec.schema_id,
                    "semantics": dict(entry.spec.semantics),
                    "unresolved_issues": issues,
                    "limitations": entry.describe_capability()["limitations"],
                }
            columns = tuple(payload.columns or entry.spec.schema.names)
            if any(column not in entry.spec.schema.names for column in columns):
                raise DataStoreError("INVALID_VALUE")
            query = Query(
                partitions=tuple(partitions),
                lower=(payload.representation, payload.subject, payload.from_key),
                upper=(payload.representation, payload.subject, payload.to_key + "\x00"),
                columns=columns, page_size=payload.page_size,
                cursor=payload.cursor, require_qualified=not payload.allow_partial,
            )
            # FastAPI runs this synchronous handler in an AnyIO worker. The
            # kernel also calls the callback from a DuckDB watchdog thread;
            # carry the event-loop token explicitly and serialize receive
            # checks across both threads.
            token = from_thread.run_sync(current_token)
            disconnect_lock = Lock()

            def cancelled() -> bool:
                with disconnect_lock:
                    return from_thread.run(request.is_disconnected, token=token)

            page = store.read_many(
                [(entry.spec, query)],
                cancelled=cancelled,
            )[0]
            rows = page.rows
            keys = [str(row["object_key"]) for row in rows if "object_key" in row]
            return {
                **page.to_dict(),
                "status": page.quality_status,
                # The current catalog proves which rows exist, but it cannot
                # prove continuous business-date coverage for an arbitrary
                # requested interval. Keep that claim false even for a clean
                # page; the caller may inspect the actual page and cursor.
                "request_satisfied": False,
                "business_date_coverage_verified": False,
                "partial_requested": payload.allow_partial,
                "actual_range": {"from": min(keys) if keys else None,
                                 "to": max(keys) if keys else None},
                "selected_partitions": partitions,
                "limitations": entry.describe_capability()["limitations"],
            }
    except DataStoreError as error:
        raise _http_error(error) from None


@legacy_router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
@legacy_router.api_route("", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def retired_legacy_route(path: str = "") -> None:
    """One deterministic refusal; no old ID is ever resolved to current data."""
    raise HTTPException(410, detail={
        "code": "LEGACY_FOUNDATION_REMOVED",
        "message": "旧数据底座的发布与候选接口已退役，请使用当前数据接口。",
    })
