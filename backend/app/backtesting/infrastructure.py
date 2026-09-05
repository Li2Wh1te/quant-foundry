"""Registered infrastructure factories with explicit runtime dependency injection.

Configuration contains only JSON parameters. Database sessions and resolved
calendars are supplied when the Worker builds a component; importing the registry
never imports persistence adapters into the deterministic engine kernel.
"""
from dataclasses import dataclass
from typing import Callable, Mapping, Any


@dataclass(frozen=True)
class RuntimeFactory:
    create: Callable[..., object]

    def build(self, **dependencies):
        return self.create(**dependencies)


@dataclass(frozen=True)
class StrictCalendarPolicy:
    key: str = "strict_compatible"
    version: int = 1

    def open_snapshot(self, provider, request, *, calendar_ids):
        from app.backtesting.data.errors import ProviderContractViolationError
        from app.backtesting.calendar_axis import CalendarSnapshotRequest
        if (request.calendar_axis_policy.key, request.calendar_axis_policy.version) != (self.key, self.version):
            raise ProviderContractViolationError("calendar policy does not match the frozen request")
        return provider.open_calendar_snapshot(CalendarSnapshotRequest(
            calendar_ids=calendar_ids,
            formal_start=request.requested_window.start_date,
            formal_end=request.requested_window.end_date,
            warmup_sessions=request.warmup_sessions, query_boundary=request.query_boundary,
            instrument_ids=request.fixed_instrument_ids, provider_key=request.provider_key,
            package_key=request.rule_package.key, package_version=request.rule_package.version,
        ))


def _data_provider(*, session, **dependencies):
    from app.backtesting.production_runtime import SqlBacktestProvider
    return SqlBacktestProvider(session, **dependencies)


def _time_axis(*, sessions):
    from app.backtesting.time_axis import TradingDayAxis
    return TradingDayAxis(sessions)


def _accounting(*, currency):
    from app.backtesting.accounting import AccountingPolicy, SettlementPolicy
    return AccountingPolicy(currency=currency, settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH)


def _rules():
    from app.instruments.rules.registry import RulePackageRegistry
    from app.instruments.rules.etf_china import register_china_listed_etf_rules
    registry = RulePackageRegistry()
    register_china_listed_etf_rules(registry)
    return registry


def _actions(*, data_session, request):
    from app.backtesting.production_runtime import _corporate_action_snapshot
    return _corporate_action_snapshot(data_session, request)


# Definitions are defaults, not dispatch branches. Every selection is resolved
# and constructed through the same exact-version ComponentRegistry interface.
INFRASTRUCTURE = (
    ("data_provider", "etf_ingestion", "ETF 数据", "ETF Data", _data_provider),
    ("rule_package", "china_listed_etf_rules", "境内 ETF 规则", "China ETF Rules", _rules),
    ("calendar_axis_policy", "strict_compatible", "严格兼容日历", "Strict Compatible Calendars", StrictCalendarPolicy),
    ("time_axis", "trading_day", "交易日时间轴", "Trading Day Axis", _time_axis),
    ("accounting_policy", "accounting_policy", "T+1 账户会计", "T+1 Accounting", _accounting),
    ("corporate_action_timing", "after_open_match", "现金分红有效日", "Cash Dividend Effective Date", _actions),
)


def register_infrastructure(registry):
    from app.backtesting.registry import ComponentRegistryEntry, RegistryError

    def factory(create):
        def construct(parameters: Mapping[str, Any]):
            if parameters:
                raise RegistryError("this infrastructure version accepts no configuration parameters")
            return RuntimeFactory(create)
        return construct

    for kind, key, zh, en, create in INFRASTRUCTURE:
        registry.register(ComponentRegistryEntry(
            component_kind=kind, key=key, version=1, name_zh=zh, name_en=en,
            factory=factory(create),
            parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
            capabilities={"frequency": "1d", "runtime_dependencies_explicit": True},
        ))
