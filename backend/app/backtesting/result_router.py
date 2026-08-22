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

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.backtesting.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorError,
    CursorPage,
)
from app.backtesting.result_repository import (
    BacktestResultRepository,
    ResultFilterError,
    UnknownResultKindError,
)
from app.backtesting.result_schemas import (
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


def _page_or_error(page: CursorPage) -> dict[str, object]:
    return {
        "items": list(page.items),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def _read_page(kind: str, *, run_id: UUID, limit: int, cursor: str | None, session: Session, **filters: object) -> dict[str, object]:
    try:
        page = BacktestResultRepository(session).read_page(
            kind, run_id=run_id, limit=limit, cursor=cursor, **filters
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
    except UnknownResultKindError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Covers the page-size policy and non-timezone-aware boundaries.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _page_or_error(page)


@router.get("/steps", response_model=ResultCursorPage[BacktestStepItem])
def list_steps(
    run_id: Annotated[UUID, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    phase: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List time steps in stable step-sequence order."""

    return _read_page(
        "steps", run_id=run_id, limit=limit, cursor=cursor, session=session, phase=phase
    )


@router.get("/decisions", response_model=ResultCursorPage[BacktestDecisionItem])
def list_decisions(
    run_id: Annotated[UUID, Path()],
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
        mode=mode,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/orders", response_model=ResultCursorPage[BacktestOrderItem])
def list_orders(
    run_id: Annotated[UUID, Path()],
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
        instrument_id=instrument_id,
        status=status,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/order-updates", response_model=ResultCursorPage[BacktestOrderUpdateItem])
def list_order_updates(
    run_id: Annotated[UUID, Path()],
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
        status=status,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/fills", response_model=ResultCursorPage[BacktestFillItem])
def list_fills(
    run_id: Annotated[UUID, Path()],
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
        instrument_id=instrument_id,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/positions", response_model=ResultCursorPage[BacktestPositionItem])
def list_positions(
    run_id: Annotated[UUID, Path()],
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
        instrument_id=instrument_id,
        side=side,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/equity-curve", response_model=ResultCursorPage[BacktestEquityCurveItem])
def list_equity_curve(
    run_id: Annotated[UUID, Path()],
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
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/metrics", response_model=ResultCursorPage[BacktestMetricItem])
def list_metrics(
    run_id: Annotated[UUID, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List metric values; unavailable metrics expose their reason."""

    return _read_page("metrics", run_id=run_id, limit=limit, cursor=cursor, session=session)


@router.get("/data-preflight", response_model=ResultCursorPage[BacktestDataPreflightItem])
def list_data_preflight(
    run_id: Annotated[UUID, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """List run-level data preflight reports by phase."""

    return _read_page(
        "data_preflight", run_id=run_id, limit=limit, cursor=cursor, session=session
    )


@router.get("/data-chunks", response_model=ResultCursorPage[BacktestDataChunkItem])
def list_data_chunks(
    run_id: Annotated[UUID, Path()],
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
        phase=phase,
    )
