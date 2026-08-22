"""Authenticated HTTP APIs for account-profile CRUD and selector search."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.schemas import (
    AccountProfileCreateRequest,
    AccountProfileResponse,
    AccountProfileUpdateRequest,
    FeeRuleResponse,
    FeeScheduleResponse,
)
from app.backtesting.service import (
    AccountProfileNameConflictError,
    AccountProfileNotFoundError,
    AccountProfileService,
    AccountProfileValidationError,
    fee_schedule_from_record,
)
from app.db.session import get_db_session


router = APIRouter(
    prefix="/api/admin/backtest-account-profiles",
    tags=["admin-backtest-account-profiles"],
)


@router.get("", response_model=list[AccountProfileResponse])
def list_account_profiles(
    session: Annotated[Session, Depends(get_db_session)],
    status_filter: Annotated[str | None, Query(alias="status", min_length=1, max_length=16)] = None,
    name: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AccountProfileResponse]:
    """List profiles; pass ``name`` to filter by the visible account name."""

    service = AccountProfileService(session)
    try:
        records = service.list(
            status=_status_or_none(status_filter),
            name_query=name,
            limit=limit,
            offset=offset,
        )
    except AccountProfileValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [_response(record) for record in records]


@router.post(
    "",
    response_model=AccountProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_account_profile(
    payload: AccountProfileCreateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> AccountProfileResponse:
    """Create a named account and its explicitly bound fee schedule."""

    try:
        record = AccountProfileService(session).create(
            name=payload.name,
            status=payload.status,
            fee_schedule=payload.fee_schedule.model_dump(),
            metadata=payload.metadata,
        )
        session.commit()
        session.refresh(record)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return _response(record)


@router.get("/{profile_id}", response_model=AccountProfileResponse)
def get_account_profile(
    profile_id: Annotated[UUID, Path()],
    session: Annotated[Session, Depends(get_db_session)],
) -> AccountProfileResponse:
    """Return one account profile and its complete fee configuration."""

    try:
        return _response(AccountProfileService(session).get(profile_id))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.patch("/{profile_id}", response_model=AccountProfileResponse)
def update_account_profile(
    profile_id: Annotated[UUID, Path()],
    payload: AccountProfileUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> AccountProfileResponse:
    """Edit an account name, lifecycle, metadata, or bound fee schedule."""

    fields = payload.model_fields_set
    try:
        record = AccountProfileService(session).update(
            profile_id,
            name=payload.name if "name" in fields else None,
            status=payload.status if "status" in fields else None,
            fee_schedule=(
                payload.fee_schedule.model_dump()
                if "fee_schedule" in fields and payload.fee_schedule is not None
                else None
            ),
            metadata=payload.metadata if "metadata" in fields else None,
        )
        session.commit()
        session.refresh(record)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return _response(record)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account_profile(
    profile_id: Annotated[UUID, Path()],
    session: Annotated[Session, Depends(get_db_session)],
) -> None:
    """Delete an editable catalogue row; no historical run row is touched."""

    try:
        AccountProfileService(session).delete(profile_id)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


def _status_or_none(value: str | None):
    """Normalize query status while keeping invalid values as HTTP 422."""

    if value is None:
        return None
    from app.backtesting.account_profiles import AccountProfileStatus

    try:
        return AccountProfileStatus(value)
    except ValueError as exc:
        raise AccountProfileValidationError("账户状态不受支持") from exc


def _response(record: BacktestAccountProfileRecord) -> AccountProfileResponse:
    """Map JSONB fee rules back through the domain boundary before responding."""

    schedule = fee_schedule_from_record(record)
    return AccountProfileResponse(
        id=record.id,
        name=record.name,
        status=record.status,
        fee_schedule=FeeScheduleResponse(
            key=schedule.key,
            fee_rules=[
                FeeRuleResponse.model_validate(
                    {
                        "key": rule.key,
                        "category": rule.category,
                        "side": rule.side,
                        "rate": rule.rate,
                        "minimum": rule.minimum,
                        "fixed_amount": rule.fixed_amount,
                        "rounding_level": rule.rounding_level,
                        "rounding_scope": rule.rounding_scope,
                        "rounding_mode": rule.rounding_mode,
                        "rounding_precision": rule.rounding_precision,
                        "applicability": dict(rule.applicability),
                    }
                )
                for rule in schedule.fee_rules
            ],
            metadata=dict(schedule.metadata),
        ),
        metadata=dict(record.profile_metadata),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _http_error(exc: Exception) -> HTTPException:
    """Map expected storage failures to safe Chinese API responses."""

    if isinstance(exc, AccountProfileNotFoundError):
        return HTTPException(status_code=404, detail="账户档案不存在。")
    if isinstance(exc, AccountProfileNameConflictError):
        return HTTPException(status_code=409, detail="账户名称已存在。")
    if isinstance(exc, AccountProfileValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="账户名称已存在或账户配置违反约束。")
    return HTTPException(status_code=500, detail="账户档案操作失败。")


__all__ = ["router"]
