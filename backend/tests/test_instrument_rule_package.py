"""Tests for the ``china_listed_etf_rules@1`` rule-package domain capability."""

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID, uuid4

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rules import (
    FactQualityStatus,
    ParseMode,
    ResolutionStatus,
    RuleExceptionEntry,
    RuleExceptionPolicy,
    RuleExceptionSetDefinition,
    RuleFactCandidate,
    RuleFieldDefinition,
    RuleFieldType,
    RulePackageDefinition,
    RulePackageIssueCode,
    RulePackageNotRegisteredError,
    RulePackageRegistrationError,
    RulePackageRegistry,
    RulePackageResolution,
    RulePackageResolver,
    PARSE_ORDER,
    build_definition,
    canonical_decimal_string,
    register_china_listed_etf_rules,
)

INSTRUMENT_ID = uuid4()
OTHER_INSTRUMENT_ID = uuid4()

PACKAGE_REF = VersionedReference(key="china_listed_etf_rules", version=1)
EXCEPTION_SET_REF = VersionedReference(key="etf_named_exceptions", version=1)
EXCEPTION_FACT_REF = VersionedReference(key="cash_etf_special_rule", version=1)

EFFECTIVE_DATE = date(2026, 6, 1)
DATA_CUTOFF = datetime(2026, 8, 1, tzinfo=timezone.utc)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=timezone.utc)


def complete_fields() -> dict[str, Any]:
    """A full set of valid v1 fact values for one ordinary ETF.

    Every value here is fixture data for tests only; none of them are
    engine or rule-package defaults.
    """

    return {
        "lot_size": "100",
        "quantity_precision": 0,
        "price_precision": 3,
        "price_tick": "0.001",
        "contract_multiplier": "1",
        "trading_session_template": {
            "key": "cn_etf_session_template",
            "version": 1,
        },
        "settlement_rule_class": "t1_before_open_match",
        "sellable_rule": {
            "statements": [
                "sellable_quantity_restores_next_open",
                "sell_limited_by_available_position",
            ]
        },
        "fee_categories": ["commission"],
        "trading_status_applicability": {
            "suspension": "required",
            "opening_availability": "required",
            "price_limit_tradability": "not_applicable",
        },
        "currency": "cny",
        "order_types": ["limit", "market"],
        "minimum_order_quantity": "100",
        "price_limit_rule": {"key": "cn_etf_price_limit_rule", "version": 1},
        "cash_availability_rule": {
            "key": "cn_cash_availability_rule",
            "version": 1,
        },
        "position_availability_rule": {
            "key": "cn_position_availability_rule",
            "version": 1,
        },
    }


def make_fact(
    instrument_id: UUID = INSTRUMENT_ID,
    fields: dict[str, Any] | None = None,
    **overrides: Any,
) -> RuleFactCandidate:
    """Build one ordinary (non-exception) fact candidate."""

    kwargs: dict[str, Any] = dict(
        fact_reference=VersionedReference(key="etf_rule_fact", version=1),
        instrument_id=instrument_id,
        package_reference=PACKAGE_REF,
        source="fixture_provider",
        source_revision="rev-1",
        known_at=KNOWN_AT,
        observed_at=KNOWN_AT,
        quality_status=FactQualityStatus.COMPLETE,
        fixture_only=False,
        content_hash="a" * 64,
        fields=complete_fields() if fields is None else fields,
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )
    kwargs.update(overrides)
    return RuleFactCandidate(**kwargs)


def make_exception_set(
    instrument_id: UUID = INSTRUMENT_ID,
    entries: tuple[RuleExceptionEntry, ...] | None = None,
) -> RuleExceptionSetDefinition:
    """Build a one-entry named-exception set bound to the ETF package."""

    if entries is None:
        entries = (
            RuleExceptionEntry(
                instrument_id=instrument_id,
                exception_fact_ref=EXCEPTION_FACT_REF,
                valid_from=date(2025, 1, 1),
                valid_to=None,
            ),
        )
    return RuleExceptionSetDefinition(
        reference=EXCEPTION_SET_REF,
        package_reference=PACKAGE_REF,
        entries=entries,
    )


