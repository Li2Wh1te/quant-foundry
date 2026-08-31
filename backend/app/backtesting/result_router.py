"""Read-only HTTP APIs for backtest result lists.

These endpoints only read results that a run has already persisted; they
never create runs, execute backtests, or recompute any engine output.  All
lists share one stable cursor-pagination contract: ``limit`` between 1 and
500 (default 100), an optional opaque ``cursor``, inclusive time bounds,
and kind-specific stable filters.  Invalid or mismatched cursors produce
an explicit 400 parameter error instead of silently restarting pagination.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Body
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.backtesting.models import BacktestRunRecord
from app.backtesting.result_records import BacktestEquityCurveRecord, BacktestMetricRecord

from app.backtesting.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorError,
    CursorPage,
    build_cursor,
    compute_query_digest,
    parse_cursor,
)
from app.backtesting.result_repository import (
    BacktestResultRepository,
    InternalResultNotVisibleError,
    ResultFilterError,
    UnknownResultKindError,
)
from app.backtesting.result_schemas import (
    BacktestAnalysisSummaryItem,
    BacktestDataChunkItem,
    BacktestDataPreflightItem,
    BacktestDecisionItem,
    BacktestEquityCurveItem,
    BacktestFillItem,
    BacktestMetricItem,
    BacktestOrderItem,
    BacktestOrderUpdateItem,
    BacktestPositionItem,
    BacktestStepItem,
    ResultCursorPage,
)
from app.db.session import get_db_session


router = APIRouter(
    prefix="/api/admin/backtest-runs/{run_id}/results",
    tags=["backtest-results"],
)
# Comparison is a collection-level formal backtest endpoint.  Keep it on the
# documented ``/api/admin/backtests`` resource rather than the internal run
# root namespace used by persistence handlers.
compare_router = APIRouter(prefix="/api/admin/backtests", tags=["backtest-compare"])
formal_result_alias_router = APIRouter(prefix="/api/admin/backtests/{run_id}", tags=["backtest-results"])


@compare_router.post("/compare")
def compare_runs(
    payload: dict = Body(...),
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """Compare persisted formal runs without invoking runtime or analyzers."""
    run_ids = payload.get("run_ids") if isinstance(payload, dict) else None
    if not isinstance(run_ids, list) or len(run_ids) < 2 or len(run_ids) != len(set(run_ids)):
        raise HTTPException(status_code=422, detail="run_ids 必须为至少两个不重复的运行 ID")
    try:
        ids = [UUID(str(value)) for value in run_ids]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="run_ids 包含无效 UUID") from exc
    roots = list(session.scalars(select(BacktestRunRecord).where(BacktestRunRecord.id.in_(ids))))
    by_id = {row.id: row for row in roots}
    # Apply the same root/visibility guard as every result handler. Unknown
    # roots and internal runs intentionally share one non-disclosing 404.
    if len(by_id) != len(ids) or any(
        row.run_kind != "backtest_run" or row.profile != "formal@1" for row in roots
    ):
        raise HTTPException(status_code=404, detail="正式回测结果不存在")
    # Fetch all rows in two set-based queries to avoid one query per run.
    equity_rows = list(session.scalars(
        select(BacktestEquityCurveRecord)
        .where(BacktestEquityCurveRecord.run_id.in_(ids))
        .order_by(BacktestEquityCurveRecord.run_id, BacktestEquityCurveRecord.as_of, BacktestEquityCurveRecord.sequence)
    ))
    metric_rows = list(session.scalars(
        select(BacktestMetricRecord)
        .where(BacktestMetricRecord.run_id.in_(ids))
        .order_by(BacktestMetricRecord.run_id, BacktestMetricRecord.metric_key, BacktestMetricRecord.formula_version)
    ))
    equity_by_run: dict[UUID, list[BacktestEquityCurveRecord]] = {rid: [] for rid in ids}
    metrics_by_run: dict[UUID, list[BacktestMetricRecord]] = {rid: [] for rid in ids}
    for row in equity_rows:
        equity_by_run.setdefault(row.run_id, []).append(row)
    for row in metric_rows:
        metrics_by_run.setdefault(row.run_id, []).append(row)
    summaries = []
    curves = []
    drawdowns = []
    metrics = []
    for rid in ids:
        root = by_id[rid]
        points = [{"as_of": row.as_of, "equity": (str(row.equity) if row.equity is not None else None), "drawdown": (str(row.drawdown) if row.drawdown is not None else None), "valuation_status": row.valuation_status} for row in equity_by_run[rid]]
        curves.append({"run_id": str(rid), "points": points})
        drawdowns.append({"run_id": str(rid), "points": [{"as_of": p["as_of"], "drawdown": p["drawdown"], "valuation_status": p["valuation_status"]} for p in points]})
        metrics.append({"run_id": str(rid), "items": [{"metric_key": row.metric_key, "formula_version": row.formula_version, "value": (str(row.value) if row.value is not None else None), "unit": row.unit, "sample_count": row.sample_count, "unavailable_reason": row.unavailable_reason} for row in metrics_by_run[rid]]})
        summaries.append({"run_id": str(rid), "status": root.status, "terminal_status": root.terminal_status, "config_hash": root.config_hash, "parameters": root.parameters, "backtest_config": root.backtest_config, "data_request": root.data_request, "behavior_versions": root.behavior_versions})
    # Include a compact, deterministic configuration diff for each run pair;
    # curves/metrics are fetched in batches above, never by invoking runtime.
    baseline = summaries[0].get("backtest_config") or summaries[0].get("parameters") or {}
    for summary in summaries:
        current = summary.get("backtest_config") or summary.get("parameters") or {}
        summary["config_diff"] = {
            key: {"baseline": baseline.get(key), "current": current.get(key)}
            for key in sorted(set(baseline) | set(current))
            if baseline.get(key) != current.get(key)
        }
    metric_matrix = []
    for rid in ids:
        for row in metrics_by_run[rid]:
            metric_matrix.append({"metric_key": row.metric_key, "formula_version": row.formula_version, "run_id": str(rid), "value": str(row.value) if row.value is not None else None, "unit": row.unit, "sample_count": row.sample_count, "unavailable_reason": row.unavailable_reason})
    configuration_diff = [summary["config_diff"] for summary in summaries]
    return {"summaries": summaries, "equity_curves": curves, "equity_curve_series": curves, "drawdown_curve_series": drawdowns, "metrics": metrics, "metric_matrix": metric_matrix, "configuration_diff": configuration_diff}

legacy_router = APIRouter(
    prefix="/api/admin/backtests/{run_id}",
    tags=["backtest-results-legacy"],
)

# The current deployment serves API @3; @4 permanently removes the old alias.
DATA_PREFLIGHT_API_VERSION = 3
DATA_PREFLIGHT_REDIRECT_SUNSET_VERSION = 4
DEFAULT_PREFLIGHT_CALENDAR_LIMIT = 32
DEFAULT_PREFLIGHT_DIFFERENCE_LIMIT = 100


def _page_or_error(page: CursorPage) -> dict[str, object]:
    return {
        "items": list(page.items),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "truncated": page.has_more,
    }


def _cursor_signing_key(request: Request) -> str:
    """Resolve the server-only cursor HMAC secret for this app instance.

    The signing key is a dedicated secret (``QF_CURSOR_SIGNING_KEY``) that
    clients never hold; deriving it from the API token would let any
    authenticated caller forge validly signed cursors.  Reading
    ``request.app.state.settings`` keeps cursors signed with exactly the
    configuration of the serving deployment, including
    ``create_app(settings=...)`` and test injections.
    """

    settings = getattr(request.app.state, "settings", None)
    signing_key = getattr(settings, "cursor_signing_key", None)
    if signing_key is None:
        raise RuntimeError("application settings are not attached to app.state")
    return signing_key.get_secret_value()


CursorSigningKey = Annotated[str, Depends(_cursor_signing_key)]


def _read_page(
    kind: str,
    *,
    run_id: UUID,
    limit: int,
    cursor: str | None,
    session: Session,
    signing_key: str,
    query_context: dict[str, object] | None = None,
    **filters: object,
) -> dict[str, object]:
    repository = BacktestResultRepository(
        session,
        # Use the deployment's dedicated cursor HMAC secret.  It is not the
        # API token, so possession of an API credential cannot forge cursors.
        cursor_signing_key=signing_key,
    )
    try:
        page = repository.read_page(
            kind,
            run_id=run_id,
            limit=limit,
            cursor=cursor,
            query_context=query_context,
            **filters,
        )
    except ResultFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CursorError as exc:
        # Invalid, tampered, version-mismatched, or query-incompatible
        # cursors are explicit parameter errors; never restart silently.
        raise HTTPException(
            status_code=400,
            detail=f"无效或不适配的游标：{exc}",
        ) from exc
    except (UnknownResultKindError, InternalResultNotVisibleError) as exc:
        # Do not reveal whether a UUID is unknown or belongs to an internal
        # run; both are outside the formal result visibility boundary.
        raise HTTPException(status_code=404, detail="正式回测结果不存在") from exc
    except ValueError as exc:
        # Covers the page-size policy and non-timezone-aware boundaries.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page_or_error(page)


@router.get("/steps", response_model=ResultCursorPage[BacktestStepItem])
def list_steps(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    phase: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List time steps in stable step-sequence order."""

    return _read_page(
        "steps", run_id=run_id, limit=limit, cursor=cursor, session=session, signing_key=signing_key, phase=phase
    )


