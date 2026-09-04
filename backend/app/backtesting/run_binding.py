"""Immutable run bindings and admission/creation orchestration for backtests.

This module intentionally contains only pure domain services.  Persistence and
worker implementations can project these objects without re-resolving mutable
configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence
from types import MappingProxyType
from uuid import UUID, uuid4

from app.backtesting.spec import BacktestSpec
from app.backtesting.data.reports import canonical_json, canonical_hash


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value

def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Reject credential-bearing fields so callers cannot silently lose evidence."""
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        lowered = name.lower()
        sensitive_name = (
            any(
                word in lowered
                for word in (
                    "secret",
                    "password",
                    "credential",
                    "access_token",
                    "refresh_token",
                    "private_key",
                )
            )
            or lowered in {"token", "api_token", "auth_token", "bearer_token"}
        )
        if sensitive_name:
            raise ValueError(f"sensitive field is forbidden in run binding: {name}")
        if isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, (list, tuple)):
            result[name] = [(_safe_mapping(v) if isinstance(v, Mapping) else _plain(v)) for v in item]
        else:
            result[name] = _plain(item)
    return result


@dataclass(frozen=True, slots=True)
class RunBinding:
    spec: BacktestSpec
    run_kind: str
    profile: str
    strategy: Mapping[str, Any] = field(default_factory=dict)
    components: Mapping[str, Any] = field(default_factory=dict)
    data_request: Mapping[str, Any] = field(default_factory=dict)
    account: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    random_seed: int | None = None
    config: Mapping[str, Any] = field(init=False)
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.run_kind not in {"backtest_run", "internal_link_acceptance"}:
            raise ValueError("unsupported run_kind")
        if self.run_kind == "backtest_run" and self.profile != "formal@1":
            raise ValueError("formal runs must use formal@1")
        if self.run_kind == "internal_link_acceptance" and self.profile != "internal_link_acceptance@1":
            raise ValueError("internal runs must use internal_link_acceptance@1")
        if self.random_seed is not None and (
            isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int)
        ):
            raise ValueError("random_seed must be an integer or null")
        payload = {
            # Bump this only when the serialized meaning of a run input
            # changes.  The value is part of config_hash, so an old run can
            # never be mistaken for a new snapshot shape.
            "schema_version": 2,
            "spec": {
                "start_date": self.spec.start_date,
                "end_date": self.spec.end_date,
                "currency": self.spec.currency,
                "timezone": self.spec.timezone,
                "frequency": self.spec.frequency,
                "warmup_sessions": self.spec.warmup_sessions,
                "initial_cash": str(self.spec.initial_cash),
                "initial_positions": [
                    {"instrument_id": p.instrument_id, "side": p.side.value,
                     "quantity": str(p.quantity), "available_quantity": str(p.available_quantity),
                     "average_price": None if p.average_price is None else str(p.average_price)}
                    for p in self.spec.initial_positions
                ],
                "dynamic_universe": self.spec.dynamic_universe,
                "instrument_ids": list(self.spec.instrument_ids),
                "exchanges": list(self.spec.exchanges),
                "strategy_price_bases": list(self.spec.strategy_price_bases),
                "strategy_revision_id": self.spec.strategy_revision_id,
                "strategy_parameters": (
                    None
                    if self.spec.strategy_parameters is None
                    else _safe_mapping(self.spec.strategy_parameters)
                ),
                "account_profile_id": self.spec.account_profile_id,
                "slippage_model": {
                    "key": self.spec.slippage_model.key,
                    "version": self.spec.slippage_model.version,
                    "parameters": _safe_mapping(self.spec.slippage_model.parameters),
                },
                "random_seed": self.spec.random_seed,
            },
            "run_kind": self.run_kind, "profile": self.profile,
            "strategy": _safe_mapping(self.strategy), "components": _safe_mapping(self.components),
            "data_request": _safe_mapping(self.data_request), "account": _safe_mapping(self.account),
            "metadata": _safe_mapping(self.metadata),
            "random_seed": self.random_seed,
        }
        object.__setattr__(self, "config", _freeze(_plain(payload)))
        object.__setattr__(self, "config_hash", canonical_hash(payload))