def make_exception_fact(
    instrument_id: UUID = INSTRUMENT_ID,
    fields: dict[str, Any] | None = None,
) -> RuleFactCandidate:
    """Build the alternate fact an exception entry points at."""

    exception_fields = complete_fields()
    exception_fields.update(
        {
            "lot_size": "1",
            "price_precision": 4,
            "price_tick": "0.0001",
        }
    )
    if fields is not None:
        exception_fields.update(fields)
    return RuleFactCandidate(
        fact_reference=EXCEPTION_FACT_REF,
        instrument_id=instrument_id,
        package_reference=PACKAGE_REF,
        source="exception_fact_provider",
        source_revision="rev-1",
        known_at=KNOWN_AT,
        observed_at=KNOWN_AT,
        quality_status=FactQualityStatus.COMPLETE,
        fixture_only=False,
        content_hash="b" * 64,
        fields=exception_fields,
        exception_fact_ref=EXCEPTION_FACT_REF,
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )


def resolve(
    facts,
    exception_sets=(),
    mode: ParseMode = ParseMode.FORMAL,
    asset_class: str = "etf",
):
    """Resolve through a fresh registry holding only the v1 ETF package."""

    registry = RulePackageRegistry()
    register_china_listed_etf_rules(registry)
    resolver = RulePackageResolver(registry)
    return resolver.resolve(
        PACKAGE_REF,
        instrument_id=INSTRUMENT_ID,
        asset_class=asset_class,
        effective_date=EFFECTIVE_DATE,
        data_cutoff=DATA_CUTOFF,
        facts=facts,
        exception_sets=exception_sets,
        mode=mode,
    )


def issue_codes(resolution) -> set[str]:
    return {issue.code.value for issue in resolution.issues}


class RegistryAndDefinitionTestCase(unittest.TestCase):
    """Registration, exact versioning, and immutability requirements."""

    def setUp(self) -> None:
        self.registry = RulePackageRegistry()
        self.definition = register_china_listed_etf_rules(self.registry)

    def test_package_resolves_by_exact_key_and_version(self) -> None:
        self.assertIs(self.registry.get(PACKAGE_REF), self.definition)
        self.assertIs(self.registry.require(PACKAGE_REF), self.definition)

    def test_missing_version_never_falls_back_to_latest(self) -> None:
        missing_version = VersionedReference(
            key="china_listed_etf_rules", version=2
        )
        self.assertIsNone(self.registry.get(missing_version))
        with self.assertRaises(RulePackageNotRegisteredError):
            self.registry.require(missing_version)

    def test_duplicate_registration_is_rejected(self) -> None:
        with self.assertRaises(RulePackageRegistrationError):
            register_china_listed_etf_rules(self.registry)

    def test_registered_definition_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.definition.parse_order = ()

    def test_list_is_stably_sorted_by_key_then_version(self) -> None:
        other = RulePackageDefinition(
            reference=VersionedReference(key="aaa_other_rules", version=1),
            supported_asset_classes=frozenset({"etf"}),
            field_definitions=(
                RuleFieldDefinition(
                    "lot_size", RuleFieldType.POSITIVE_DECIMAL, True
                ),
            ),
            capability_schema=("suspension",),
            known_settlement_rule_classes=frozenset({"same_day"}),
            formal_settlement_rule_classes=frozenset({"same_day"}),
            exception_policy=self.definition.exception_policy,
            parse_order=("load",),
        )
        self.registry.register(other)
        keys = [item.reference.key for item in self.registry.list()]
        self.assertEqual(keys, ["aaa_other_rules", "china_listed_etf_rules"])

    def test_v1_defines_exactly_sixteen_required_fields(self) -> None:
        names = self.definition.field_names()
        self.assertEqual(len(names), 16)
        self.assertEqual(len(set(names)), 16)
        for field_definition in self.definition.field_definitions:
            self.assertTrue(field_definition.required)

    def test_definition_hash_is_stable_and_content_sensitive(self) -> None:
        rebuilt = build_definition()
        self.assertEqual(self.definition.semantic_hash, rebuilt.semantic_hash)
        modified_fields = rebuilt.field_definitions + (
            RuleFieldDefinition(
                "extra_field", RuleFieldType.POSITIVE_DECIMAL, True
            ),
        )
        modified = RulePackageDefinition(
            reference=rebuilt.reference,
            supported_asset_classes=rebuilt.supported_asset_classes,
            field_definitions=modified_fields,
            capability_schema=rebuilt.capability_schema,
            known_settlement_rule_classes=(
                rebuilt.known_settlement_rule_classes
            ),
            formal_settlement_rule_classes=(
                rebuilt.formal_settlement_rule_classes
            ),
            exception_policy=rebuilt.exception_policy,
            parse_order=rebuilt.parse_order,
        )
        self.assertNotEqual(
            self.definition.semantic_hash, modified.semantic_hash
        )

    def test_formal_settlement_support_is_phase1_only(self) -> None:
        self.assertEqual(
            self.definition.formal_settlement_rule_classes,
            {"t1_before_open_match"},
        )
        self.assertLess(
            self.definition.formal_settlement_rule_classes,
            self.definition.known_settlement_rule_classes,
        )

    def test_exception_policy_rejects_non_identity_match_keys(self) -> None:
        with self.assertRaises(DomainValidationError):
            RuleExceptionPolicy(("trading_code", "validity_interval"))

    def test_exception_policy_order_is_canonical(self) -> None:
        first = RuleExceptionPolicy(("instrument_id", "validity_interval"))
        second = RuleExceptionPolicy(("validity_interval", "instrument_id"))
        self.assertEqual(first, second)

    def test_contract_collections_reject_scalar_text(self) -> None:
        with self.assertRaises(DomainValidationError):
            RulePackageDefinition(
                reference=PACKAGE_REF,
                supported_asset_classes="etf",
                field_definitions=self.definition.field_definitions,
                capability_schema=self.definition.capability_schema,
                known_settlement_rule_classes=(
                    self.definition.known_settlement_rule_classes
                ),
                formal_settlement_rule_classes=(
                    self.definition.formal_settlement_rule_classes
                ),
                exception_policy=self.definition.exception_policy,
                parse_order=self.definition.parse_order,
            )


