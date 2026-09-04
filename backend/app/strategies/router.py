"""Authenticated HTTP APIs for private database-backed strategy management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.strategies.repository import StrategyRepository
from app.strategies.schemas import (
    StrategyCreateRequest,
    StrategyDetailResponse,
    StrategyDraftResponse,
    StrategyDraftSaveRequest,
    StrategyDraftValidationResponse,
    StrategyMetadataUpdateRequest,
    StrategyPublishRequest,
    StrategyRevisionResponse,
    StrategyRevisionSummaryResponse,
    StrategySummaryResponse,
    StrategyValidationIssueResponse,
    StrategyBacktestWorkspaceResponse,
)
from app.backtesting.run_repository import DatabaseRunRepository, FORMAL_KIND
from app.backtesting.run_router import _owner_scope, _response as _run_response
from app.backtesting.pagination import CursorError
from app.backtesting.result_router import _cursor_signing_key
from app.strategies.service import (
    StrategyAlreadyArchivedError,
    StrategyArchivedError,
    StrategyDraftConflictError,
    StrategyDraftIntegrityError,
    StrategyDraftNotFoundError,
    StrategyDraftValidationError,
    StrategyMetadataConflictError,
    StrategyNotFoundError,
    StrategyStorageService,
    StrategyStorageValidationError,
)
from app.strategies.validation import StrategyValidationIssue


router = APIRouter(prefix="/api/admin/strategies", tags=["admin-strategies"])


@router.get("/{strategy_id}/backtests", response_model=StrategyBacktestWorkspaceResponse)
def strategy_backtest_workspace(
    strategy_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
    signing_key: Annotated[str, Depends(_cursor_signing_key)],
    request: Request = None,  # type: ignore[assignment]
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> StrategyBacktestWorkspaceResponse:
    """Compose strategy metadata, published revisions and formal runs."""
    strategy = _strategy_or_404(session, strategy_id)
    repository = StrategyRepository(session)
    revisions = repository.list_revisions(strategy_id)
    draft = repository.get_draft(strategy_id)
    try:
        page = DatabaseRunRepository(session).list_page(
            queue_kind=FORMAL_KIND,
            strategy_id=str(strategy_id),
            owner_scope=_owner_scope(request),
            limit=limit,
            cursor=cursor,
            signing_key=signing_key,
        )
    except CursorError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor", "message": str(exc)},
        ) from exc
    rows = page.items
    current = next((r for r in revisions if r.id == strategy.current_revision_id), None)
    formal_gate: dict[str, object] = {
        "status": "待配置" if current is None else "待预检",
        "allowed": False,
        "blocking_issues": [],
        "metric_decisions": [],
        "checked_at": None,
    }
    if current is not None:
        for row in rows:
            candidate = getattr(row, "formal_gate_evidence", None)
            if not isinstance(candidate, dict):
                data_evidence = getattr(row, "data_evidence", {})
                evidence = data_evidence if isinstance(data_evidence, dict) else {}
                candidate = evidence.get("formal_gates")
            if isinstance(candidate, dict) and candidate:
                formal_gate = candidate
                break
    return StrategyBacktestWorkspaceResponse(
        strategy={
            "id": strategy.id, "name": strategy.name, "state": strategy.state,
            "current_revision_id": strategy.current_revision_id,
            "draft_version": draft.version if draft else None,
            "draft_changed_since_revision": bool(draft and current and draft.source_hash != current.source_hash),
        },
        published_revisions=[{
            "id": r.id, "revision_number": r.revision_number, "source_hash": r.source_hash,
            "parameter_schema": r.parameter_schema, "default_parameters": r.default_parameters,
            "runtime_manifest": r.runtime_manifest, "published_at": r.published_at,
        } for r in revisions],
        formal_gate=formal_gate,
        runs={"items": [_run_response(r) for r in rows],
              "next_cursor": page.next_cursor,
              "has_more": page.has_more,
              "query_summary": {"strategy_id": str(strategy_id), "run_kind": FORMAL_KIND}},
    )


@router.get("", response_model=list[StrategySummaryResponse])
def list_strategies(
    session: Annotated[Session, Depends(get_db_session)],
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StrategySummaryResponse]:
    """List strategy identities without transferring private source text."""
    strategies = StrategyRepository(session).list_strategies(
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return [_strategy_summary_response(strategy) for strategy in strategies]


@router.post(
    "", response_model=StrategyDetailResponse, status_code=status.HTTP_201_CREATED
)
def create_strategy(
    payload: StrategyCreateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategyDetailResponse:
    """Persist a private strategy and an editable draft without publishing it."""
    try:
        strategy = StrategyStorageService(session).create_strategy(
            name=payload.name,
            description=payload.description,
            source_code=payload.source_code,
            parameter_schema=payload.parameter_schema,
            default_parameters=payload.default_parameters,
        )
        session.commit()
        session.refresh(strategy)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return _strategy_detail_response(session, strategy)


@router.get("/{strategy_id}", response_model=StrategyDetailResponse)
def get_strategy(
    strategy_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategyDetailResponse:
    """Return one private strategy's editable draft and current revision metadata."""
    return _strategy_detail_response(session, _strategy_or_404(session, strategy_id))


