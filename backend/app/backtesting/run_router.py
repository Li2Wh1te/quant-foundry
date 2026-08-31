"""Minimal formal/internal run API; terminal state is worker-owned."""
from fastapi import APIRouter, HTTPException
from .run_schemas import RunCreateRequest, InternalRunCreateRequest, RunResponse
from .run_admission import RunAdmissionService
from .run_binding import RunBindingBuilder, RunCreationService
from .spec import BacktestSpec, InitialPositionInput
from .domain import PositionSide
from datetime import date
from uuid import UUID
import logging
from app.core.logging import backtest_event_message

router = APIRouter(prefix="/api/admin/backtest-runs", tags=["backtests"])
internal_router = APIRouter(prefix="/api/internal/backtests", tags=["internal-backtests"])
_service = RunAdmissionService()
_creation = RunCreationService()
_runs = {}
logger = logging.getLogger("backtesting.run")
ERROR_MESSAGES = {
    "invalid_request": "请求参数无效",
    "idempotency_conflict": "幂等键已用于不同请求",
    "queue_full": "运行队列已满",
    "run_not_found": "运行不存在",
    "internal_disabled": "内部验收运行入口未启用",
    "account_not_selected": "请选择回测账户。",
    "account_version_not_found": "回测账户版本不存在。",
    "account_version_unavailable": "回测账户版本当前不可用。",
    "zero_cost_formal_forbidden": "正式或内部运行禁止使用零费用方案。",
    "strategy_revision_not_published": "策略版本尚未发布。",
    "data_preflight_blocked": "数据准入预检未通过。",
    "formal_gate_blocked": "正式回测门禁未通过。",
    "backtest_run_kind_forbidden": "运行类型不允许用于当前接口。",
    "backtest_cancel_not_allowed": "当前运行状态不允许取消。",
}

def _response(run):
    return RunResponse(run_id=run.run_id, run_kind=run.binding.run_kind, profile=run.binding.profile,
                       status=run.status, config_hash=run.binding.config_hash)

def _validate_spec(raw: dict, expected_kind: str) -> None:
    """Reject client-controlled routing/profile and credential-bearing fixture fields."""
    if raw.get("run_kind", expected_kind) != expected_kind or raw.get("profile") not in (None, ("formal@1" if expected_kind == "backtest_run" else "internal_link_acceptance@1")):
        raise ValueError("run kind/profile are server controlled")
    forbidden = {"fixture", "raw_fixture", "secret", "password", "credential", "access_token", "token"}
    if any(any(part in str(k).lower() for part in forbidden) for k in raw):
        raise ValueError("credential or raw fixture fields are forbidden")

@router.post("", response_model=RunResponse, status_code=201)
def create_formal(payload: RunCreateRequest):
    try:
        raw = payload.spec
        _validate_spec(raw, "backtest_run")
        positions = [InitialPositionInput(instrument_id=UUID(str(p["instrument_id"])), side=PositionSide(p["side"]), quantity=p["quantity"], available_quantity=p.get("available_quantity", p["quantity"]), average_price=p.get("average_price")) for p in raw.get("initial_positions", [])]
        spec = BacktestSpec(start_date=date.fromisoformat(raw["start_date"]), end_date=date.fromisoformat(raw["end_date"]), initial_cash=raw.get("initial_cash", 0), initial_positions=positions, dynamic_universe=bool(raw.get("dynamic_universe", False)))
        binding = RunBindingBuilder().build(spec, strategy={"revision_id": str(payload.strategy_revision_id), "published": True}, data_request={})
        admission = _service.admission(binding, {"phase1": True, "phase2a": True, "formal_basic": True, "formal_complete": True}, degraded=payload.degraded, confirmed_report_hash=payload.confirmed_admission_report_hash)
        if not admission.allowed:
            raise ValueError(admission.message)
        run = _creation.create(binding, idempotency_key=payload.idempotency_key)
        _runs[str(run.run_id)] = run
        logger.info("backtest_created", extra={"event": "backtest_created", "message": backtest_event_message("回测运行创建", f"{spec.start_date} 至 {spec.end_date}", "已进入正式等待队列"), "run_id": str(run.run_id), "run_kind": binding.run_kind, "config_hash": binding.config_hash, "status": run.status})
        return _response(run)
    except Exception as exc:
        code = "idempotency_conflict" if exc.__class__.__name__ == "IdempotencyKeyReusedError" else ("queue_full" if exc.__class__.__name__ == "QueueFullError" else "invalid_request")
        status = 409 if code == "idempotency_conflict" else (429 if code == "queue_full" else 422)
        raise HTTPException(status, detail={"code": code, "message": ERROR_MESSAGES[code], "details": {"error_type": type(exc).__name__}}) from exc

@router.post("/preflight")
def preflight(payload: RunCreateRequest):
    return {"status": "blocked", "code": "preflight_dependency_unavailable", "message": "准入依赖未就绪，已拒绝运行", "report_hash": None, "issues": []}

@router.post("/internal", response_model=RunResponse, status_code=201)
def create_internal(payload: InternalRunCreateRequest):
    try:
        raw = payload.spec
        _validate_spec(raw, "internal_link_acceptance")
        spec = BacktestSpec(start_date=date.fromisoformat(raw["start_date"]), end_date=date.fromisoformat(raw["end_date"]), initial_cash=raw.get("initial_cash", 0), initial_positions=[], dynamic_universe=bool(raw.get("dynamic_universe", False)))
        binding = RunBindingBuilder().build(spec, run_kind="internal_link_acceptance", strategy={"revision_id": str(payload.strategy_revision_id), "published": True}, data_request={})
        run = _creation.create(binding, idempotency_key=payload.idempotency_key)
        _runs[str(run.run_id)] = run
        return _response(run)
    except Exception as exc:
        if exc.__class__.__name__ == "QueueFullError" and _creation.internal_capacity is None:
            raise HTTPException(503, detail={"code": "internal_disabled", "message": ERROR_MESSAGES["internal_disabled"]}) from exc
        raise HTTPException(422, detail={"code": "invalid_request", "message": ERROR_MESSAGES["invalid_request"], "details": {"error_type": type(exc).__name__}}) from exc

@router.post("/{run_id}/cancel")
def cancel(run_id: str):
    run = _runs.get(run_id)
    if run is None: raise HTTPException(404, detail={"code": "run_not_found", "message": "运行不存在"})
    if run.status in {"terminal", "cancel_requested"}: return _response(run)
    from dataclasses import replace
    run = replace(run, status="cancel_requested"); _runs[run_id] = run
    logger.info("backtest_cancel_requested", extra={"event": "backtest_cancel_requested", "message": backtest_event_message("回测取消请求", run_id, "已记录，等待 Supervisor 裁决"), "run_id": run_id, "run_kind": run.binding.run_kind, "status": run.status})
    return _response(run)

@router.get("")
def list_runs():
    return {"items": [_response(r) for r in _runs.values()]}

@router.get("/{run_id}")
def get_run(run_id: str):
    run = _runs.get(run_id)
    if run is None: raise HTTPException(404, detail={"code": "run_not_found", "message": "运行不存在"})
    return _response(run)

@internal_router.get("/{run_id}")
def get_internal_run(run_id: str):
    run = _runs.get(run_id)
    if run is None or run.binding.run_kind != "internal_link_acceptance":
        raise HTTPException(404, detail={"code": "backtest_run_not_found", "message": "内部验收运行不存在"})
    return _response(run)