class ReadyPathTestCase(unittest.TestCase):
    """Example A: ordinary ETF without exceptions resolves ready."""

    def test_plain_fact_resolves_ready_with_fact_sourced_values(self) -> None:
        resolution = resolve([make_fact()])
        self.assertIs(resolution.status, ResolutionStatus.READY)
        self.assertEqual(resolution.issues, ())
        self.assertIsNone(resolution.exception_reference)
        self.assertEqual(resolution.normalized_values["lot_size"], Decimal("100"))
        self.assertEqual(
            resolution.normalized_values["price_tick"], Decimal("0.001")
        )
        self.assertEqual(resolution.normalized_values["currency"], "CNY")
        self.assertEqual(
            resolution.normalized_values["order_types"], ("limit", "market")
        )
        self.assertEqual(
            resolution.normalized_values["trading_session_template"],
            VersionedReference(key="cn_etf_session_template", version=1),
        )
        self.assertEqual(len(resolution.selected_facts), 1)
        self.assertEqual(
            resolution.selected_facts[0].source, "fixture_provider"
        )

    def test_not_applicable_declaration_is_preserved(self) -> None:
        # Example E: "not_applicable" is an explicit declaration and must
        # survive into the resolution, not be read as a missing field.
        resolution = resolve([make_fact()])
        self.assertEqual(
            resolution.capability_declarations,
            {
                "suspension": "required",
                "opening_availability": "required",
                "price_limit_tradability": "not_applicable",
            },
        )

    def test_identical_inputs_produce_identical_hash_and_order(self) -> None:
        first = resolve([make_fact()])
        second = resolve([make_fact()])
        self.assertEqual(first.semantic_hash, second.semantic_hash)
        self.assertEqual(first.parse_order, second.parse_order)
        self.assertEqual(first.normalized_values, second.normalized_values)

    def test_resolver_exposes_the_frozen_parse_order(self) -> None:
        resolution = resolve([make_fact()])
        self.assertEqual(resolution.parse_order, PARSE_ORDER)

    def test_fact_input_order_does_not_change_semantic_hash(self) -> None:
        normal = make_fact()
        exception = make_exception_fact()
        first = resolve(
            [normal, exception], exception_sets=[make_exception_set()]
        )
        second = resolve(
            [exception, normal], exception_sets=[make_exception_set()]
        )
        self.assertEqual(first.status, ResolutionStatus.READY)
        self.assertEqual(second.status, ResolutionStatus.READY)
        self.assertEqual(first.semantic_hash, second.semantic_hash)


