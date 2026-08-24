"""Acceptance tests for task package 05B: account versions and resolution.

Covers the frozen decisions around immutable profile versions,
availability separation, the fixed ``explicit > strategy default > user
default`` resolution order, classified failures without silent
fallback, ``zero_cost@1`` run-mode gating, deletion protection, and
audit-snapshot immutability.
"""

import unittest
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.account_resolution import (
    AccountDefaultReference,
    AccountNotApplicableError,
    AccountNotSelectedError,
    AccountProfileLifecycle,
    AccountProfileUnavailableError,
    AccountProfileVersionCatalog,
    AccountProfileVersionNotFoundError,
    AccountProfileVersionReferencedError,
    AccountResolutionLayer,
    AccountResolver,
    AccountRunMode,
    BacktestAccountProfileVersion,
    ZeroCostFormalForbiddenError,
)
from app.backtesting.fees import (
    FeeError,
    FeeRule,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeSchedule,
    FeeScheduleVersionRegistry,
)

STOCK_ID = UUID("33333333-3333-4333-8333-333333333331")
ETF_ID = UUID("33333333-3333-4333-8333-333333333332")


def commission_rule(rate="0.0003", minimum="5"):
    return FeeRule(
        key="commission",
        category="commission",
        side="both",
        rate=rate,
        minimum=minimum,
        rounding_level=FeeRoundingLevel.FEE_ITEM,
        rounding_scope="commission",
        rounding_mode=FeeRoundingMode.HALF_UP,
        rounding_precision="0.01",
    )


class ResolutionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fee_registry = FeeScheduleVersionRegistry()
        self.fee_registry.register(
            FeeSchedule(key="stock_cash", fee_rules=(commission_rule(),), version=2),
            version=2,
        )
        self.fee_registry.register(
            FeeSchedule(key="etf_cash", fee_rules=(commission_rule(),), version=3),
            version=3,
        )
        self.catalog = AccountProfileVersionCatalog()
        self.catalog.register(
            BacktestAccountProfileVersion(
                profile_id=STOCK_ID,
                version=2,
                display_name="股票现金账户",
                fee_schedule_key="stock_cash",
                fee_schedule_version=2,
            )
        )
        self.catalog.register(
            BacktestAccountProfileVersion(
                profile_id=ETF_ID,
                version=3,
                display_name="ETF现金账户",
                fee_schedule_key="etf_cash",
                fee_schedule_version=3,
            )
        )
        self.resolver = AccountResolver(
            catalog=self.catalog, fee_registry=self.fee_registry
        )

    def ref(self, scope, profile_id, version):
        return AccountDefaultReference(
            scope=scope, profile_id=profile_id, version=version
        )


class ResolutionOrderTests(ResolutionFixture):
    """Acceptance 2: explicit selection beats strategy default beats user
    default; every layer's evaluation lands in the audit trail."""

    def test_explicit_selection_wins_over_both_defaults(self) -> None:
        selection = self.resolver.resolve(
            run_mode="formal",
            explicit=self.ref("strategy", STOCK_ID, 2),
            strategy_default=self.ref("strategy", ETF_ID, 3),
            user_default=self.ref("user", ETF_ID, 3),
        )
        self.assertEqual(selection.profile_version.profile_id, STOCK_ID)
        self.assertEqual(selection.profile_version.version, 2)
        self.assertEqual(
            selection.audit.hit_layer, AccountResolutionLayer.EXPLICIT_SELECTION
        )

    def test_strategy_default_beats_user_default(self) -> None:
        # The documented example: strategy pins etf_cash@3, the user
        # default is stock_cash@2 -- etf_cash@3 must win.
        selection = self.resolver.resolve(
            run_mode="formal",
            strategy_default=self.ref("strategy", ETF_ID, 3),
            user_default=self.ref("user", STOCK_ID, 2),
        )
        self.assertEqual(
            (selection.profile_version.profile_id, selection.profile_version.version),
            (ETF_ID, 3),
        )
        self.assertEqual(
            selection.audit.hit_layer, AccountResolutionLayer.STRATEGY_DEFAULT
        )

    def test_audit_records_all_three_candidates_and_statuses(self) -> None:
        selection = self.resolver.resolve(
            run_mode="formal",
            strategy_default=self.ref("strategy", ETF_ID, 3),
            user_default=self.ref("user", STOCK_ID, 2),
        )
        payload = selection.audit.to_payload()
        self.assertEqual(len(payload["candidates"]), 3)
        by_layer = {
            candidate["layer"]: candidate for candidate in payload["candidates"]
        }
        self.assertEqual(by_layer["explicit_selection"]["outcome"], "not_configured")
        self.assertEqual(by_layer["explicit_selection"]["configured"], False)
        self.assertEqual(by_layer["strategy_default"]["outcome"], "hit")
        self.assertEqual(by_layer["strategy_default"]["status"], "active")
        # Lower layers after a hit are not evaluated at all.
        self.assertNotIn("user_default_evaluation", payload)


