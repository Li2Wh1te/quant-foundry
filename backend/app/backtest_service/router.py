"""Authenticated v2 REST with explicit legacy dispatch and bounded results."""
import json
import math
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select

from app.core.auth import AuthenticatedPrincipal, require_api_token, require_internal_backtest_token
from app.db.session import get_engine
from .history.router import _cursor_signing_key
from .models import Run
from .repository import DatabaseRunRepository, summary
from .runtime_contracts import RuntimeFault, configured_budgets
from .service import FormalProvider, RunService, normalize
from .serialization import encoded
from .openapi import RUN_BODY, BATCH_BODY, COMPARE_BODY

router = APIRouter(prefix="/api/admin/backtests", tags=["S3 backtests"], dependencies=[Depends(require_api_token)])
retired_router = APIRouter(dependencies=[Depends(require_api_token)])


def owner(request):
    principal = getattr(request.state, "authenticated_principal", None)
    if not isinstance(principal, AuthenticatedPrincipal):
        raise HTTPException(401, "需要有效身份")
    return principal.owner_scope


def service(request: Request):
    settings = request.app.state.settings
    limits, budget = configured_budgets(settings.s3_worker_limits, settings.s3_group_budget)
    repository = getattr(request.app.state, "s3_repository", None) or DatabaseRunRepository(
        get_engine(), budget=budget, result_root=settings.s3_result_root)
    if settings.s3_group_budget is not None and repository.budget != budget:
        raise fault(RuntimeFault('RESOURCE_LIMIT', 'API运行仓储与可信整组预算不一致'))
    provider = getattr(request.app.state, "s3_provider", None) or FormalProvider(settings, engine=repository.engine)
    return RunService(repository, provider, limits=limits, accepting=settings.s3_accept_new_runs)


def fault(error):
    code = getattr(error, "code", "INVALID_CONTRACT")
    status = (410 if code == "LEGACY_ENGINE_RETIRED" else 404 if code == "DATA_RESTRICTED"
              else 409 if code in ("IDEMPOTENCY_CONFLICT", "DATA_CHANGED", "RESULT_NOT_READY") else 503 if code == "CAPABILITY_UNAVAILABLE"
              else 429 if code == "RESOURCE_LIMIT" else 422)
    detail = error.diagnostic() if isinstance(error, RuntimeFault) else dict(code=code, operation="run_service", message="请求或结果不符合运行契约", scope={})
    return HTTPException(status, detail)


async def body(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk) > 1024*1024:
            raise HTTPException(413, dict(code="RESOURCE_LIMIT", message="控制消息超过1MiB"))
        data.extend(chunk)
    try:
        def constant(value):
            raise ValueError()
        def floating(value):
            v = float(value)
            if not math.isfinite(v):
                raise ValueError()
            return v
        def object_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        return json.loads(data, parse_constant=constant, parse_float=floating, object_pairs_hook=object_pairs)
    except (ValueError, RecursionError, UnicodeError):
        raise HTTPException(422, dict(code="INVALID_RUN_CONFIG", message="需要有限且无重复字段的JSON")) from None


def dispatch(payload):
    try:
        return normalize(payload)
    except (RuntimeFault, ValueError) as error:
        raise fault(error) from None


@router.get("/capabilities")
def capabilities(s: RunService = Depends(service)):
    return s.capabilities()


@router.post("/preflight", openapi_extra=RUN_BODY)
async def preflight(request: Request, s: RunService = Depends(service)):
    return s.preflight(owner(request), dispatch(await body(request)))


@router.post("", status_code=202, openapi_extra=RUN_BODY)
@router.post("/", status_code=202, include_in_schema=False)
async def submit(request: Request, s: RunService = Depends(service), key: str | None = Header(None, alias="Idempotency-Key")):
    config = dispatch(await body(request))
    try:
        return s.submit(owner(request), config, key)
    except RuntimeFault as error:
        raise fault(error) from None


@router.post("/batches", status_code=202, openapi_extra=BATCH_BODY)
async def batch(request: Request, s: RunService = Depends(service), key: str | None = Header(None, alias="Idempotency-Key")):
    value = await body(request)
    if type(value) is not dict or set(value) != {"configs"} or type(value["configs"]) is not list or not 1 <= len(value["configs"]) <= 256:
        raise HTTPException(422, "批次须为1..256项显式configs数组")
    configs = [dispatch(config) for config in value["configs"]]
    try:
        batch_id = s.batch(owner(request), configs, key)
        return dict(batch_id=batch_id, **batch_page(s, owner(request), batch_id, 100, None, _cursor_signing_key(request)))
    except RuntimeFault as error:
        raise fault(error) from None


