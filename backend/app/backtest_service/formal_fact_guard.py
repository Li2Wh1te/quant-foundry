"""Scoped SQL content checks for the existing formal fact dependencies.

These are computed checksums of actual rows, not synthetic revision flags.
PostgreSQL folds each row's SHA256 into four exact integer sums and a count;
only this bounded metadata crosses into Python. No ORM fact is reconstructed
at an ordinary market-page check. The final caller retains the existing short
table locks and uses the same result transaction.
"""
from datetime import datetime, time, timedelta

from sqlalchemy import BigInteger, Numeric, Text, cast, func, or_, select
from sqlalchemy.dialects.postgresql import BIT

from .serialization import canonical_hash


def fact_statements(work, *, zone, inventory_asof=None):
    from datetime import date
    from app.instruments.models import InstrumentCodeMappingRecord, InstrumentIdentityFactRecord
    from app.instruments.rule_facts_models import InstrumentRuleFactRecord
    from app.instruments.spec_provider import DEFAULT_MAPPING_SOURCE, DEFAULT_RULE_PACKAGE_REFERENCE
    from .formal_execution_producer import PACKAGE
    from app.calendars.models import (CalendarRegistryRecord, CalendarDefinitionRecord,
        CalendarSessionFactRecord, CalendarSourcePriorityRecord)
    from app.data_ingestion.models.trading_calendar import TradingStatusFact, TradingStatusCoverageFact
    from app.data_ingestion.models.corporate_action import CorporateActionFact, CorporateActionCoverageFact

    start, end = (date.fromisoformat(work.config[name]) for name in ('start', 'end'))
    cutoff = inventory_asof or datetime.combine(end, time(), zone)
    mapping = InstrumentCodeMappingRecord
    # A revision may remove an earlier effective interval. Retain all visible
    # mapping/identity revisions, as the PIT repositories do before folding.
    source = func.lower(func.trim(mapping.source)) == DEFAULT_MAPPING_SOURCE
    visible = mapping.known_at <= cutoff
    requested = mapping.source_code.in_(tuple(work.config['universe']))
    ids = select(mapping.instrument_id).where(source, visible, requested)
    identity = InstrumentIdentityFactRecord
    calendars = select(identity.calendar_id).where(identity.instrument_id.in_(ids), identity.known_at <= cutoff)
    statements = [select(mapping.__table__).where(source, visible,
        or_(requested, mapping.instrument_id.in_(ids))),
        select(identity.__table__).where(identity.instrument_id.in_(ids), identity.known_at <= cutoff)]
    rule = InstrumentRuleFactRecord
    packages = or_((rule.rule_package_key == DEFAULT_RULE_PACKAGE_REFERENCE.key)
        & (rule.rule_package_version == DEFAULT_RULE_PACKAGE_REFERENCE.version),
        (rule.rule_package_key == PACKAGE.key) & (rule.rule_package_version == PACKAGE.version))
    related_keys = select(rule.fact_key).where(rule.instrument_id.in_(ids), packages,
        rule.valid_from <= end, or_(rule.valid_to.is_(None), rule.valid_to > start), rule.known_at <= cutoff)
    # A correction can move its effective window or package out of this run.
    # Include its visible revision before the producer folds versions.
    statements.append(select(rule.__table__).where(rule.fact_key.in_(related_keys), rule.known_at <= cutoff))
    for model in (CalendarRegistryRecord, CalendarDefinitionRecord):
        statements.append(select(model.__table__).where(model.calendar_id.in_(calendars)))
    # Calendar assembly verifies full weeks/months and the following session's
    # period. Its bounded next-session search is at most 31 days past the run
    # month/week; the following period fits inside this explicit outer window.
    model = CalendarSessionFactRecord
    statements.append(select(model.__table__).where(model.calendar_id.in_(calendars),
        model.session_date >= start - timedelta(days=31),
        model.session_date <= end + timedelta(days=101)))
    statements.append(select(CalendarSourcePriorityRecord.__table__))
    model = TradingStatusFact
    statements.append(select(model.__table__).where(model.instrument_id.in_(ids),
        model.ts_code.in_(tuple(work.config['universe'])), model.dimension == 'suspension',
        model.trade_date.between(start, end), model.known_at <= cutoff, model.observed_at <= cutoff))
    model = TradingStatusCoverageFact
    statements.append(select(model.__table__).where(model.instrument_id.in_(ids), model.dimension == 'suspension',
        model.start_date <= end, model.end_date >= start, model.known_at <= cutoff, model.observed_at <= cutoff))
    model = CorporateActionCoverageFact
    statements.append(select(model.__table__).where(model.instrument_id.in_(ids),
        model.start_date <= end, model.end_date >= start, model.known_at <= cutoff, model.observed_at <= cutoff))
    model = CorporateActionFact
    statements.append(select(model.__table__).where(model.instrument_id.in_(ids),
        or_(model.record_date.between(start, end), model.ex_date.between(start, end),
            model.cash_effective_date.between(start, end),
            (model.record_date <= end) & (model.cash_effective_date >= start)),
        model.known_at <= cutoff, model.observed_at <= cutoff))
    return tuple(statements)


def fact_fingerprint(session, work, *, zone, inventory_asof=None):
    """Fingerprint the related run scope without fetching or decoding history.

    Some scope is deliberately conservative: identity revision chains, shared
    calendar definitions/priorities and the following calendar period. A change
    there rejects the run rather than risking mixed facts. Unrelated securities
    and cold session history outside the run window are excluded.
    """
    metadata = []
    for statement in fact_statements(work, zone=zone, inventory_asof=inventory_asof):
        rows = statement.subquery()
        digest = func.encode(func.sha256(func.convert_to(cast(func.to_jsonb(rows.table_valued()), Text), 'UTF8')), 'hex')
        limbs = [cast(cast(func.concat('x', func.substr(digest, 1 + 16 * i, 16)), BIT(64)), BigInteger)
                 for i in range(4)]
        folded = select(func.count(), *(func.coalesce(func.sum(cast(value, Numeric)), 0) for value in limbs))
        metadata.append(tuple(session.execute(folded.select_from(rows)).one()))
    return canonical_hash(metadata)