class FallbackClassificationTests(ResolutionFixture):
    """Acceptance 3: empty layers fall back; configured-but-invalid
    references fail with a classified reason and never fall through."""

    def test_empty_layers_fall_through_to_the_user_default(self) -> None:
        selection = self.resolver.resolve(
            run_mode="formal",
            user_default=self.ref("user", STOCK_ID, 2),
        )
        self.assertEqual(selection.profile_version.profile_id, STOCK_ID)
        candidates = selection.audit.candidates
        self.assertEqual(candidates[0].outcome, "not_configured")
        self.assertEqual(candidates[1].outcome, "not_configured")

    def test_disabled_strategy_default_fails_instead_of_falling_back(self) -> None:
        # The documented example: etf_cash@3 configured but disabled --
        # resolving to stock_cash@2 would silently swap the account.
        self.catalog.set_availability(ETF_ID, 3, "disabled")
        with self.assertRaises(AccountProfileUnavailableError) as context:
            self.resolver.resolve(
                run_mode="formal",
                strategy_default=self.ref("strategy", ETF_ID, 3),
                user_default=self.ref("user", STOCK_ID, 2),
            )
        self.assertEqual(context.exception.code, "account_version_unavailable")
        self.assertEqual(context.exception.audit.hit_layer, None)
        # The audit names exactly the failed candidate.
        failed = [
            candidate
            for candidate in context.exception.audit.candidates
            if candidate.outcome == "failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].version, 3)

    def test_retired_reference_is_also_unavailable(self) -> None:
        self.catalog.set_availability(STOCK_ID, 2, "retired")
        with self.assertRaises(AccountProfileUnavailableError):
            self.resolver.resolve(
                run_mode="formal",
                user_default=self.ref("user", STOCK_ID, 2),
            )

    def test_missing_pinned_version_fails_as_not_found(self) -> None:
        with self.assertRaises(AccountProfileVersionNotFoundError) as context:
            self.resolver.resolve(
                run_mode="formal",
                user_default=self.ref("user", STOCK_ID, 9),
            )
        self.assertEqual(context.exception.code, "account_version_not_found")

    def test_incompatible_applicability_blocks_without_fallback(self) -> None:
        self.catalog.register(
            BacktestAccountProfileVersion(
                profile_id=ETF_ID,
                version=4,
                display_name="港股通账户",
                fee_schedule_key="etf_cash",
                fee_schedule_version=3,
                applicability={"market": "CN", "asset_class": "etf"},
            )
        )
        with self.assertRaises(AccountNotApplicableError) as context:
            self.resolver.resolve(
                run_mode="formal",
                user_default=self.ref("user", ETF_ID, 4),
                applicability_context={"market": "HK", "asset_class": "etf"},
            )
        self.assertEqual(context.exception.code, "account_not_applicable")

    def test_no_configuration_at_all_requires_an_explicit_choice(self) -> None:
        with self.assertRaises(AccountNotSelectedError) as context:
            self.resolver.resolve(run_mode="formal")
        self.assertEqual(context.exception.code, "account_not_selected")


class ZeroCostRunModeTests(ResolutionFixture):
    """Acceptance 4: zero_cost@1 may be referenced by test runs only."""

    def setUp(self) -> None:
        super().setUp()
        self.zero_id = uuid4()
        self.fee_registry.register(FeeSchedule.zero_cost(), version=1)
        self.catalog.register(
            BacktestAccountProfileVersion(
                profile_id=self.zero_id,
                version=1,
                display_name="零费率测试账户",
                fee_schedule_key="zero_cost",
                fee_schedule_version=1,
            ),
            status=AccountProfileLifecycle.ACTIVE,
        )

    def test_formal_run_cannot_bind_zero_cost(self) -> None:
        with self.assertRaises(ZeroCostFormalForbiddenError) as context:
            self.resolver.resolve(
                run_mode=AccountRunMode.FORMAL,
                explicit=self.ref("strategy", self.zero_id, 1),
            )
        self.assertEqual(context.exception.code, "zero_cost_formal_forbidden")

    def test_test_run_may_bind_zero_cost(self) -> None:
        selection = self.resolver.resolve(
            run_mode=AccountRunMode.TEST,
            explicit=self.ref("strategy", self.zero_id, 1),
        )
        self.assertEqual(selection.fee_schedule.key, "zero_cost")
        self.assertEqual(selection.fee_schedule.version, 1)


class VersionImmutabilityTests(ResolutionFixture):
    """Acceptance 1 and 18: configuration changes create new versions;
    historical bindings and registry history are never rewritten."""

    def test_new_version_does_not_change_a_historical_binding(self) -> None:
        historical = self.resolver.resolve(
            run_mode="formal",
            explicit=self.ref("strategy", ETF_ID, 3),
        )
        hash_before = historical.selection_hash

        # Operators publish a new profile version and a new schedule
        # version afterwards.
        self.fee_registry.register(
            FeeSchedule(key="etf_cash", fee_rules=(commission_rule(minimum="9"),), version=4),
            version=4,
        )
        self.catalog.register(
            BacktestAccountProfileVersion(
                profile_id=ETF_ID,
                version=4,
                display_name="ETF现金账户",
                fee_schedule_key="etf_cash",
                fee_schedule_version=4,
            )
        )
        self.catalog.set_availability(ETF_ID, 3, "disabled")

        # The frozen binding still reports v3 content and its original hash.
        self.assertEqual(historical.profile_version.version, 3)
        self.assertEqual(historical.fee_schedule.metadata, {})
        self.assertEqual(len(historical.fee_schedule.fee_rules), 1)
        rule = historical.fee_schedule.fee_rules[0]
        self.assertEqual(rule.minimum, Decimal("5"))
        self.assertEqual(historical.selection_hash, hash_before)

        # New resolutions see v4, never the mutated old one.
        fresh = AccountResolver(
            catalog=self.catalog, fee_registry=self.fee_registry
        ).resolve(run_mode="formal", explicit=self.ref("strategy", ETF_ID, 4))
        self.assertEqual(fresh.profile_version.version, 4)
        self.assertNotEqual(fresh.selection_hash, hash_before)

    def test_registry_rejects_rewriting_an_existing_fee_version(self) -> None:
        self.fee_registry.register(
            FeeSchedule(key="stock_cash", fee_rules=(commission_rule(),), version=2),
            version=2,
        )  # identical replay is idempotent
        with self.assertRaises(FeeError):
            self.fee_registry.register(
                FeeSchedule(
                    key="stock_cash",
                    fee_rules=(commission_rule(minimum="7"),),
                    version=2,
                ),
                version=2,
            )


class DeletionProtectionTests(ResolutionFixture):
    """Versions referenced by a completed run binding can never be
    physically deleted."""

    def test_referenced_version_cannot_be_deleted(self) -> None:
        self.resolver.resolve(
            run_mode="formal", explicit=self.ref("strategy", ETF_ID, 3)
        )
        with self.assertRaises(AccountProfileVersionReferencedError) as context:
            self.catalog.remove(ETF_ID, 3)
        self.assertEqual(context.exception.code, "account_version_referenced")
        # The version survives and stays resolvable.
        version = self.catalog.get(ETF_ID, 3)
        self.assertEqual(version.display_name, "ETF现金账户")

    def test_unreferenced_version_can_be_removed(self) -> None:
        self.catalog.remove(STOCK_ID, 2)
        with self.assertRaises(Exception):
            self.catalog.get(STOCK_ID, 2)


if __name__ == "__main__":
    unittest.main()