class NamedExceptionTestCase(unittest.TestCase):
    """Example B: exceptions route to alternate facts, never carry values."""

    def test_exception_overrides_fields_via_referenced_fact(self) -> None:
        resolution = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=[make_exception_set()],
        )
        self.assertIs(resolution.status, ResolutionStatus.READY)
        self.assertEqual(resolution.exception_reference, EXCEPTION_FACT_REF)
        self.assertEqual(resolution.exception_set_reference, EXCEPTION_SET_REF)
        self.assertEqual(resolution.normalized_values["lot_size"], Decimal("1"))
        self.assertEqual(
            resolution.normalized_values["price_tick"], Decimal("0.0001")
        )
        self.assertEqual(len(resolution.selected_facts), 2)
        # The summary records which exception-set version routed here.
        self.assertEqual(
            resolution.selected_facts[0].exception_set_reference, None
        )
        self.assertEqual(
            resolution.selected_facts[1].exception_set_reference,
            EXCEPTION_SET_REF,
        )

    def test_unmatched_exception_falls_back_to_ordinary_fact(self) -> None:
        exception_set = make_exception_set(instrument_id=OTHER_INSTRUMENT_ID)
        resolution = resolve([make_fact()], exception_sets=[exception_set])
        self.assertIs(resolution.status, ResolutionStatus.READY)
        self.assertIsNone(resolution.exception_reference)
        self.assertIsNone(resolution.exception_set_reference)
        self.assertEqual(resolution.normalized_values["lot_size"], Decimal("100"))

    def test_exception_fact_missing_returns_structured_block(self) -> None:
        resolution = resolve([make_fact()], exception_sets=[make_exception_set()])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_EXCEPTION_FACT_MISSING.value,
            issue_codes(resolution),
        )
        self.assertEqual(resolution.normalized_values, {})

    def test_overlapping_exception_intervals_return_structured_block(self) -> None:
        first = RuleExceptionEntry(
            instrument_id=INSTRUMENT_ID,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2025, 1, 1),
            valid_to=date(2026, 12, 31),
        )
        second = RuleExceptionEntry(
            instrument_id=INSTRUMENT_ID,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2026, 1, 1),
            valid_to=None,
        )
        overlapping = RuleExceptionSetDefinition(
            reference=EXCEPTION_SET_REF,
            package_reference=PACKAGE_REF,
            entries=(first, second),
        )
        resolution = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=[overlapping],
        )
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT.value,
            issue_codes(resolution),
        )
        # No single set was selected, but every participating set version
        # is recorded in stable order for audit.
        self.assertIsNone(resolution.exception_reference)
        self.assertIsNone(resolution.exception_set_reference)
        self.assertEqual(
            resolution.exception_set_references, (EXCEPTION_SET_REF,)
        )

    def test_cross_set_interval_conflict_records_and_hashes_participants(
        self,
    ) -> None:
        # Two distinct exception sets whose intervals overlap on the
        # effective date: all participants must be recorded, and two
        # conflicts involving different set versions must hash
        # differently.
        def entry(ref: VersionedReference) -> RuleExceptionEntry:
            return RuleExceptionEntry(
                instrument_id=INSTRUMENT_ID,
                exception_fact_ref=ref,
                valid_from=date(2025, 1, 1),
                valid_to=None,
            )

        def pair(first_version: int, second_version: int):
            first = RuleExceptionSetDefinition(
                reference=VersionedReference(
                    key="etf_named_exceptions", version=first_version
                ),
                package_reference=PACKAGE_REF,
                entries=(entry(EXCEPTION_FACT_REF),),
            )
            second = RuleExceptionSetDefinition(
                reference=VersionedReference(
                    key="etf_named_exceptions", version=second_version
                ),
                package_reference=PACKAGE_REF,
                entries=(entry(EXCEPTION_FACT_REF),),
            )
            return [first, second]

        combo_a = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=pair(1, 2),
        )
        combo_b = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=pair(3, 4),
        )
        for resolution in (combo_a, combo_b):
            self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
            self.assertIn(
                RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT.value,
                issue_codes(resolution),
                resolution.issues,
            )
            self.assertIsNone(resolution.exception_set_reference)
        self.assertEqual(len(combo_a.exception_set_references), 2)
        self.assertEqual(
            [ref.version for ref in combo_a.exception_set_references], [1, 2]
        )
        self.assertEqual(
            [ref.version for ref in combo_b.exception_set_references], [3, 4]
        )
        self.assertNotEqual(combo_a.semantic_hash, combo_b.semantic_hash)

    def test_ordinary_and_exception_conflicts_block_together(self) -> None:
        # A conflict in either side of the fixed parse order must not be
        # hidden by the other side resolving successfully.
        first = RuleExceptionEntry(
            instrument_id=INSTRUMENT_ID,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2025, 1, 1),
            valid_to=date(2027, 1, 1),
        )
        second = RuleExceptionEntry(
            instrument_id=INSTRUMENT_ID,
            exception_fact_ref=EXCEPTION_FACT_REF,
            valid_from=date(2026, 1, 1),
            valid_to=None,
        )
        resolution = resolve(
            [
                make_fact(),
                make_fact(
                    fact_reference=VersionedReference(
                        key="etf_rule_fact", version=2
                    ),
                    source_revision="rev-2",
                ),
                make_exception_fact(),
            ],
            exception_sets=[make_exception_set(entries=(first, second))],
        )
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_CONFLICT.value,
            issue_codes(resolution),
        )
        self.assertIn(
            RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT.value,
            issue_codes(resolution),
        )

    def test_exception_set_bound_to_other_package_is_target_mismatch(self) -> None:
        mismatched = RuleExceptionSetDefinition(
            reference=EXCEPTION_SET_REF,
            package_reference=VersionedReference(key="other_rules", version=1),
            entries=(
                RuleExceptionEntry(
                    instrument_id=INSTRUMENT_ID,
                    exception_fact_ref=EXCEPTION_FACT_REF,
                    valid_from=date(2025, 1, 1),
                    valid_to=None,
                ),
            ),
        )
        resolution = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=[mismatched],
        )
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_EXCEPTION_TARGET_MISMATCH.value,
            issue_codes(resolution),
        )
        # Even a package-mismatched set with a covering entry participates
        # in the recorded exception-set versions.
        self.assertEqual(
            resolution.exception_set_references, (EXCEPTION_SET_REF,)
        )

    def test_exception_interval_half_open_semantics(self) -> None:
        # valid_to is exclusive: a fact ending on 2026-06-01 no longer
        # covers the effective date, and the ordinary fact applies.
        exception_set = make_exception_set(
            entries=(
                RuleExceptionEntry(
                    instrument_id=INSTRUMENT_ID,
                    exception_fact_ref=EXCEPTION_FACT_REF,
                    valid_from=date(2025, 1, 1),
                    valid_to=EFFECTIVE_DATE,
                ),
            )
        )
        resolution = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=[exception_set],
        )
        self.assertIs(resolution.status, ResolutionStatus.READY)
        self.assertIsNone(resolution.exception_reference)
        self.assertEqual(resolution.normalized_values["lot_size"], Decimal("100"))


