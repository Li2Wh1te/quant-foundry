"""Fail-closed page admission facade."""
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from .data.reports import canonical_hash
from .run_binding import Gate, GateDecision, GateOrchestrator, RunBinding, RunCreationService


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    binding: RunBinding | None = None
    code: str | None = None
    message: str = ""
    report_hash: str | None = None
    status: str | None = None
    issues: tuple[Any, ...] = ()
    formal_gates: Mapping[str, Any] | None = None


def build_gate_evidence(
    decision: GateDecision,
    *,
    report_hash: str | None,
    report_status: object | None = None,
    issues: Sequence[Any] = (),
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the small immutable audit projection shared by API and workers.

    The gate evaluator deliberately remains boolean and side-effect free.  The
    projection adds the status, timestamp, report hash, and operator-readable
    issue payload without changing the canonical preflight hash.
    """

    status = getattr(report_status, "value", report_status)
    status = str(status) if status is not None else None
    checked = (checked_at or datetime.now(UTC)).isoformat()
    issue_payload = []
    for issue in issues:
        if hasattr(issue, "as_dict") and callable(issue.as_dict):
            issue_payload.append(issue.as_dict())
        elif isinstance(issue, Mapping):
            issue_payload.append(dict(issue))
        else:
            issue_payload.append(str(issue))

    reported_status = status if status in {"ready", "degraded", "blocked"} else None
    admission_status = "blocked" if not decision.allowed else (reported_status or "ready")
    gates: dict[str, dict[str, Any]] = {}
    for gate in Gate:
        passed = bool(decision.checks.get(gate.value, False))
        gate_status = (
            "blocked"
            if not passed
            else "degraded"
            if reported_status == "degraded"
            and gate in {Gate.PHASE2A, Gate.FORMAL_BASIC, Gate.FORMAL_COMPLETE}
            else "ready"
        )
        gates[gate.value] = {
            "allowed": passed,
            "status": gate_status,
            "checked_at": checked,
            "report_hash": report_hash,
            "issues": issue_payload if not passed else [],
        }
    return {
        "schema_version": 1,
        "status": admission_status,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "checks": dict(sorted(decision.checks.items())),
        "disabled_metrics": list(decision.disabled_metrics),
        "checked_at": checked,
        "report_hash": report_hash,
        "issues": issue_payload,
        "gates": gates,
    }


class RunAdmissionService:
    def __init__(self, *, gate_orchestrator=None, creation=None, preflight_service=None, initial_position_preflight=None):
        self.gates = gate_orchestrator or GateOrchestrator()
        self.creation = creation or RunCreationService()
        self.preflight_service = preflight_service
        self.initial_position_preflight = initial_position_preflight

    def evaluate_gates(
        self,
        *,
        run_kind: str,
        checks: Mapping[str, bool],
        report_hash: str | None = None,
        report_status: object | None = None,
        issues: Sequence[Any] = (),
        metric_checks: Mapping[str, bool] | None = None,
    ) -> tuple[GateDecision, dict[str, Any]]:
        """Evaluate and serialize the one gate decision used by all run paths."""

        decision = self.gates.evaluate(
            run_kind=run_kind,
            checks=checks,
            metric_checks=metric_checks,
        )
        evidence = build_gate_evidence(
            decision,
            report_hash=report_hash,
            report_status=report_status,
            issues=issues or tuple(k for k, value in checks.items() if not value),
        )
        return decision, evidence

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

        preflight_status = "degraded" if degraded else None
        preflight_hash = None
        preflight_reason_code = None
        preflight_issues: tuple[Any, ...] = ()
        if self.preflight_service is not None:
            try:
                admit = getattr(self.preflight_service, "admit", None)
                if callable(admit):
                    decision = admit(
                        binding.data_request,
                        confirmed_report_hash=confirmed_report_hash,
                    )
                else:
                    decision = self.preflight_service.admission(binding.data_request)
                checks = {**checks, "phase2a": bool(getattr(decision, "allowed", False))}
                outcome = getattr(decision, "outcome", None)
                preflight_status = getattr(getattr(outcome, "status", None), "value", getattr(outcome, "status", None))
                preflight_hash = getattr(decision, "report_hash", None)
                preflight_reason_code = getattr(decision, "reason_code", None)
                preflight_issues = tuple(getattr(getattr(outcome, "report", None), "issues", ()) or ())
            except Exception:
                return AdmissionResult(False, code="preflight_dependency_unavailable", message="准入依赖未就绪，已拒绝运行", status="blocked")

        status = preflight_status if preflight_status in {"ready", "degraded"} else None
        report = {"run_kind": binding.run_kind, "checks": dict(sorted(checks.items())), "disabled_metrics": metric_checks or {}}
        report_hash = preflight_hash or canonical_hash(report)
        decision, formal_gates = self.evaluate_gates(
            run_kind=binding.run_kind,
            checks=checks,
            report_hash=report_hash,
            report_status=status,
            issues=preflight_issues,
            metric_checks=metric_checks,
        )
        status = status if decision.allowed and status else ("ready" if decision.allowed else "blocked")
        if not decision.allowed:
            return AdmissionResult(False, code=preflight_reason_code or "preflight_blocked", message="页面准入预检未通过", report_hash=report_hash, status=status, issues=tuple(k for k, value in checks.items() if not value), formal_gates=formal_gates)
        if degraded and binding.run_kind == "internal_link_acceptance":
            return AdmissionResult(False, code="internal_degraded_forbidden", message="内部验收运行禁止 degraded 放行", report_hash=report_hash, status=status, formal_gates=formal_gates)
        if degraded and confirmed_report_hash != report_hash:
            return AdmissionResult(False, code="formal_degraded_confirmation_required", message="正式 degraded 运行需要确认预检报告", report_hash=report_hash, status=status, formal_gates=formal_gates)
        return AdmissionResult(True, binding=binding, message="页面准入预检通过", report_hash=report_hash, status=status, formal_gates=formal_gates)

    def create(self, binding: RunBinding, checks: Mapping[str, bool], *, idempotency_key: str):
        result = self.admission(binding, checks)
        if not result.allowed:
            raise PermissionError(result.message)
        return self.creation.create(binding, idempotency_key=idempotency_key)
