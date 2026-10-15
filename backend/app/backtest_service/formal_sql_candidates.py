"""One bounded security page of existing normalized SQL facts.

No input contract or producer lives here. Actual SQL rows are converted by
the existing identity/rule validators; knowledge and effective dates remain
separate. Only this security page survives across its daily admission batches.
"""
from collections import OrderedDict, defaultdict
from datetime import datetime, time

from sqlalchemy import and_, func, or_, select

from app.instruments.models import InstrumentCodeMappingRecord as MappingRow, InstrumentIdentityFactRecord as IdentityRow
from app.instruments.rule_facts_models import InstrumentRuleFactRecord as RuleRow
from app.instruments.spec_provider import DEFAULT_MAPPING_SOURCE, DEFAULT_RULE_PACKAGE_REFERENCE
from app.instruments.identity_repository import resolve_instrument_identity, _identity_to_domain
from app.instruments.repository import _to_domain as mapping_to_domain
from app.instruments.rule_facts_repository import _to_domain as rule_to_domain
from app.instruments.domain import order_mapping_segments
from app.data_ingestion.models.trading_calendar import TradingStatusFact, TradingStatusCoverageFact
from app.data_ingestion.models.corporate_action import CorporateActionFact, CorporateActionCoverageFact
from app.data_contracts.errors import ProviderContractViolationError

from .session_fact_pages import PAGE_ROWS


def grouped(rows, key='instrument_id'):
    result = defaultdict(list)
    for row in rows:
        result[getattr(row, key)].append(row)
    return result