class RunBindingBuilder:
    """Build a binding from already-resolved strategy/account/component data."""
    def build(self, spec: BacktestSpec, *, run_kind: str = "backtest_run",
              strategy: Mapping[str, Any] | None = None,
              components: Mapping[str, Any] | None = None,
              data_request: Mapping[str, Any] | None = None,
              account: Mapping[str, Any] | None = None,
              metadata: Mapping[str, Any] | None = None,
              strategy_revision: Mapping[str, Any] | None = None,
              account_resolver: Any = None,
              account_context: Any = None,
              random_seed: int | None = None) -> RunBinding:
        if strategy_revision is not None:
            strategy = self.build_strategy(strategy_revision)
        if account_resolver is not None:
            if account_context is None:
                raise ValueError("account resolution context is required")
            try:
                account = self.build_account(account_resolver.resolve(account_context), run_kind=run_kind)
            except Exception as exc:
                raise ValueError("account resolution dependency unavailable") from exc
        profile = "formal@1" if run_kind == "backtest_run" else "internal_link_acceptance@1"
        strategy = strategy or {}
        components = components or {}
        account = account or {}
        if spec.strategy_revision_id is not None and str(
            strategy.get("revision_id")
        ) != str(spec.strategy_revision_id):
            raise ValueError("resolved strategy revision does not match run spec")
        if spec.strategy_parameters is not None and _plain(
            strategy.get("parameters", {})
        ) != _plain(spec.strategy_parameters):
            raise ValueError("resolved strategy parameters do not match run spec")
        if spec.account_profile_id is not None and str(
            account.get("profile_id", account.get("account_profile_id"))
        ) != str(spec.account_profile_id):
            raise ValueError("resolved account profile does not match run spec")
        if components:
            slippage = components.get("slippage_model")
            if not isinstance(slippage, Mapping) or (
                slippage.get("key"), slippage.get("version")
            ) != (spec.slippage_model.key, spec.slippage_model.version):
                raise ValueError("resolved slippage model does not match run spec")
            if _plain(slippage.get("parameters", {})) != _plain(
                spec.slippage_model.parameters
            ):
                raise ValueError("resolved slippage parameters do not match run spec")
        if random_seed is not None and spec.random_seed not in (None, random_seed):
            raise ValueError("resolved random seed does not match run spec")
        random_seed = spec.random_seed if random_seed is None else random_seed
        if run_kind == "backtest_run" and account:
            fee_key = account.get("fee_schedule_key")
            schedule = account.get("fee_schedule")
            if fee_key is None and isinstance(schedule, Mapping):
                fee_key = schedule.get("key")
            if fee_key == "zero_cost":
                raise ValueError("zero_cost fee schedule is reserved for tests")
        return RunBinding(
            spec,
            run_kind,
            profile,
            strategy,
            components,
            data_request or {},
            account,
            metadata or {},
            random_seed,
        )

    def build_strategy(self, revision: Mapping[str, Any]) -> Mapping[str, Any]:
        """Accept only an explicitly published, immutable strategy revision."""
        if not revision.get("published", False) or revision.get("is_draft", False):
            raise ValueError("published strategy revision is required")
        if not revision.get("revision_id"):
            raise ValueError("strategy revision_id is required")
        # A published revision is self describing; reject partial/draft
        # payloads so a run can never silently fall back to latest code.
        required = ("strategy_id", "source_hash", "contract_version", "parameter_schema")
        missing = [name for name in required if revision.get(name) in (None, "")]
        if missing:
            raise ValueError(f"strategy revision missing required fields: {','.join(missing)}")
        source_hash = str(revision["source_hash"])
        if len(source_hash) != 64 or any(c not in "0123456789abcdef" for c in source_hash.lower()):
            raise ValueError("strategy source_hash must be sha256 hex")
        if not isinstance(revision["parameter_schema"], Mapping):
            raise ValueError("strategy parameter_schema must be an object")
        return _safe_mapping(revision)

    def build_components(self, registry: Any, selections: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
        resolved: dict[str, Any] = {}
        for kind, ref in selections.items():
            key, version = ref.get("key"), ref.get("version")
            if not key or not isinstance(version, int):
                raise ValueError(f"component {kind} requires key and version")
            entry = registry.resolve(key, version)
            resolved[kind] = {
                "key": entry.key,
                "version": entry.version,
                "kind": entry.component_kind,
                "name_zh": entry.name_zh,
                "name_en": entry.name_en,
                "display_name": entry.display_name,
                "parameter_schema": _plain(entry.parameter_schema),
                "capabilities": _plain(entry.capabilities),
                "parameters": _safe_mapping(ref.get("parameters", {})),
            }
        return resolved

    def build_data_request(self, request: Mapping[str, Any], *, run_kind: str) -> Mapping[str, Any]:
        expected = "backtest_run" if run_kind == "backtest_run" else "internal_link_acceptance"
        if request.get("run_kind", expected) != expected:
            raise ValueError("data request run_kind/profile mismatch")
        if request.get("frequency", "1d") != "1d":
            raise ValueError("only 1d backtests are supported")
        if request.get("max_lookback_sessions", 512) != 512:
            raise ValueError("max_lookback_sessions must be 512")
        chunk = request.get("chunk_policy", "fixed_trading_sessions@1")
        if chunk != "fixed_trading_sessions@1" or request.get("chunk_size_sessions", 20) != 20:
            raise ValueError("fixed_trading_sessions@1 with 20 sessions is required")
        return _safe_mapping(request)

    def build_account(self, selection: Any, *, run_kind: str = "backtest_run") -> Mapping[str, Any]:
        """Project a resolver result, preserving its pinned version/snapshot."""
        if selection is None:
            raise ValueError("resolved account selection is required")
        if hasattr(selection, "to_dict"):
            value = selection.to_dict()
        elif hasattr(selection, "profile_version") and hasattr(selection, "fee_schedule"):
            pv, fs = selection.profile_version, selection.fee_schedule
            value = {
                "profile_id": str(pv.profile_id), "version": pv.version,
                "display_name": pv.display_name,
                "config_snapshot": _plain(pv.config_snapshot),
                "applicability": _plain(pv.applicability),
                "fee_schedule_key": pv.fee_schedule_key,
                "fee_schedule_version": pv.fee_schedule_version,
                "fee_schedule": {"key": fs.key, "version": fs.version,
                                  "metadata": _plain(fs.metadata),
                                  "fee_rules": [_plain(r.__dict__) if hasattr(r, "__dict__") else str(r) for r in fs.fee_rules]},
                "selection_hash": getattr(selection, "selection_hash", None),
                "resolution_audit": _plain(selection.audit.to_payload()) if hasattr(selection, "audit") else None,
            }
        elif hasattr(selection, "__dict__"):
            value = dict(selection.__dict__)
        elif isinstance(selection, Mapping):
            value = dict(selection)
        else:
            raise ValueError("unsupported resolved account selection")
        if not value.get("profile_id") and not value.get("account_profile_id"):
            raise ValueError("account profile version is required")
        fee = value.get("fee_schedule_key")
        if fee == "zero_cost":
            raise ValueError("zero_cost fee schedule is reserved for tests")
        return _safe_mapping(value)


class Gate(StrEnum):
    PHASE1 = "phase1"
    PHASE2A = "phase2a"
    FORMAL_BASIC = "formal_basic"
    FORMAL_COMPLETE = "formal_complete"


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    checks: Mapping[str, bool]
    disabled_metrics: tuple[str, ...] = ()
    reason: str | None = None


class GateOrchestrator:
    def evaluate(self, *, run_kind: str, checks: Mapping[str, bool], metric_checks: Mapping[str, bool] | None = None) -> GateDecision:
        required = (Gate.PHASE1.value, Gate.PHASE2A.value) if run_kind == "internal_link_acceptance" else tuple(g.value for g in Gate)
        failed = [k for k in required if not checks.get(k, False)]
        disabled = tuple(k for k, ok in (metric_checks or {}).items() if not ok)
        return GateDecision(not failed, dict(checks), disabled, ",".join(failed) if failed else None)


class IdempotencyKeyReusedError(ValueError):
    """An idempotency key was reused for a different frozen request."""


class QueueFullError(RuntimeError):
    """A logical backtest queue cannot accept another queued root."""

    code = "backtest_queue_full"

    def __init__(
        self,
        message: str = "backtest queue is full",
        *,
        queue_kind: str | None = None,
        queued_count: int | None = None,
        queue_limit: int | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(message)
        self.queue_kind = queue_kind
        self.queued_count = queued_count
        self.queue_limit = queue_limit
        self.disabled = disabled


@dataclass(frozen=True, slots=True)
class BacktestRun:
    run_id: UUID
    binding: RunBinding
    status: str = "queued"
    owner_scope: str = "default"
    idempotency_key: str | None = None
    terminal_status: str | None = None
    rerun_of_run_id: UUID | None = None


class RunCreationService:
    def __init__(self, *, formal_capacity: int = 32, internal_capacity: int | None = None) -> None:
        if (
            not isinstance(formal_capacity, int)
            or isinstance(formal_capacity, bool)
            or formal_capacity < 1
            or formal_capacity > 32
        ):
            raise ValueError("formal capacity must be between 1 and 32")
        if internal_capacity is not None and (
            not isinstance(internal_capacity, int)
            or isinstance(internal_capacity, bool)
            or internal_capacity < 1
            or internal_capacity >= formal_capacity
            or internal_capacity >= 32
        ):
            raise ValueError("internal capacity must be smaller than formal capacity")
        self.formal_capacity, self.internal_capacity = formal_capacity, internal_capacity
        self._runs: dict[tuple[str, str], BacktestRun] = {}
        self._queued = {"backtest_run": 0, "internal_link_acceptance": 0}

    def create(self, binding: RunBinding, *, idempotency_key: str | None = None, queued: int | None = None, tenant_id: str = "default") -> BacktestRun:
        key = (tenant_id, idempotency_key) if idempotency_key else None
        if key and key in self._runs:
            existing = self._runs[key]
            if existing.binding.config_hash != binding.config_hash:
                raise IdempotencyKeyReusedError("idempotency key already used with different request")
            return existing
        cap = self.internal_capacity if binding.run_kind == "internal_link_acceptance" else self.formal_capacity
        current = self._queued[binding.run_kind] if queued is None else queued
        if cap is None or current >= cap:
            raise QueueFullError(
                "backtest queue is full",
                queue_kind=binding.run_kind,
                queued_count=current,
                queue_limit=cap,
                disabled=cap is None,
            )
        run = BacktestRun(
            uuid4(),
            binding,
            idempotency_key=idempotency_key,
            owner_scope=tenant_id,
        )
        if idempotency_key:
            self._runs[key] = run
        self._queued[binding.run_kind] += 1
        return run

    def mark_claimed(self, run: BacktestRun) -> None:
        """Release a queued slot when a worker claims the run."""
        kind = run.binding.run_kind
        if self._queued.get(kind, 0) > 0:
            self._queued[kind] -= 1
