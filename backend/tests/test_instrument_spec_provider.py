"""Acceptance tests for the task-13 single-instrument qualification port."""

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentIdentityFact,
    VersionedReference,
)
from app.instruments.spec_provider import InstrumentSpecProvider
from app.instruments.rule_preflight import FixedInstrumentRulePreflightService
from app.instruments.rules import (
    FactQualityStatus,
    RuleFactCandidate,
)


PACKAGE = VersionedReference(key="china_listed_etf_rules", version=1)
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
EFFECTIVE = datetime(2026, 1, 5, tzinfo=UTC)


def fields() -> dict[str, object]:
    return {
        "lot_size": "100",
        "quantity_precision": 0,
        "price_precision": 3,
        "price_tick": "0.001",
        "contract_multiplier": "1",
        "trading_session_template": {"key": "cn_etf_session_template", "version": 1},
        "settlement_rule_class": "t1_before_open_match",
        "sellable_rule": {"statements": ["sell_limited_by_available_position"]},
        "fee_categories": ["commission"],
        "trading_status_applicability": {
            "suspension": "not_applicable",
            "opening_availability": "not_applicable",
            "price_limit_tradability": "not_applicable",
        },
        "currency": "CNY",
        "order_types": ["market"],
        "minimum_order_quantity": "100",
        "price_limit_rule": {"key": "price_limit", "version": 1},
        "cash_availability_rule": {"key": "cash", "version": 1},
        "position_availability_rule": {"key": "position", "version": 1},
    }


class _Stores:
    def __init__(self, instrument_id, *, known_at=CUTOFF, exchange="SH", include_mapping=True):
        self.instrument_id = instrument_id
        self.identity = InstrumentIdentityFact(
            instrument_id=instrument_id,
            fact_version=1,
            asset_class="etf",
            exchange=exchange,
            currency="CNY",
            calendar_id="XSHG",
            valid_from=date(2020, 1, 1),
            known_at=known_at,
            observed_at=known_at,
            evidence="identity://fact",
        )
        self.display = InstrumentDisplay(instrument_id, "510300", "沪深300ETF", None)
        self.mapping = InstrumentCodeMapping(
            instrument_id=instrument_id,
            source="etf_ingestion",
            source_code="510300.SH",
            trading_code="510300",
            valid_from=date(2020, 1, 1),
            mapping_source="exchange://mapping",
            evidence="mapping://fact",
            known_at=known_at,
            observed_at=known_at,
        )
        self.fact = RuleFactCandidate(
            fact_reference=VersionedReference(key="etf_rule_fact", version=1),
            instrument_id=instrument_id,
            package_reference=PACKAGE,
            source="exchange_rule_book",
            source_revision="revision-1",
            known_at=known_at,
            observed_at=known_at,
            quality_status=FactQualityStatus.COMPLETE,
            fixture_only=False,
            content_hash="a" * 64,
            fields=fields(),
            valid_from=date(2020, 1, 1),
        )
        self.include_mapping = include_mapping
        self.universe_called = False

    def resolve_identity_at(self, instrument_id, *, effective_at, data_cutoff):
        return self.identity

    def resolve_display_at(self, instrument_id, *, effective_at, data_cutoff):
        return self.display

    def resolve_code_mappings(self, instrument_id, **kwargs):
        return (self.mapping,) if self.include_mapping else ()

    def list_facts(self, instrument_id, package_reference, **kwargs):
        return (self.fact,)

    def resolve(self, *args, **kwargs):
        self.universe_called = True
        raise AssertionError("qualification must not query a dynamic universe")


class _Calendar:
    def resolve(self, calendar_id, *, effective_at, data_cutoff):
        return {"timezone": "Asia/Shanghai", "sessions": [{"start": "09:30", "end": "15:00"}]}


class _MissingCalendar:
    def resolve(self, calendar_id, *, effective_at, data_cutoff):
        return None