class MissingAndInvalidFieldTestCase(unittest.TestCase):
    """Example C plus type, precision, and multiple validation."""

    def test_missing_required_field_blocks_without_defaults(self) -> None:
        fields = complete_fields()
        del fields["price_tick"]
        resolution = resolve([make_fact(fields=fields)])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            issue_codes(resolution),
            {RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING.value},
        )
        issue = resolution.issues[0]
        self.assertEqual(issue.field, "price_tick")
        self.assertEqual(resolution.normalized_values, {})

    def test_every_required_field_missing_is_reported(self) -> None:
        for name in build_definition().field_names():
            fields = complete_fields()
            del fields[name]
            resolution = resolve([make_fact(fields=fields)])
            self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
            self.assertIn(
                RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING.value,
                issue_codes(resolution),
            )
            self.assertTrue(
                any(issue.field == name for issue in resolution.issues),
                f"missing field {name} was not reported",
            )

    def test_binary_float_and_bool_are_rejected(self) -> None:
        fields = complete_fields()
        fields["lot_size"] = 100.0
        resolution = resolve([make_fact(fields=fields)])
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_INVALID.value, issue_codes(resolution)
        )

        fields = complete_fields()
        fields["quantity_precision"] = True
        resolution = resolve([make_fact(fields=fields)])
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_INVALID.value, issue_codes(resolution)
        )

    def test_nan_and_infinity_are_rejected(self) -> None:
        for bad in (Decimal("NaN"), Decimal("Infinity")):
            fields = complete_fields()
            fields["contract_multiplier"] = bad
            resolution = resolve([make_fact(fields=fields)])
            self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
            self.assertIn(
                RulePackageIssueCode.RULE_FIELD_INVALID.value,
                issue_codes(resolution),
            )

    def test_price_tick_must_be_representable_with_price_precision(self) -> None:
        fields = complete_fields()
        fields["price_tick"] = "0.0001"
        resolution = resolve([make_fact(fields=fields)])
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_INVALID.value, issue_codes(resolution)
        )

    def test_minimum_order_quantity_must_be_lot_multiple(self) -> None:
        fields = complete_fields()
        fields["minimum_order_quantity"] = "150"
        resolution = resolve([make_fact(fields=fields)])
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_INVALID.value, issue_codes(resolution)
        )

    def test_order_types_must_include_market(self) -> None:
        fields = complete_fields()
        fields["order_types"] = ["limit"]
        resolution = resolve([make_fact(fields=fields)])
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_INVALID.value, issue_codes(resolution)
        )


