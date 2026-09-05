"""Tests for explicit account selection and frozen run configuration."""

from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.account_profiles import (
    AccountProfileCatalog,
    AccountProfileError,
    AccountProfileStatus,
    AccountProfileUnavailableError,
    AccountSelectionRequiredError,
    BacktestAccountProfile,
)
from app.backtesting.fees import (
    FeeCalculator,
    FeeError,
    FeeRule,
    FeeRoundingLevel,
    FeeRoundingMode,
    FeeSchedule,
)


def schedule(*, complete: bool = True) -> FeeSchedule:
    """Build a normal schedule or an intentionally incomplete draft."""

    rule_kwargs = {
        "key": "commission",
        "category": "commission",
        "rate": "0.0003",
        "minimum": "5",
        "applicability": {"asset_class": "etf"},
    }
    if complete:
        rule_kwargs.update(
            {
                "rounding_level": FeeRoundingLevel.FEE_ITEM,
                "rounding_scope": "commission",
                "rounding_mode": FeeRoundingMode.HALF_UP,
                "rounding_precision": "0.01",
            }
        )
    return FeeSchedule(version=1, key="etf-cny", fee_rules=(FeeRule(**rule_kwargs),))


def profile(*, name: str = "ETF 账户", status: AccountProfileStatus = AccountProfileStatus.ACTIVE) -> BacktestAccountProfile:
    """Build a selectable account profile with no hidden defaults."""

    return BacktestAccountProfile(
        profile_id=uuid4(),
        display_name=name,
        fee_schedule=schedule(),
        status=status,
        metadata={"owner": "research"},
    )


class AccountProfileSelectionTestCase(unittest.TestCase):
    def test_run_creation_requires_an_explicit_profile(self) -> None:
        catalog = AccountProfileCatalog([profile()])

        with self.assertRaises(AccountSelectionRequiredError):
            catalog.create_run_snapshot(None)

    def test_unknown_and_inactive_profiles_are_not_fallbacks(self) -> None:
        inactive = profile(status=AccountProfileStatus.INACTIVE)
        catalog = AccountProfileCatalog([inactive])

        with self.assertRaises(AccountProfileUnavailableError):
            catalog.create_run_snapshot(inactive.profile_id)
        with self.assertRaises(AccountProfileError):
            catalog.create_run_snapshot(uuid4())

    def test_selector_returns_only_active_profiles_in_stable_order(self) -> None:
        zulu = profile(name="Zulu")
        alpha = profile(name="alpha")
        retired = profile(name="Retired", status=AccountProfileStatus.RETIRED)
        catalog = AccountProfileCatalog([zulu, retired, alpha])

        self.assertEqual(
            [item.profile_id for item in catalog.selectable()],
            [alpha.profile_id, zulu.profile_id],
        )


class AccountProfileSnapshotTestCase(unittest.TestCase):
    def test_snapshot_contains_full_fee_rules_and_is_stable_after_replacement(self) -> None:
        original = profile()
        catalog = AccountProfileCatalog([original])

        snapshot = catalog.create_run_snapshot(original.profile_id)
        replacement = BacktestAccountProfile(
            profile_id=original.profile_id,
            display_name="Updated account",
            fee_schedule=FeeSchedule(version=1,
                key="updated-fees",
                fee_rules=(
                    FeeRule(
                        key="commission",
                        category="commission",
                        rate="0.0005",
                        rounding_level=FeeRoundingLevel.FEE_ITEM,
                        rounding_scope="commission",
                        rounding_mode=FeeRoundingMode.DOWN,
                        rounding_precision="0.01",
                    ),
                ),
            ),
        )
        catalog.replace(replacement)

        self.assertEqual(snapshot.display_name, "ETF 账户")
        self.assertEqual(snapshot.fee_schedule.key, "etf-cny")
        self.assertEqual(snapshot.fee_schedule.fee_rules[0].rate, Decimal("0.0003"))
        self.assertNotEqual(snapshot.snapshot_hash, catalog.create_run_snapshot(original.profile_id).snapshot_hash)

    def test_snapshot_hash_is_deterministic_and_metadata_is_detached(self) -> None:
        metadata = {"owner": "research"}
        account = BacktestAccountProfile(
            profile_id=uuid4(),
            display_name="Account",
            fee_schedule=schedule(),
            metadata=metadata,
        )
        metadata["owner"] = "changed"
        snapshot = account.snapshot()

        self.assertEqual(snapshot.metadata["owner"], "research")
        self.assertEqual(len(snapshot.snapshot_hash), 64)
        self.assertEqual(snapshot.snapshot_hash, account.snapshot().snapshot_hash)
        self.assertEqual(len(snapshot.fee_schedule_hash), 64)

    def test_incomplete_rounding_configuration_blocks_run_creation(self) -> None:
        account = BacktestAccountProfile(
            profile_id=uuid4(),
            display_name="Draft account",
            fee_schedule=schedule(complete=False),
        )

        with self.assertRaises(FeeError):
            account.snapshot()

    def test_frozen_schedule_preserves_its_required_version(self) -> None:
        snapshot = profile().snapshot()

        breakdown = FeeCalculator(snapshot.fee_schedule).calculate(
            side="buy",
            notional="1000",
        )

        self.assertEqual(breakdown.schedule_key, "etf-cny")
        self.assertEqual(breakdown.schedule_version, 1)

    def test_zero_cost_fixture_cannot_be_attached_to_a_formal_profile(self) -> None:
        with self.assertRaises(AccountProfileError):
            BacktestAccountProfile(
                profile_id=uuid4(),
                display_name="Test only",
                fee_schedule=FeeSchedule.zero_cost(),
            )

        with self.assertRaises(FeeError):
            FeeSchedule(version=1, key="zero_cost", fee_rules=())


if __name__ == "__main__":
    unittest.main()
