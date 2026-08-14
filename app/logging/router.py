from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.logging.query import LocalLogQuery, LogFilters, StatusClass


router = APIRouter(prefix="/api/admin/logs", tags=["admin-logs"])


def get_log_query(request: Request) -> LocalLogQuery:
    return LocalLogQuery(
        request.app.state.settings.log_dir,
        request.app.state.settings.log_query_max_files,
    )


@router.get("")
def search_logs(
    log_query: Annotated[LocalLogQuery, Depends(get_log_query)],
    keyword: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    | None = None,
    status_class: StatusClass | None = None,
    path: Annotated[str | None, Query(max_length=500)] = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    effective_start = _as_utc(start_time) or datetime.now(UTC) - timedelta(days=1)
    effective_end = _as_utc(end_time)
    if effective_end is not None and effective_end < effective_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_time must not be earlier than start_time",
        )
    if (effective_end or datetime.now(UTC)) - effective_start > timedelta(days=31):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="log query time range must not exceed 31 days",
        )
    filters = LogFilters(
        keyword=keyword,
        level=level,
        method=method,
        status_class=status_class,
        path=path,
        start_time=effective_start,
        end_time=effective_end,
    )
    return log_query.search(filters, limit)


@router.post("/clear", status_code=status.HTTP_200_OK)
def clear_visible_logs(
    log_query: Annotated[LocalLogQuery, Depends(get_log_query)],
) -> dict[str, str]:
    visible_after = log_query.clear_visible_logs()
    return {"visible_after": visible_after.isoformat()}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