class SettlementClassTestCase(unittest.TestCase):
    """Example D: known-but-unsupported classes parse then formally block."""

    def test_same_day_is_recognized_but_unsupported(self) -> None:
        fields = complete_fields()
        fields["settlement_rule_class"] = "same_day"
        resolution = resolve([make_fact(fields=fields)])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            issue_codes(resolution),
            {RulePackageIssueCode.RULE_SETTLEMENT_UNSUPPORTED.value},
        )

    def test_t_plus_1_is_recognized_but_unsupported(self) -> None:
        fields = complete_fields()
        fields["settlement_rule_class"] = "t_plus_1"
        resolution = resolve([make_fact(fields=fields)])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_SETTLEMENT_UNSUPPORTED.value,
            issue_codes(resolution),
        )

    def test_unknown_settlement_class_is_reported_as_unknown(self) -> None:
        fields = complete_fields()
        fields["settlement_rule_class"] = "mystery_settlement"
        resolution = resolve([make_fact(fields=fields)])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            issue_codes(resolution),
            {RulePackageIssueCode.RULE_SETTLEMENT_UNKNOWN.value},
        )

    def test_missing_settlement_class_never_defaults_to_formal_class(self) -> None:
        fields = complete_fields()
        del fields["settlement_rule_class"]
        resolution = resolve([make_fact(fields=fields)])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            issue_codes(resolution),
            {RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING.value},
        )


class FactSelectionTestCase(unittest.TestCase):
    """Validity/cutoff selection, conflicts, and package consistency."""

    def test_equally_applicable_facts_conflict_instead_of_last_write_wins(
        self,
    ) -> None:
        second = make_fact(source="another_provider")
        resolution = resolve([make_fact(), second])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_FIELD_CONFLICT.value,
            issue_codes(resolution),
        )

    def test_fact_known_after_cutoff_is_invisible(self) -> None:
        late_fact = make_fact(
            known_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        resolution = resolve([late_fact])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_FACT_NOT_COMPLETE.value,
            issue_codes(resolution),
        )

    def test_fact_for_wrong_package_reference_is_reported(self) -> None:
        mismatched = make_fact(
            package_reference=VersionedReference(key="other_rules", version=1)
        )
        resolution = resolve([mismatched])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_PACKAGE_MISMATCH.value,
            issue_codes(resolution),
        )

    def test_non_etf_asset_class_is_blocked(self) -> None:
        resolution = resolve([make_fact()], asset_class="stock")
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_PACKAGE_MISMATCH.value,
            issue_codes(resolution),
        )

    def test_incomplete_quality_blocks(self) -> None:
        fact = make_fact(quality_status=FactQualityStatus.INCOMPLETE)
        resolution = resolve([fact])
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_FACT_NOT_COMPLETE.value,
            issue_codes(resolution),
        )


class ModeGateTestCase(unittest.TestCase):
    """Example F: formal mode rejects fixtures; acceptance modes allow them."""

    def test_formal_mode_rejects_fixture_fact(self) -> None:
        fact = make_fact(fixture_only=True)
        resolution = resolve([fact], mode=ParseMode.FORMAL)
        self.assertIs(resolution.status, ResolutionStatus.BLOCKED)
        self.assertIn(
            RulePackageIssueCode.RULE_FIXTURE_SOURCE_FORBIDDEN.value,
            issue_codes(resolution),
        )
        self.assertEqual(resolution.normalized_values, {})

    def test_phase1_fixture_mode_accepts_fixture_fact(self) -> None:
        fact = make_fact(fixture_only=True)
        resolution = resolve([fact], mode=ParseMode.PHASE1_FIXTURE)
        self.assertIs(resolution.status, ResolutionStatus.READY)

    def test_internal_link_acceptance_mode_accepts_fixture_fact(self) -> None:
        fact = make_fact(fixture_only=True)
        resolution = resolve(
            [fact], mode=ParseMode.INTERNAL_LINK_ACCEPTANCE
        )
        self.assertIs(resolution.status, ResolutionStatus.READY)


