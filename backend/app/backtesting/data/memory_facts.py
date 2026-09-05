"""Optional immutable fact families served under the memory chunk contract."""
from dataclasses import fields, is_dataclass
from datetime import datetime
from collections.abc import Mapping

from app.backtesting.data.facts import (
    Tick, DataPoint, AdjustedSeriesPoint, CorporateAction, TradingRule,
    TradingStatus, InstrumentCodeMapping,
)
from app.backtesting.data.requests import DataCapability, DateRange, LookbackWindow
from app.backtesting.data.errors import (
    InvalidDataRequestError, HistoryIncompleteError, UnsupportedCapabilityError,
)

FAMILIES = {
    DataCapability.TICKS: ("ticks", Tick),
    DataCapability.VALUES: ("values", DataPoint),
    DataCapability.ADJUSTED_SERIES: ("adjusted_points", AdjustedSeriesPoint),
    DataCapability.ACTIONS: ("corporate_actions", CorporateAction),
    DataCapability.MAPPINGS: ("mappings", InstrumentCodeMapping),
    DataCapability.RULES: ("rules", TradingRule),
    DataCapability.STATUS: ("statuses", TradingStatus),
}


def fact_payload(value):
    """Preserve every immutable field, including evidence, in token hashes."""
    if is_dataclass(value):
        return {field.name: fact_payload(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: fact_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [fact_payload(item) for item in value]
    return value


def query_facts(chunk, query, capability, query_type):
    """Select fixture rows without widening the frozen session or chunk.

    An absent collection means unsupported; an explicitly empty collection
    supports reads but never constitutes a complete coverage assertion.
    """
    name, _ = FAMILIES[capability]
    chunk._guard_business_query(name)
    chunk._require_query_type(query, query_type, name)
    rows = getattr(chunk._provider._dataset, name)
    if rows is None:
        raise UnsupportedCapabilityError(f"the memory fixture does not serve {name}")
    chunk._require_declared_fact_type(capability, name)
    chunk._require_authorized_instruments(query.instrument_ids, name)
    boundary = query.boundary
    frozen = chunk._session._request.query_boundary
    if boundary.data_cutoff > frozen.data_cutoff or (
        frozen.knowledge_as_of is not None and
        (boundary.knowledge_as_of is None or boundary.knowledge_as_of > frozen.knowledge_as_of)
    ):
        raise InvalidDataRequestError("fact query widens the frozen PIT boundary")
    pool = (*chunk._session.warmup_sessions,
            *chunk._session.resolved_sessions[:chunk._formal_end_index])
    wanted = None
    if capability is DataCapability.TICKS:
        start, end = query.start_at.date(), query.end_at.date()
    elif isinstance(query.window, DateRange):
        start, end = query.window.start_date, query.window.end_date
    else:
        window = query.window
        if not isinstance(window, LookbackWindow):
            raise InvalidDataRequestError("invalid fact query window")
        eligible = [point.session_date for point in pool
                    if point.session_date < window.end_at.date() or
                    (point.session_date == window.end_at.date() == boundary.cutoff_date
                     and boundary.include_cutoff_day)]
        if len(eligible) < window.sessions:
            raise HistoryIncompleteError("fact lookback exceeds available session history")
        wanted = set(eligible[-window.sessions:])
        start, end = min(wanted), max(wanted)
    if end > chunk._sessions[-1].session_date or start < pool[0].session_date:
        raise InvalidDataRequestError("fact query exceeds bounded chunk history")
    result = []
    for row in rows:
        if row.instrument_id not in query.instrument_ids:
            continue
        evidence = row if capability is DataCapability.MAPPINGS else row.evidence
        if evidence.observed_at > boundary.data_cutoff:
            continue
        if evidence.known_at is not None and evidence.known_at > boundary.data_cutoff:
            continue
        if boundary.knowledge_as_of is not None and (
            evidence.known_at is None or evidence.known_at > boundary.knowledge_as_of
        ):
            continue
        if capability is DataCapability.TICKS:
            if not query.start_at <= row.traded_at <= query.end_at:
                continue
            day = row.traded_at.date()
        elif capability in (DataCapability.VALUES, DataCapability.ADJUSTED_SERIES):
            day = row.point_date
            if capability is DataCapability.VALUES and (
                row.series != query.series or
                (query.frequency is not None and row.frequency != query.frequency)
            ):
                continue
            if capability is DataCapability.ADJUSTED_SERIES and (
                row.price_basis != query.price_basis or query.frequency != "1d"
            ):
                continue
        elif capability is DataCapability.ACTIONS:
            day = row.ex_date
            if query.action_types and row.action_type not in query.action_types:
                continue
        else:
            if row.valid_from > end or (row.valid_to is not None and row.valid_to <= start):
                continue
            if capability is DataCapability.MAPPINGS and row.source != query.source:
                continue
            if capability is DataCapability.RULES and query.rule_class and row.rule_class != query.rule_class:
                continue
            day = max(start, row.valid_from)
        if start <= day <= end and (wanted is None or day in wanted):
            result.append(row)
    if wanted is not None:
        for instrument_id in query.instrument_ids:
            complete = {row.point_date for row in result if row.instrument_id == instrument_id
                        and row.evidence.quality_status.value == "complete"}
            if complete != wanted:
                raise HistoryIncompleteError("fact lookback contains missing or incomplete sessions")
    from app.backtesting.data.reports import canonical_json
    return tuple(sorted(result, key=lambda row: canonical_json(fact_payload(row))))