@router.get("/decisions", response_model=ResultCursorPage[BacktestDecisionItem])
def list_decisions(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    mode: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List strategy decisions ordered by step, time, and decision id."""

    return _read_page(
        "decisions",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        mode=mode,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/orders", response_model=ResultCursorPage[BacktestOrderItem])
def list_orders(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    instrument_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query(min_length=1, max_length=24)] = None,
    side: Annotated[str | None, Query(min_length=1, max_length=8)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List orders ordered by submission time with id tie-breaking."""

    return _read_page(
        "orders",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        instrument_id=instrument_id,
        status=status,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/order-updates", response_model=ResultCursorPage[BacktestOrderUpdateItem])
def list_order_updates(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    status: Annotated[str | None, Query(min_length=1, max_length=24)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List order state transitions in update order."""

    return _read_page(
        "order_updates",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        status=status,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/fills", response_model=ResultCursorPage[BacktestFillItem])
def list_fills(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    instrument_id: Annotated[UUID | None, Query()] = None,
    side: Annotated[str | None, Query(min_length=1, max_length=8)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List simulated fills ordered by execution time and fill id."""

    return _read_page(
        "fills",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        instrument_id=instrument_id,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/positions", response_model=ResultCursorPage[BacktestPositionItem])
def list_positions(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    instrument_id: Annotated[UUID | None, Query()] = None,
    side: Annotated[str | None, Query(min_length=1, max_length=8)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List raw non-zero position snapshots; rows are never collapsed."""

    return _read_page(
        "positions",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        instrument_id=instrument_id,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/equity-curve", response_model=ResultCursorPage[BacktestEquityCurveItem])
def list_equity_curve(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    start_time: Annotated[datetime | None, Query()] = None,
    end_time: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List account valuation points ordered by valuation time and sequence."""

    return _read_page(
        "equity_curve",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/metrics", response_model=ResultCursorPage[BacktestMetricItem])
def list_metrics(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List metric values; unavailable metrics expose their reason."""

    return _read_page(
        "metrics",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
    )


@router.get("/analysis-summary", response_model=BacktestAnalysisSummaryItem)
def get_analysis_summary(
    run_id: Annotated[UUID, Path()],
    session: Session = Depends(get_db_session),
) -> object:
    """Return the run's frozen analysis summary without recomputation."""

    import json

    from app.backtesting.analysis_inputs import canonical_evidence_json

    repository = BacktestResultRepository(
        session,
        # The read path never builds cursors; a placeholder key only
        # satisfies the repository constructor contract.
        cursor_signing_key="internal:analysis-summary-read",
    )
    summary = repository.get_analysis_summary(run_id)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail="该运行没有分析摘要",
        )
    # Render domain objects (Decimals, dates) through the canonical JSON
    # contract; the schema layer then validates the wire shape.
    from dataclasses import fields as dataclass_fields

    plain = {
        field.name: getattr(summary, field.name)
        for field in dataclass_fields(summary)
    }
    return json.loads(canonical_evidence_json(plain))


def _project_preflight_item(
    row: object,
    *,
    compact: bool = True,
) -> dict[str, object]:
    """Flatten PIT evidence, capping default nested lists by contract.

    ``section=calendar|sessions`` explicitly requests full detail and calls
    this projection with ``compact=False``.  The ordinary report list is a
    bounded summary: it never silently returns an unbounded differences array
    merely because the outer result row itself is small.
    """

    item = BacktestDataPreflightItem.model_validate(row).model_dump()
    calendar_summary = item.get("calendar_summary")
    session_summary = item.get("session_summary")
    # Calendar and session evidence are separate projections.  Do not use a
    # truthiness fallback when compacting: an empty calendar summary must not
    # cause session evidence to be copied into the calendar field.
    summary = (
        calendar_summary
        if isinstance(calendar_summary, dict) and calendar_summary
        else session_summary
        if isinstance(session_summary, dict)
        else {}
    )
    # Keep the PIT source selected before compacting.  An empty calendar
    # summary is populated with count/truncation metadata below; using the
    # mutated mapping for PIT projection would hide a valid session context.
    pit_summary = summary
    if compact and isinstance(calendar_summary, dict):
        summary = dict(calendar_summary)
        raw_calendar_ids = summary.get("calendar_ids", ())
        raw_calendar_ids = list(raw_calendar_ids) if isinstance(raw_calendar_ids, (list, tuple)) else []
        raw_differences = summary.get("differences", ())
        raw_differences = list(raw_differences) if isinstance(raw_differences, (list, tuple)) else []
        summary["calendar_ids"] = raw_calendar_ids[:DEFAULT_PREFLIGHT_CALENDAR_LIMIT]
        summary["calendar_ids_count"] = len(raw_calendar_ids)
        summary["calendar_ids_truncated"] = len(raw_calendar_ids) > DEFAULT_PREFLIGHT_CALENDAR_LIMIT
        summary["calendar_ids_next_cursor"] = None
        summary["differences"] = raw_differences[:DEFAULT_PREFLIGHT_DIFFERENCE_LIMIT]
        summary["differences_count"] = len(raw_differences)
        summary["differences_truncated"] = len(raw_differences) > DEFAULT_PREFLIGHT_DIFFERENCE_LIMIT
        # The endpoint fills an opaque signed section cursor when the caller
        # has requested the ordinary list and a continuation is available.
        # Keeping the field in the nested summary makes truncation explicit
        # without pretending the bounded array is complete evidence.
        summary["differences_next_cursor"] = None
        item["calendar_summary"] = summary
    context = pit_summary.get("pit_context") if isinstance(pit_summary, dict) else None
    if not isinstance(context, dict):
        context = {}
    item.update(
        {
            "data_cutoff": context.get("data_cutoff"),
            "cutoff_local_date": context.get("cutoff_local_date"),
            "include_cutoff_day": context.get("include_cutoff_day"),
            "knowledge_as_of": context.get("knowledge_as_of"),
            "pit_profile": context.get("pit_profile"),
            "profile_version": context.get("profile_version"),
            "non_strict_pit": pit_summary.get("non_strict_pit") if isinstance(pit_summary, dict) else None,
            "non_strict_pit_capabilities": pit_summary.get("non_strict_pit_capabilities") if isinstance(pit_summary, dict) else None,
            "calendar_revision_digest": pit_summary.get("calendar_revision_digest") if isinstance(pit_summary, dict) else None,
            "snapshot_fingerprint": pit_summary.get("snapshot_fingerprint") if isinstance(pit_summary, dict) else None,
        }
    )
    return item


def _preflight_section_page(
    *,
    run_id: UUID,
    section: str,
    limit: int,
    cursor: str | None,
    session: Session,
    signing_key: str,
) -> dict[str, object]:
    """Page calendar/session evidence inside persisted report JSON.

    The result table contains at most one admission and one session report;
    section pagination therefore operates on their nested immutable evidence,
    not on report rows.  Each response item carries one detail entry together
    with its parent phase/hash so cursors cannot mix attempts.
    """

    base_page = _read_page(
        "data_preflight",
        run_id=run_id,
        limit=500,
        cursor=None,
        session=session,
        signing_key=signing_key,
    )
    flattened: list[tuple[dict[str, object], object]] = []
    report_hashes: list[str] = []
    for row in base_page["items"]:
        item = _project_preflight_item(row, compact=False)
        report_hashes.append(item["report_hash"])
        summary_name = "calendar_summary" if section == "calendar" else "session_summary"
        summary = item.get(summary_name) or {}
        entries: list[object] = []
        if isinstance(summary, dict):
            if section == "calendar":
                entries.extend(
                    {"kind": "difference", "value": value}
                    for value in summary.get("differences", ())
                )
                entries.extend(
                    {"kind": "definition_usage", "value": value}
                    for value in summary.get("definition_usage_by_date", ())
                )
                # Keep differences first so the continuation cursor exposed
                # by the compact list starts after the documented 100-item
                # difference prefix, even when calendar IDs are also present.
                entries.extend(
                    {"kind": "calendar_id", "value": value}
                    for value in summary.get("calendar_ids", ())
                )
            else:
                entries.extend(
                    {"kind": "formal_session", "value": value}
                    for value in summary.get("formal_sessions", ())
                )
                entries.extend(
                    {"kind": "warmup_session", "value": value}
                    for value in summary.get("warmup_sessions", ())
                )
        # Legacy reports may not have nested arrays.  Keep one parent summary
        # item so section readers still receive the old evidence unchanged.
        if not entries:
            entries = [{"kind": "summary", "value": summary}]
        flattened.extend((item, entry) for entry in entries)

    total = len(flattened)
    digest = compute_query_digest(
        {
            "kind": "backtest_data_preflight",
            "run_id": str(run_id),
            "section": section,
            "limit": limit,
            "report_hashes": report_hashes,
            "total": total,
        }
    )
    offset = 0
    if cursor is not None:
        parsed = parse_cursor(
            cursor,
            signing_key=signing_key,
            expected_query_digest=digest,
            key_kinds=("int",),
            upper_bound_columns={"total": "int"},
        )
        offset = parsed.last_sort_key[0]
        if parsed.query_upper_bound.get("total") != total or not 0 <= offset <= total:
            raise HTTPException(status_code=400, detail="无效或不适配的预检详情游标。")
    selected = flattened[offset : offset + limit]
    projected_items: list[dict[str, object]] = []
    for index, (item, entry) in enumerate(selected, start=offset):
        # A report may yield many detail entries; each response row must own
        # its projection instead of mutating the shared parent dictionary.
        item = dict(item)
        item["section"] = section
        if section == "calendar":
            item["session_summary"] = None
            item["calendar_summary"] = {
                "detail": entry,
                "detail_index": index,
                "detail_count": total,
            }
        else:
            item["calendar_summary"] = None
            item["session_summary"] = {
                "detail": entry,
                "detail_index": index,
                "detail_count": total,
            }
        projected_items.append(item)
    from app.backtesting.data.reports import canonical_json

    max_page_bytes = 256 * 1024
    while (
        len(projected_items) > 1
        and len(canonical_json(projected_items).encode("utf-8")) > max_page_bytes
    ):
        projected_items.pop()
    if projected_items and len(canonical_json(projected_items).encode("utf-8")) > max_page_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "reason_code": "calendar_preflight_resource_limit_exceeded",
                "issue_code": "CALENDAR_PREFLIGHT_RESOURCE_LIMIT_EXCEEDED",
                "message": "单条预检详情超过 256 KiB，请缩小报告范围后重试。",
                "limit_bytes": max_page_bytes,
            },
        )
    next_offset = offset + len(projected_items)
    has_more = next_offset < total
    next_cursor = (
        build_cursor(
            signing_key=signing_key,
            query_digest=digest,
            key_kinds=("int",),
            last_sort_key=(next_offset,),
            upper_bound_columns={"total": "int"},
            query_upper_bound={"total": total},
        )
        if has_more
        else None
    )
    return {
        "items": projected_items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "truncated": has_more,
    }


@router.get("/data-preflight", response_model=ResultCursorPage[BacktestDataPreflightItem])
def list_data_preflight(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    section: Annotated[str | None, Query()] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List reports, with optional calendar/session detail projection."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise HTTPException(status_code=422, detail="limit 必须介于 1 和 500 之间。")
    if section is not None and section not in {"calendar", "sessions"}:
        raise HTTPException(status_code=422, detail="section 必须为 calendar 或 sessions。")
    if section is not None and limit > 100:
        raise HTTPException(status_code=422, detail="section 查询的 limit 不能超过 100。")
    if section is not None:
        try:
            return _preflight_section_page(
                run_id=run_id,
                section=section,
                limit=limit,
                cursor=cursor,
                session=session,
                signing_key=signing_key,
            )
        except CursorError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"无效或不适配的游标：{exc}",
            ) from exc
    page = _read_page(
        "data_preflight",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
    )
    page["items"] = [_project_preflight_item(row, compact=True) for row in page["items"]]
    # A default report row carries only the bounded 100-difference summary.
    # If more detail exists, obtain the real signed cursor from the canonical
    # section paginator rather than exposing a synthetic or guessable token.
    for item in page["items"]:
        summary = item.get("calendar_summary")
        if not isinstance(summary, dict) or not summary.get("differences_truncated"):
            continue
        try:
            detail_page = _preflight_section_page(
                run_id=run_id,
                section="calendar",
                limit=DEFAULT_PREFLIGHT_DIFFERENCE_LIMIT,
                cursor=None,
                session=session,
                signing_key=signing_key,
            )
        except (HTTPException, CursorError):
            # The ordinary list remains usable even if a concurrent report
            # mutation makes a detail cursor unavailable.
            continue
        summary["differences_next_cursor"] = detail_page.get("next_cursor")
    return page


@legacy_router.api_route(
    "/data-preflight",
    methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def legacy_data_preflight_method_not_allowed() -> None:
    """Reject non-GET legacy calls without touching preflight data."""

    raise HTTPException(
        status_code=405,
        detail={
            "reason_code": "calendar_preflight_legacy_method_not_allowed",
            "message": "旧预检路径仅支持 GET 重定向。",
        },
    )


@legacy_router.get("/data-preflight", include_in_schema=False)
def legacy_data_preflight_redirect(
    run_id: Annotated[UUID, Path()],
    request: Request,
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    """Redirect the documented legacy alias without reading or writing data."""

    root = session.get(BacktestRunRecord, run_id)
    if root is None or root.run_kind != "backtest_run" or root.profile != "formal@1":
        raise HTTPException(status_code=404, detail="正式回测结果不存在")

    if DATA_PREFLIGHT_API_VERSION >= DATA_PREFLIGHT_REDIRECT_SUNSET_VERSION:
        raise HTTPException(
            status_code=410,
            detail={
                "reason_code": "calendar_preflight_redirect_sunset",
                "message": "旧预检路径已下线，请使用 canonical data-preflight 路径。",
            },
        )
    location = f"/api/admin/backtest-runs/{run_id}/results/data-preflight"
    if request.url.query:
        location += f"?{request.url.query}"
    return RedirectResponse(url=location, status_code=308)


@router.get("/data-chunks", response_model=ResultCursorPage[BacktestDataChunkItem])
def list_data_chunks(
    run_id: Annotated[UUID, Path()],
    signing_key: CursorSigningKey,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    phase: Annotated[str | None, Query(min_length=1, max_length=16)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List bounded data chunks in phase and chunk-sequence order."""

    return _read_page(
        "data_chunks",
        run_id=run_id,
        limit=limit,
        cursor=cursor,
        session=session,
        signing_key=signing_key,
        phase=phase,
    )


# Public contract aliases use ``/api/admin/backtests/{run_id}/...`` while the
# canonical preflight resource remains under ``backtest-runs/.../results``.
# These aliases delegate to the same guarded handlers and do not create a
# second persistence/query implementation.
formal_result_alias_router.add_api_route("/steps", list_steps, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/decisions", list_decisions, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/orders", list_orders, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/fills", list_fills, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/positions", list_positions, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/equity", list_equity_curve, methods=["GET"], include_in_schema=True)
formal_result_alias_router.add_api_route("/metrics", list_metrics, methods=["GET"], include_in_schema=True)