class SemanticHashTestCase(unittest.TestCase):
    """Stable hash inputs and canonical decimal serialization."""

    def test_decimal_canonicalization_strips_exponents_and_zeros(self) -> None:
        self.assertEqual(canonical_decimal_string(Decimal("100")), "100")
        self.assertEqual(canonical_decimal_string(Decimal("1E+2")), "100")
        self.assertEqual(canonical_decimal_string(Decimal("0.0010")), "0.001")
        self.assertEqual(canonical_decimal_string(Decimal("0.000")), "0")

    def test_exception_reference_participates_in_hash(self) -> None:
        plain = resolve([make_fact()])
        with_exception = resolve(
            [make_fact(), make_exception_fact()],
            exception_sets=[make_exception_set()],
        )
        self.assertNotEqual(plain.semantic_hash, with_exception.semantic_hash)

    def test_exception_set_version_participates_in_hash(self) -> None:
        # Two exception-set versions pointing at the SAME fact reference
        # must still produce distinguishable resolutions and hashes.
        entries = (
            RuleExceptionEntry(
                instrument_id=INSTRUMENT_ID,
                exception_fact_ref=EXCEPTION_FACT_REF,
                valid_from=date(2025, 1, 1),
                valid_to=None,
            ),
        )
        set_v1 = RuleExceptionSetDefinition(
            reference=VersionedReference(key="etf_named_exceptions", version=1),
            package_reference=PACKAGE_REF,
            entries=entries,
        )
        set_v2 = RuleExceptionSetDefinition(
            reference=VersionedReference(key="etf_named_exceptions", version=2),
            package_reference=PACKAGE_REF,
            entries=entries,
        )
        first = resolve(
            [make_fact(), make_exception_fact()], exception_sets=[set_v1]
        )
        second = resolve(
            [make_fact(), make_exception_fact()], exception_sets=[set_v2]
        )
        self.assertEqual(first.exception_reference, EXCEPTION_FACT_REF)
        self.assertEqual(second.exception_reference, EXCEPTION_FACT_REF)
        self.assertNotEqual(
            first.exception_set_reference, second.exception_set_reference
        )
        self.assertNotEqual(first.semantic_hash, second.semantic_hash)

    def test_blocked_hash_distinguishes_fact_revisions(self) -> None:
        # Same blocking reason (missing price_tick), different fact
        # revisions: the blocked hash must keep the provenance distinct
        # while continuing to exclude Chinese issue messages.
        missing_tick = complete_fields()
        del missing_tick["price_tick"]
        first = resolve([make_fact(fields=dict(missing_tick))])
        second = resolve(
            [make_fact(fields=dict(missing_tick), source_revision="rev-9")]
        )
        self.assertIs(first.status, ResolutionStatus.BLOCKED)
        self.assertIs(second.status, ResolutionStatus.BLOCKED)
        self.assertEqual(
            [issue.code for issue in first.issues],
            [issue.code for issue in second.issues],
        )
        self.assertNotEqual(first.semantic_hash, second.semantic_hash)

    def test_blocked_hash_is_deterministic_and_message_free(self) -> None:
        fields = complete_fields()
        del fields["price_tick"]
        first = resolve([make_fact(fields=fields)])
        second = resolve([make_fact(fields=fields)])
        self.assertEqual(first.semantic_hash, second.semantic_hash)
        self.assertEqual(
            [issue.code for issue in first.issues],
            [issue.code for issue in second.issues],
        )