class InstrumentSpecProviderTests(unittest.TestCase):
    def _provider(self, stores):
        return InstrumentSpecProvider(
            stores,
            stores,
            stores,
            None,
            stores,
            None,
            InstrumentCapabilities(
                position_sides=frozenset({"long"}),
                order_types=frozenset({"market"}),
                margin_supported=False,
                corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
            ),
            _Calendar(),
        )

    def test_complete_pit_facts_produce_spec_and_stable_hash(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        provider = self._provider(stores)
        result = provider.qualify(instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF)
        self.assertTrue(result.eligible)
        self.assertEqual(result.spec.exchange, "SH")
        self.assertEqual(result.spec.lot_size, 100)
        self.assertEqual(result.spec.display.trading_code, "510300")
        self.assertEqual(result.rule_evidence["selected_facts"][0]["content_hash"], "a" * 64)
        self.assertEqual(result.resolution_hash, provider.qualify(instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF).resolution_hash)

    def test_missing_exchange_is_structured_block(self):
        instrument_id = uuid4()
        result = self._provider(_Stores(instrument_id, exchange=None)).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertFalse(result.eligible)
        self.assertIn("identity_exchange_missing", result.reason_codes)
        self.assertIsNone(result.spec)

    def test_missing_identity_is_structured_block(self):
        instrument_id = uuid4()

        class MissingIdentity(_Stores):
            def resolve_identity_at(self, instrument_id, *, effective_at, data_cutoff):
                return None

        result = self._provider(MissingIdentity(instrument_id)).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertEqual(result.reason_codes, ("identity_fact_missing",))
        self.assertIsNone(result.spec)

    def test_missing_display_fields_remain_none(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        stores.display = InstrumentDisplay(instrument_id)
        result = self._provider(stores).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertTrue(result.eligible)
        self.assertIsNone(result.spec.display.trading_code)
        self.assertIsNone(result.spec.display.name)
        self.assertIsNone(result.spec.display.display_name)

    def test_missing_display_provider_keeps_identity_only_spec(self):
        instrument_id = uuid4()

        class IdentityOnly(_Stores):
            resolve_display_at = None
            resolve_display = None

        stores = IdentityOnly(instrument_id)
        provider = InstrumentSpecProvider(
            stores,
            None,
            stores,
            None,
            stores,
            None,
            InstrumentCapabilities(
                position_sides=frozenset({"long"}),
                order_types=frozenset({"market"}),
                margin_supported=False,
                corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
            ),
            _Calendar(),
        )
        result = provider.qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.spec.display.instrument_id, instrument_id)
        self.assertIsNone(result.spec.display.trading_code)

    def test_missing_calendar_blocks(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        provider = InstrumentSpecProvider(
            stores,
            stores,
            stores,
            None,
            stores,
            None,
            InstrumentCapabilities(
                position_sides=frozenset({"long"}),
                order_types=frozenset({"market"}),
                margin_supported=False,
                corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
            ),
            _MissingCalendar(),
        )
        result = provider.qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertIn("calendar_session_missing", result.reason_codes)
        self.assertIsNone(result.spec)

    def test_missing_capabilities_blocks(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        provider = InstrumentSpecProvider(
            stores,
            stores,
            stores,
            None,
            stores,
            None,
            None,
            _Calendar(),
        )
        result = provider.qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertIn("RULE_CAPABILITY_FACT_MISSING", result.reason_codes)
        self.assertIsNone(result.spec)

    def test_missing_mapping_does_not_fall_back_to_current_code(self):
        instrument_id = uuid4()
        result = self._provider(_Stores(instrument_id, include_mapping=False)).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertEqual(result.reason_codes, ("identity_mapping_incomplete",))
        self.assertIsNone(result.spec)

    def test_mapping_for_another_identity_is_blocked(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        stores.mapping = replace(stores.mapping, instrument_id=uuid4())
        result = self._provider(stores).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertIn("identity_mapping_instrument_mismatch", result.reason_codes)
        self.assertIsNone(result.spec)

    def test_cutoff_hides_late_rule_fact(self):
        instrument_id = uuid4()
        late = datetime(2026, 9, 1, tzinfo=UTC)
        result = self._provider(_Stores(instrument_id, known_at=late)).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertIn("RULE_FACT_NOT_COMPLETE", result.reason_codes)
        self.assertIsNone(result.spec)

    def test_single_instrument_path_never_queries_universe(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        result = self._provider(stores).qualify(
            instrument_id, effective_at=EFFECTIVE, data_cutoff=CUTOFF
        )
        self.assertTrue(result.eligible)
        self.assertFalse(stores.universe_called)

    def test_preflight_exposes_the_same_single_instrument_qualification(self):
        instrument_id = uuid4()
        stores = _Stores(instrument_id)
        provider = self._provider(stores)
        result = FixedInstrumentRulePreflightService(
            provider.rule_registry, stores, provider
        ).qualify_instrument(
            instrument_id,
            effective_at=EFFECTIVE,
            data_cutoff=CUTOFF,
        )
        self.assertTrue(result.eligible)
        self.assertFalse(stores.universe_called)


if __name__ == "__main__":
    unittest.main()
