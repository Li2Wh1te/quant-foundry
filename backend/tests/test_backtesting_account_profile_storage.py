"""Tests for persistent account-profile CRUD and name-based selection APIs."""

from pathlib import Path
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.backtesting.account_profiles import AccountProfileStatus
from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.repository import BacktestAccountProfileRepository
from app.backtesting.schemas import (
    AccountProfileCreateRequest,
    AccountProfileUpdateRequest,
    FeeScheduleRequest,
)
from app.backtesting.service import (
    AccountProfileNameConflictError,
    AccountProfileService,
    AccountProfileValidationError,
)
from app.core.config import Settings
from app.main import create_app


def fee_schedule_payload() -> dict[str, object]:
    """Return a complete explicit fee schedule payload for service tests."""

    return {
        "key": "etf-cny",
        "fee_rules": [
            {
                "key": "commission",
                "category": "commission",
                "rate": "0.0003",
                "minimum": "5",
                "rounding_level": "fee_item",
                "rounding_scope": "commission",
                "rounding_mode": "half_up",
                "rounding_precision": "0.01",
            }
        ],
        "metadata": {"currency": "CNY"},
    }


class AccountProfileModelTestCase(unittest.TestCase):
    """Verify the table stores a name and does not expose version/default fields."""

    def test_model_has_named_profile_and_json_fee_configuration(self) -> None:
        sql = str(
            CreateTable(BacktestAccountProfileRecord.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("name VARCHAR(100) NOT NULL", sql)
        self.assertIn("fee_rules JSONB DEFAULT '[]'::jsonb NOT NULL", sql)
        self.assertNotIn("version", {column.name for column in BacktestAccountProfileRecord.__table__.columns})
        self.assertNotIn("default", {column.name for column in BacktestAccountProfileRecord.__table__.columns})

    def test_migration_contains_name_search_index_and_no_run_table(self) -> None:
        migration = (
            Path(__file__).parents[1]
            / "app/db/migrations/versions/20260822_01_add_backtest_account_profiles.py"
        ).read_text(encoding="utf-8")
        self.assertIn('sa.Column(\n            "name"', migration)
        self.assertIn("uq_backtest_account_profiles_name_ci", migration)
        self.assertNotIn("backtest_runs", migration)


class AccountProfileSchemaTestCase(unittest.TestCase):
    """Ensure the API requires an explicit named account and fee schedule."""

    def test_create_request_requires_name_and_nested_fee_schedule(self) -> None:
        payload = AccountProfileCreateRequest.model_validate(
            {"name": "研究账户", "fee_schedule": fee_schedule_payload()}
        )
        self.assertEqual(payload.name, "研究账户")
        self.assertEqual(payload.status, AccountProfileStatus.ACTIVE)
        self.assertIsInstance(payload.fee_schedule, FeeScheduleRequest)

        with self.assertRaises(ValueError):
            AccountProfileCreateRequest.model_validate(
                {"fee_schedule": fee_schedule_payload()}
            )
        with self.assertRaises(ValueError):
            AccountProfileUpdateRequest.model_validate({})

    def test_account_crud_routes_are_authenticated_and_exclude_run_creation(self) -> None:
        app = create_app(
            Settings(
                api_token="a" * 64,
                database_password="test-secret",
                _env_file=None,
            )
        )
        paths = app.openapi()["paths"]
        self.assertEqual(
            set(path for path in paths if path.startswith("/api/admin/backtest-account-profiles")),
            {
                "/api/admin/backtest-account-profiles",
                "/api/admin/backtest-account-profiles/{profile_id}",
            },
        )
        self.assertNotIn("/api/backtest-runs", paths)
        for path in paths:
            if not path.startswith("/api/admin/backtest-account-profiles"):
                continue
            for operation in paths[path].values():
                self.assertEqual(operation.get("security"), [{"API Token": []}])


class AccountProfileServiceTestCase(unittest.TestCase):
    """Exercise CRUD validation without requiring a live PostgreSQL server."""

    def setUp(self) -> None:
        self.session = Mock()
        self.service = AccountProfileService(self.session)
        self.service.repository = Mock(spec=BacktestAccountProfileRepository)

    def test_create_persists_name_and_complete_fee_rules(self) -> None:
        self.service.repository.name_exists.return_value = False

        record = self.service.create(
            name="  研究账户  ",
            status=AccountProfileStatus.ACTIVE,
            fee_schedule=fee_schedule_payload(),
            metadata={"owner": "quant"},
        )

        self.assertEqual(record.name, "研究账户")
        self.assertEqual(record.fee_schedule_key, "etf-cny")
        self.assertEqual(record.fee_rules[0]["rounding_precision"], "0.01")
        self.assertEqual(record.profile_metadata, {"owner": "quant"})
        self.session.flush.assert_called_once_with()

    def test_create_rejects_duplicate_name_and_incomplete_rounding(self) -> None:
        self.service.repository.name_exists.return_value = True
        with self.assertRaises(AccountProfileNameConflictError):
            self.service.create(
                name="研究账户",
                status=AccountProfileStatus.ACTIVE,
                fee_schedule=fee_schedule_payload(),
                metadata={},
            )

        self.service.repository.name_exists.return_value = False
        incomplete = fee_schedule_payload()
        incomplete["fee_rules"] = [{"key": "commission", "category": "commission"}]
        with self.assertRaises(AccountProfileValidationError):
            self.service.create(
                name="研究账户",
                status=AccountProfileStatus.ACTIVE,
                fee_schedule=incomplete,
                metadata={},
            )

    def test_update_and_delete_use_row_lock(self) -> None:
        record = BacktestAccountProfileRecord(
            id=uuid4(),
            name="旧名称",
            status="active",
            fee_schedule_key="etf-cny",
            fee_rules=fee_schedule_payload()["fee_rules"],
            fee_schedule_metadata={},
            profile_metadata={},
        )
        self.service.repository.get.return_value = record
        self.service.repository.name_exists.return_value = False

        updated = self.service.update(record.id, name="新名称")
        self.assertIs(updated, record)
        self.assertEqual(record.name, "新名称")
        self.service.repository.get.assert_called_with(record.id, for_update=True)

        self.service.delete(record.id)
        self.session.delete.assert_called_once_with(record)


if __name__ == "__main__":
    unittest.main()