class SqlCandidatePage:
    def __init__(self, session, symbols, start, end, cutoff):
        self.session, self.symbols = session, tuple(symbols)
        source = func.lower(func.trim(MappingRow.source)) == DEFAULT_MAPPING_SOURCE
        ids = select(MappingRow.instrument_id).where(source,
            MappingRow.source_code.in_(self.symbols), MappingRow.known_at <= cutoff).correlate(None)
        rows = tuple(session.scalars(select(MappingRow).where(source,
            MappingRow.instrument_id.in_(ids), MappingRow.known_at <= cutoff).order_by(
                MappingRow.logical_fact_key, MappingRow.fact_version)))
        self.mappings = grouped(rows)
        self.by_symbol = defaultdict(set)
        allowed = frozenset(symbols)
        for row in rows:
            if row.source_code in allowed:
                self.by_symbol[row.source_code].add(row.instrument_id)
        ids = tuple(self.mappings)
        self.identities = grouped(session.scalars(select(IdentityRow).where(
            IdentityRow.instrument_id.in_(ids), IdentityRow.known_at <= cutoff).order_by(
                IdentityRow.logical_fact_key, IdentityRow.fact_version)))
        self.rules = grouped(session.scalars(select(RuleRow).where(
            RuleRow.instrument_id.in_(ids), RuleRow.rule_package_key == DEFAULT_RULE_PACKAGE_REFERENCE.key,
            RuleRow.rule_package_version == DEFAULT_RULE_PACKAGE_REFERENCE.version,
            RuleRow.valid_from <= end, or_(RuleRow.valid_to.is_(None), RuleRow.valid_to > start),
            RuleRow.known_at <= cutoff).order_by(RuleRow.valid_from, RuleRow.fact_version)))
        self.ids = ids
        self.converted, self.identity_cache, self.rule_cache, self.commissions = {}, OrderedDict(), OrderedDict(), OrderedDict()
        self.status_rows, self.status_coverage, self.action_coverage, self.events = {}, {}, {}, {}

    @staticmethod
    def _remember(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > PAGE_ROWS:
            cache.popitem(last=False)
        return value

    def _domain(self, kind, row, convert):
        key = (kind, row.id)
        if key not in self.converted:
            self.converted[key] = convert()
        return self.converted[key]

    def identity(self, symbol, day, cutoff, zone):
        at = datetime.combine(day, time(), zone)
        found = []
        for iid in sorted(self.by_symbol.get(symbol, ()), key=str):
            mappings = [row for row in self.mappings[iid] if row.known_at <= cutoff]
            if not any(row.source_code == symbol for row in mappings):
                continue
            identities = [row for row in self.identities.get(iid, ()) if row.known_at <= cutoff]
            def signature(rows):
                return tuple((row.id, row.valid_from <= day and (row.valid_to is None or day < row.valid_to))
                             for row in rows)
            key = (symbol, iid, signature(mappings), signature(identities))
            if key in self.identity_cache:
                pair = self.identity_cache[key]
                self.identity_cache.move_to_end(key)
            else:
                mapping_facts = tuple(self._domain('mapping', row, lambda row=row: mapping_to_domain(row,
                    instrument_id=iid, source=DEFAULT_MAPPING_SOURCE, session_date=day, data_cutoff=cutoff))
                    for row in mappings)
                identity_facts = tuple(self._domain('identity', row, lambda row=row: _identity_to_domain(row,
                    effective_at=at, data_cutoff=cutoff)) for row in identities)
                resolved = resolve_instrument_identity(iid, effective_at=at, data_cutoff=cutoff,
                    identity_facts=identity_facts, mappings=mapping_facts, source=DEFAULT_MAPPING_SOURCE)
                mapping = resolved.mapping if resolved is not None else None
                # Preserve the original repository's missing-window refusal.
                selected = order_mapping_segments(() if mapping is None else (mapping,),
                    start_date=day, end_date=day, data_cutoff=cutoff,
                    instrument_id=iid, source=DEFAULT_MAPPING_SOURCE)
                pair = ((resolved.identity_fact, selected) if resolved.identity_fact is not None
                        and mapping.source_code == symbol else None)
                self._remember(self.identity_cache, key, pair)
            if pair is not None:
                found.append(pair)
        if len(found) > 1:
            raise ValueError('identity_mapping_ambiguous')
        return found[0] if found else None

    def rule(self, fact, day, cutoff, registry):
        from app.instruments.rules.resolver import RulePackageResolver
        from app.instruments.rules.contracts import ResolutionStatus
        rows = [row for row in self.rules.get(fact.instrument_id, ()) if row.known_at <= cutoff
                and row.valid_from <= day and (row.valid_to is None or day < row.valid_to)]
        if not rows:
            return None
        key = (fact.instrument_id, fact.asset_class, tuple(row.id for row in rows))
        if key in self.rule_cache:
            self.rule_cache.move_to_end(key)
            return self.rule_cache[key]
        candidates = tuple(self._domain('rule', row, lambda row=row: rule_to_domain(row)) for row in rows)
        resolved = RulePackageResolver(registry).resolve(DEFAULT_RULE_PACKAGE_REFERENCE,
            instrument_id=fact.instrument_id, asset_class=fact.asset_class,
            effective_date=day, data_cutoff=cutoff, facts=candidates)
        if resolved.status != ResolutionStatus.READY:
            raise ValueError(','.join(issue.code.value for issue in resolved.issues))
        return self._remember(self.rule_cache, key, resolved)

    def load_day(self, day, cutoff):
        # Drop the prior daily buffers BEFORE querying the next batch.
        self.status_rows, self.status_coverage, self.action_coverage, self.events = {}, {}, {}, {}
        if not self.ids:
            return
        self.status_rows = grouped(self.session.scalars(select(TradingStatusFact).where(
            TradingStatusFact.instrument_id.in_(self.ids), TradingStatusFact.ts_code.in_(self.symbols),
            TradingStatusFact.trade_date == day, TradingStatusFact.dimension == 'suspension',
            TradingStatusFact.known_at <= cutoff, TradingStatusFact.observed_at <= cutoff)))
        self.status_coverage = grouped(self.session.scalars(select(TradingStatusCoverageFact).where(
            TradingStatusCoverageFact.instrument_id.in_(self.ids), TradingStatusCoverageFact.dimension == 'suspension',
            TradingStatusCoverageFact.start_date <= day, TradingStatusCoverageFact.end_date >= day,
            TradingStatusCoverageFact.known_at <= cutoff, TradingStatusCoverageFact.observed_at <= cutoff).order_by(
                TradingStatusCoverageFact.known_at.desc(), TradingStatusCoverageFact.observed_at.desc())))
        self.action_coverage = grouped(self.session.scalars(select(CorporateActionCoverageFact).where(
            CorporateActionCoverageFact.instrument_id.in_(self.ids),
            CorporateActionCoverageFact.start_date <= day, CorporateActionCoverageFact.end_date >= day,
            CorporateActionCoverageFact.known_at.is_not(None), CorporateActionCoverageFact.known_at <= cutoff,
            or_(CorporateActionCoverageFact.observed_at <= cutoff,
                and_(CorporateActionCoverageFact.observed_at.is_(None), CorporateActionCoverageFact.computed_at <= cutoff)))
            .order_by(CorporateActionCoverageFact.instrument_id, CorporateActionCoverageFact.action_type,
                CorporateActionCoverageFact.observed_at.desc())))
        model = CorporateActionFact
        self.events = grouped(self.session.scalars(select(model).where(model.instrument_id.in_(self.ids),
            or_(model.record_date.between(day, day), model.ex_date.between(day, day),
                model.cash_effective_date.between(day, day),
                and_(model.record_date <= day, model.cash_effective_date >= day)),
            model.known_at.is_not(None), model.known_at <= cutoff,
            or_(model.observed_at <= cutoff, and_(model.observed_at.is_(None), model.created_at <= cutoff)))
            .order_by(model.logical_fact_key, model.fact_version.desc())))

    def status_inputs(self, iid, symbol):
        return (tuple(row for row in self.status_rows.get(iid, ()) if row.ts_code == symbol),
                tuple(self.status_coverage.get(iid, ())))

    def action_coverage_inputs(self, iid):
        candidates = self.action_coverage.get(iid, ())
        selected = {}
        for row in candidates:
            rank = (row.known_at, row.observed_at or row.computed_at)
            if row.action_type not in selected or rank > selected[row.action_type][0]:
                selected[row.action_type] = (rank, row)
        coverage = tuple(selected[name][1] for name in sorted(selected, key=lambda name: name or ''))
        return coverage, tuple(row for row in candidates if row.observed_at is not None)

    def action_events(self, iid):
        # Same logical-version fold as the existing normalized action reader.
        events = []
        for logical_key, versions in grouped(self.events.get(iid, ()), 'logical_fact_key').items():
            winner = versions[0]
            if len(versions) > 1 and winner.supersedes_fact_id not in {row.event_id for row in versions[1:]}:
                raise ProviderContractViolationError('multiple active corporate-action fact versions',
                    details=dict(logical_fact_key=logical_key, fact_versions=[row.fact_version for row in versions]))
            events.append(winner)
        return tuple(events)