def batch_page(s, o, batch_id, limit, cursor, key):
    from .cursors import decode, encode
    scope = dict(owner=o, batch_id=batch_id, limit=limit)
    offset = decode(cursor, scope, key) if cursor else 0
    rows, more = s.repository.batch_rows(o, batch_id, offset=offset, limit=limit)
    # Item count alone does not bound pages when accepted fee/parameter JSON is
    # large. Keep room for the signed cursor and advance only over emitted rows.
    items, used = [], 0
    for row in rows:
        size = len(encoded(row)) + 1
        if used + size > 1024*1024 - 16*1024:
            if not items:
                raise RuntimeFault("RESOURCE_LIMIT", "单项运行摘要超过控制消息预算")
            more = True
            break
        items.append(row)
        used += size
    return dict(items=items, has_more=more, next_cursor=encode(offset+len(items), scope, key) if more else None)


@router.get("/batches/{batch_id}")
def batch_status(batch_id: str, request: Request, s: RunService = Depends(service), limit: int = Query(100, ge=1, le=256), cursor: str | None = None):
    try:
        return dict(batch_id=batch_id, **batch_page(s, owner(request), batch_id, limit, cursor, _cursor_signing_key(request)))
    except RuntimeFault as error:
        raise fault(error) from None


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, request: Request, s: RunService = Depends(service)):
    try:
        s.repository.cancel_batch(owner(request), batch_id)
        return batch_status(batch_id, request, s, 100, None)
    except RuntimeFault as error:
        raise fault(error) from None


@router.post("/batches/{batch_id}/retry-failed", status_code=202)
def retry_batch(batch_id: str, request: Request, s: RunService = Depends(service), key: str | None = Header(None, alias="Idempotency-Key")):
    try:
        s.require_admission()
        ids = s.repository.retry_failed(owner(request), batch_id, key)
        return dict(batch_id=batch_id, run_ids=ids)
    except RuntimeFault as error:
        raise fault(error) from None


@router.post("/compare", openapi_extra=COMPARE_BODY)
async def compare(request: Request, s: RunService = Depends(service)):
    from .results import compare_runs
    value = await body(request)
    if type(value) is not dict or set(value) != {"run_ids"} or type(value["run_ids"]) is not list or not 2 <= len(value["run_ids"]) <= 16 or any(type(i) is not str for i in value["run_ids"]) or len(set(value["run_ids"])) != len(value["run_ids"]):
        raise HTTPException(422, "比较须为2..16个不同run_ids")
    try:
        return compare_runs(s.repository, owner(request), value["run_ids"], _cursor_signing_key(request))
    except RuntimeFault as error:
        raise fault(error) from None


@router.get("")
def list_runs(request: Request, s: RunService = Depends(service), limit: int = Query(100, ge=1, le=500), cursor: str | None = None):
    from .listing import list_page
    try:
        return list_page(s.repository, owner(request), limit, cursor, _cursor_signing_key(request))
    except RuntimeFault as error:
        raise fault(error) from None


@router.get("/{run_id}")
def status(run_id: str, request: Request, s: RunService = Depends(service)):
    from .history.runs import read_run
    try:
        return read_run(s.repository, owner(request), run_id)
    except RuntimeFault as error:
        raise fault(error) from None


@router.post("/{run_id}/cancel")
def cancel(run_id: str, request: Request, s: RunService = Depends(service)):
    try:
        return s.repository.request_cancel(owner(request), run_id)
    except RuntimeFault as error:
        raise fault(error) from None


@router.get("/{run_id}/results/{kind}")
def result(run_id: str, kind: str, request: Request, s: RunService = Depends(service), limit: int = Query(100, ge=1, le=10000), cursor: str | None = None):
    from .results import read_result
    try:
        return read_result(s.repository, owner(request), run_id, kind, limit, cursor, _cursor_signing_key(request))
    except (RuntimeFault, ValueError) as error:
        raise fault(error) from None


def retired():
    raise fault(RuntimeFault("LEGACY_ENGINE_RETIRED", "旧创建、取消、重跑及内部执行接口已退役"))


for path in ("/api/admin/backtest-runs", "/api/admin/backtest-runs/preflight",
             "/api/admin/backtest-runs/{run_id}/cancel", "/api/admin/backtest-runs/{run_id}/rerun",
             "/api/admin/backtests/{run_id}/rerun"):
    retired_router.add_api_route(path, retired, methods=["POST"])
for path in ("/api/internal/backtests", "/api/internal/backtests/{run_id}", "/api/internal/backtests/{run_id}/cancel"):
    for method in ("GET", "POST"):
        retired_router.add_api_route(path, retired, methods=[method], dependencies=[Depends(require_internal_backtest_token)])