@router.patch("/{strategy_id}", response_model=StrategySummaryResponse)
def update_strategy_metadata(
    strategy_id: UUID,
    payload: StrategyMetadataUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategySummaryResponse:
    """Update a private strategy name or description with optimistic locking."""
    fields = payload.model_fields_set
    metadata: dict[str, object] = {}
    if "name" in fields:
        metadata["name"] = payload.name
    if "description" in fields:
        metadata["description"] = payload.description
    try:
        strategy = StrategyStorageService(session).update_strategy_metadata(
            strategy_id,
            expected_version=payload.version,
            **metadata,
        )
        session.commit()
        session.refresh(strategy)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return _strategy_summary_response(strategy)


@router.patch("/{strategy_id}/draft", response_model=StrategyDraftResponse)
def save_strategy_draft(
    strategy_id: UUID,
    payload: StrategyDraftSaveRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategyDraftResponse:
    """Patch a private draft only when the editor version still matches."""
    fields = payload.model_fields_set
    draft_fields: dict[str, object] = {}
    if "source_code" in fields:
        draft_fields["source_code"] = payload.source_code
    if "parameter_schema" in fields:
        draft_fields["parameter_schema"] = payload.parameter_schema
    if "default_parameters" in fields:
        draft_fields["default_parameters"] = payload.default_parameters
    try:
        draft = StrategyStorageService(session).save_draft(
            strategy_id,
            expected_version=payload.version,
            **draft_fields,
        )
        session.commit()
        session.refresh(draft)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return StrategyDraftResponse.model_validate(draft)


@router.post(
    "/{strategy_id}/validate", response_model=StrategyDraftValidationResponse
)
def validate_strategy_draft(
    strategy_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategyDraftValidationResponse:
    """Run static source validation without importing or executing private code."""
    try:
        draft, validation = StrategyStorageService(session).validate_draft(strategy_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    return StrategyDraftValidationResponse(
        valid=validation.valid,
        draft_version=draft.version,
        source_hash=draft.source_hash,
        issues=[_validation_issue_response(issue) for issue in validation.issues],
    )


@router.post(
    "/{strategy_id}/publish", response_model=StrategyRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def publish_strategy_revision(
    strategy_id: UUID,
    payload: StrategyPublishRequest,
    session: Annotated[Session, Depends(get_db_session)],
    response: Response,
) -> StrategyRevisionResponse:
    """Publish an immutable revision after revalidating the locked draft."""
    try:
        service = StrategyStorageService(session)
        revision = service.publish_revision(
            strategy_id,
            expected_draft_version=payload.draft_version,
        )
        session.commit()
        session.refresh(revision)
        if service.last_publish_reused:
            response.status_code = status.HTTP_200_OK
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return StrategyRevisionResponse.model_validate(revision)


@router.get(
    "/{strategy_id}/revisions", response_model=list[StrategyRevisionSummaryResponse]
)
def list_strategy_revisions(
    strategy_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
) -> list[StrategyRevisionSummaryResponse]:
    """List immutable revision audit metadata without returning every source body."""
    _strategy_or_404(session, strategy_id)
    revisions = StrategyRepository(session).list_revisions(strategy_id)
    return [StrategyRevisionSummaryResponse.model_validate(item) for item in revisions]


@router.get(
    "/{strategy_id}/revisions/{revision_number}",
    response_model=StrategyRevisionResponse,
)
def get_strategy_revision(
    strategy_id: UUID,
    revision_number: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_db_session)],
) -> StrategyRevisionResponse:
    """Return one authorized immutable source snapshot for review or comparison."""
    _strategy_or_404(session, strategy_id)
    revision = StrategyRepository(session).get_revision_by_number(
        strategy_id, revision_number
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="策略版本不存在。")
    return StrategyRevisionResponse.model_validate(revision)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_strategy(
    strategy_id: UUID,
    version: Annotated[int, Query(ge=1)],
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    """Archive future edits while retaining all private source and audit history."""
    try:
        StrategyStorageService(session).archive_strategy(
            strategy_id, expected_version=version
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _strategy_or_404(session: Session, strategy_id: UUID) -> Strategy:
    """Load the strategy identity once for read-only endpoints."""
    strategy = StrategyRepository(session).get_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在。")
    return strategy


def _strategy_detail_response(
    session: Session, strategy: Strategy
) -> StrategyDetailResponse:
    """Build an editor response while detecting broken storage invariants early."""
    repository = StrategyRepository(session)
    draft = repository.get_draft(strategy.id)
    if draft is None:
        raise HTTPException(status_code=409, detail="策略草稿不存在。")
    current_revision = _current_revision_or_conflict(repository, strategy)
    return StrategyDetailResponse(
        **_strategy_summary_response(strategy).model_dump(),
        draft=StrategyDraftResponse.model_validate(draft),
        current_revision=(
            StrategyRevisionSummaryResponse.model_validate(current_revision)
            if current_revision is not None
            else None
        ),
    )


def _current_revision_or_conflict(
    repository: StrategyRepository, strategy: Strategy
) -> StrategyRevision | None:
    """Ensure a non-null current pointer resolves to its owning revision row."""
    if strategy.current_revision_id is None:
        return None
    revision = repository.get_revision(strategy.current_revision_id)
    if revision is None or revision.strategy_id != strategy.id:
        raise HTTPException(status_code=409, detail="策略当前版本记录不完整。")
    return revision


def _strategy_summary_response(strategy: Strategy) -> StrategySummaryResponse:
    """Convert ORM metadata without exposing the source held in its draft row."""
    return StrategySummaryResponse.model_validate(strategy)


def _validation_issue_response(
    issue: StrategyValidationIssue,
) -> StrategyValidationIssueResponse:
    """Keep static validation messages structured for the future strategy editor."""
    return StrategyValidationIssueResponse(
        code=issue.code,
        message=issue.message,
        line=issue.line,
        column=issue.column,
    )


def _http_error(exc: Exception) -> HTTPException:
    """Map expected lifecycle failures to safe, user-facing Chinese API errors."""
    if isinstance(exc, StrategyNotFoundError):
        return HTTPException(status_code=404, detail="策略不存在。")
    if isinstance(exc, StrategyDraftNotFoundError):
        return HTTPException(status_code=409, detail="策略草稿不存在。")
    if isinstance(exc, StrategyDraftConflictError):
        return HTTPException(
            status_code=409, detail="策略草稿已更新，请刷新后重试。"
        )
    if isinstance(exc, StrategyMetadataConflictError):
        return HTTPException(
            status_code=409, detail="策略信息已更新，请刷新后重试。"
        )
    if isinstance(exc, StrategyArchivedError):
        return HTTPException(status_code=409, detail="已归档的策略不能修改或发布。")
    if isinstance(exc, StrategyAlreadyArchivedError):
        return HTTPException(status_code=409, detail="策略已经归档。")
    if isinstance(exc, StrategyDraftIntegrityError):
        return HTTPException(
            status_code=409, detail="策略草稿完整性校验失败，请重新保存草稿。"
        )
    if isinstance(exc, StrategyDraftValidationError):
        return HTTPException(
            status_code=422,
            detail={
                "message": "策略草稿未通过校验。",
                "issues": [
                    _validation_issue_response(issue).model_dump()
                    for issue in exc.issues
                ],
            },
        )
    if isinstance(exc, StrategyStorageValidationError):
        return HTTPException(status_code=422, detail="策略内容不符合存储要求。")
    return HTTPException(status_code=500, detail="策略操作失败。")
