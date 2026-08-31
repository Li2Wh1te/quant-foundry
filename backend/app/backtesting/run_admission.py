"""Fail-closed page admission facade."""
from dataclasses import dataclass
from typing import Any, Mapping
from .run_binding import GateOrchestrator, RunBinding, RunCreationService
from .data.reports import canonical_hash

@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    binding: RunBinding | None = None
    code: str | None = None
    message: str = ""
    report_hash: str | None = None
    status: str | None = None
    issues: tuple[Any, ...] = ()

class RunAdmissionService:
    def __init__(self, *, gate_orchestrator=None, creation=None, preflight_service=None, initial_position_preflight=None):
        self.gates = gate_orchestrator or GateOrchestrator()
        self.creation = creation or RunCreationService()
        self.preflight_service = preflight_service
        self.initial_position_preflight = initial_position_preflight

    def admission(self, binding: RunBinding, checks: Mapping[str, bool], *, degraded: bool = False, confirmed_report_hash: str | None = None, metric_checks: Mapping[str, bool] | None = None) -> AdmissionResult:
        # Non-zero opening positions require the dedicated six-dimension
        # preflight.  Missing service is fail-closed rather than guessing.
        if binding.spec.non_zero_initial_positions:
            initial_service = getattr(self, "initial_position_preflight", None)
            if initial_service is None:
                initial_service = getattr(self.preflight_service, "initial_position_service", None) if self.preflight_service is not None else None
            if initial_service is not None:
                try:
                    report = initial_service.run(binding.spec)
                    checks = {**checks, "initial_positions": getattr(report, "status", "blocked").value == "ready" if hasattr(getattr(report, "status", None), "value") else getattr(report, "status", "blocked") == "ready"}
                except Exception:
                    checks = {**checks, "initial_positions": False}
            elif not checks.get("initial_positions", False):
                checks = {**checks, "initial_positions": False}
        if binding.spec.non_zero_initial_positions and not checks.get("initial_positions", False):
            checks = {**checks, "phase1": False, "initial_positions": False}
        if self.preflight_service is not None:
            try:
                decision = self.preflight_service.admission(binding.data_request)
                checks = {**checks, "phase2a": bool(getattr(decision, "allowed", False))}
            except Exception:
                return AdmissionResult(False, code="preflight_dependency_unavailable", message="准入依赖未就绪，已拒绝运行", status="blocked")
        decision = self.gates.evaluate(run_kind=binding.run_kind, checks=checks, metric_checks=metric_checks)
        status = "degraded" if degraded and decision.allowed else ("ready" if decision.allowed else "blocked")
        report = {"run_kind": binding.run_kind, "checks": dict(sorted(checks.items())), "disabled_metrics": decision.disabled_metrics}
        report_hash = canonical_hash(report)
        if not decision.allowed:
            return AdmissionResult(False, code="preflight_blocked", message="页面准入预检未通过", report_hash=report_hash, status=status, issues=tuple(k for k,v in checks.items() if not v))
        if degraded and binding.run_kind == "internal_link_acceptance":
            return AdmissionResult(False, code="internal_degraded_forbidden", message="内部验收运行禁止 degraded 放行", report_hash=report_hash, status=status)
        if degraded and confirmed_report_hash != report_hash:
            return AdmissionResult(False, code="formal_degraded_confirmation_required", message="正式 degraded 运行需要确认预检报告", report_hash=report_hash, status=status)
        return AdmissionResult(True, binding=binding, message="页面准入预检通过", report_hash=report_hash, status=status)

    def create(self, binding: RunBinding, checks: Mapping[str, bool], *, idempotency_key: str):
        result = self.admission(binding, checks)
        if not result.allowed:
            raise PermissionError(result.message)
        return self.creation.create(binding, idempotency_key=idempotency_key)