class ResolutionDtoBoundaryTestCase(unittest.TestCase):
    """Direct DTO construction must enforce the immutability contract."""

    def build(self, **overrides: Any) -> RulePackageResolution:
        kwargs: dict[str, Any] = dict(
            status=ResolutionStatus.READY,
            package_reference=PACKAGE_REF,
            parse_order=("step_one", "step_two"),
            parser_revision="rule-package-resolver@1",
            semantic_hash="a" * 64,
            exception_set_references=[EXCEPTION_SET_REF],
            normalized_values={"lot_size": Decimal("100")},
            capability_declarations={"suspension": "required"},
        )
        kwargs.update(overrides)
        return RulePackageResolution(**kwargs)

    def test_exception_set_references_are_sorted_deduped_and_frozen(self) -> None:
        set_v1 = VersionedReference(key="etf_named_exceptions", version=1)
        set_v2 = VersionedReference(key="etf_named_exceptions", version=2)
        resolution = self.build(
            exception_set_references=[set_v2, set_v1, set_v2]
        )
        self.assertEqual(
            resolution.exception_set_references, (set_v1, set_v2)
        )
        with self.assertRaises(AttributeError):
            resolution.exception_set_references.append(set_v1)

    def test_non_reference_exception_set_element_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.build(exception_set_references=["etf_named_exceptions@1"])

    def test_non_iterable_exception_set_container_is_rejected(self) -> None:
        # Previously None/ints crashed with a bare TypeError at iteration.
        for bad in (None, 1, 1.5):
            with self.assertRaises(DomainValidationError):
                self.build(exception_set_references=bad)

    def test_bogus_dunder_iter_container_is_rejected(self) -> None:
        # A class whose __iter__ attribute exists but is not callable
        # passes a hasattr probe yet cannot be iterated; the contract
        # still requires a domain error instead of a bare TypeError.
        class Broken:
            __iter__ = None

        with self.assertRaises(DomainValidationError):
            self.build(exception_set_references=Broken())

    def test_non_mapping_normalized_values_are_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.build(normalized_values=["bad"])
        with self.assertRaises(DomainValidationError):
            self.build(normalized_values=None)

    def test_normalized_values_keys_must_be_field_name_strings(self) -> None:
        # A non-string key would still deep-freeze fine but could collide
        # in the canonical hash payload with a stringified variant.
        with self.assertRaises(DomainValidationError):
            self.build(normalized_values={1: "value"})

    def test_capability_declarations_must_map_strings_to_strings(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.build(capability_declarations={"suspension": 1})
        with self.assertRaises(DomainValidationError):
            self.build(capability_declarations={1: "required"})

    def test_non_mapping_capability_declarations_are_rejected(self) -> None:
        # Previously this crashed with AttributeError instead of a
        # structured domain validation error.
        with self.assertRaises(DomainValidationError):
            self.build(capability_declarations=["suspension=required"])
        with self.assertRaises(DomainValidationError):
            self.build(capability_declarations=None)

    def test_valid_mapping_construction_freezes_nested_values(self) -> None:
        resolution = self.build(
            normalized_values={
                "trading_status_applicability": {"suspension": "required"}
            }
        )
        self.assertEqual(
            resolution.normalized_values["trading_status_applicability"],
            {"suspension": "required"},
        )
        with self.assertRaises(TypeError):
            resolution.normalized_values["trading_status_applicability"][
                "suspension"
            ] = "not_applicable"


class DeepImmutabilityTestCase(unittest.TestCase):
    """Nested structures inside DTOs must be frozen at every level."""

    def test_fact_fields_are_deeply_immutable(self) -> None:
        fact = make_fact()
        with self.assertRaises(TypeError):
            fact.fields["lot_size"] = "200"
        with self.assertRaises(TypeError):
            fact.fields["trading_status_applicability"]["suspension"] = (
                "not_applicable"
            )

    def test_resolution_normalized_values_are_deeply_immutable(self) -> None:
        resolution = resolve([make_fact()])
        with self.assertRaises(TypeError):
            resolution.normalized_values["lot_size"] = Decimal("200")
        with self.assertRaises(TypeError):
            resolution.normalized_values["trading_status_applicability"][
                "suspension"
            ] = "not_applicable"

    def test_issue_details_are_deeply_immutable(self) -> None:
        from app.instruments.rules.contracts import RulePackageIssue

        issue = RulePackageIssue(
            code=RulePackageIssueCode.RULE_FIELD_INVALID,
            message="字段 lot_size 非法",
            field="lot_size",
            details={"nested": {"members": ["a", "b"]}},
        )
        with self.assertRaises(TypeError):
            issue.details["nested"] = {}
        # Lists are frozen into tuples, so mutation methods do not even
        # exist; both outcomes prove immutability.
        with self.assertRaises((TypeError, AttributeError)):
            issue.details["nested"]["members"].append("c")


if __name__ == "__main__":
    unittest.main()
