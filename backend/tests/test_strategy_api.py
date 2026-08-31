"""Tests for the private strategy administration contract."""

import unittest
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.strategies.models import Strategy, StrategyDraft
from app.strategies.router import _http_error
from app.strategies.schemas import (
    StrategyCreateRequest,
    StrategyMetadataUpdateRequest,
    StrategyPublishRequest,
    StrategyDraftSaveRequest,
)
from app.strategies.service import (
    StrategyArchivedError,
    StrategyDraftIntegrityError,
    StrategyDraftValidationError,
    StrategyMetadataConflictError,
    StrategyStorageService,
)
from app.strategies.validation import StrategyValidationIssue, validate_strategy_draft


API_TOKEN = "a" * 64
SOURCE = "def run(context, parameters):\n    return {'mode': 'hold'}\n"


class StrategyValidationTestCase(unittest.TestCase):
    """Ensure validation parses source but never imports or executes it."""

    def test_accepts_the_initial_strategy_contract(self) -> None:
        result = validate_strategy_draft(
            SOURCE,
            parameter_schema={
                "type": "object",
                "properties": {"window": {"type": "integer"}},
                "required": ["window"],
            },
            default_parameters={"window": 20},
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.issues, ())

    def test_reports_syntax_and_entrypoint_issues_without_running_source(self) -> None:
        source = "raise RuntimeError('must never run')\n"
        result = validate_strategy_draft(
            source,
            parameter_schema={},
            default_parameters={},
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            [issue.code for issue in result.issues], ["entrypoint_missing"]
        )

        syntax_result = validate_strategy_draft(
            "def run(context, parameters)\n    pass\n",
            parameter_schema={},
            default_parameters={},
        )
        self.assertEqual(syntax_result.issues[0].code, "syntax_error")

    def test_rejects_async_or_ambiguous_entrypoints_and_bad_schema_shape(self) -> None:
        async_result = validate_strategy_draft(
            "async def run(context, parameters):\n    return {}\n",
            parameter_schema={"type": "array"},
            default_parameters={},
        )
        self.assertEqual(
            [issue.code for issue in async_result.issues],
            ["entrypoint_async", "parameter_schema_type"],
        )

        signature_result = validate_strategy_draft(
            "def run(context):\n    return {}\n",
            parameter_schema={"required": ["x", "x"]},
            default_parameters={},
        )
        self.assertEqual(
            [issue.code for issue in signature_result.issues],
            ["entrypoint_signature", "parameter_schema_required_duplicate"],
        )


class StrategyApiSchemaTestCase(unittest.TestCase):
    """Cover request semantics and authenticated route registration."""

    def test_create_and_publish_requests_are_strict_json_objects(self) -> None:
        payload = StrategyCreateRequest.model_validate(
            {"name": "私有策略", "source_code": SOURCE}
        )
        self.assertEqual(payload.parameter_schema, {})
        self.assertEqual(payload.default_parameters, {})

        with self.assertRaises(ValidationError):
            StrategyCreateRequest.model_validate(
                {"name": "私有策略", "source_code": SOURCE, "unexpected": True}
            )
        with self.assertRaises(ValidationError):
            StrategyPublishRequest.model_validate({"draft_version": 0})

    def test_metadata_update_allows_explicit_description_clear_but_not_noop(self) -> None:
        payload = StrategyMetadataUpdateRequest.model_validate(
            {"version": 3, "description": None}
        )
        self.assertIn("description", payload.model_fields_set)

        with self.assertRaises(ValidationError):
            StrategyMetadataUpdateRequest.model_validate({"version": 3})
        with self.assertRaises(ValidationError):
            StrategyMetadataUpdateRequest.model_validate(
                {"version": 3, "name": None}
            )

    def test_draft_patch_preserves_omitted_fields_and_rejects_noop(self) -> None:
        payload = StrategyDraftSaveRequest.model_validate(
            {"version": 2, "source_code": SOURCE}
        )
        self.assertEqual(payload.model_fields_set, {"version", "source_code"})

        with self.assertRaises(ValidationError):
            StrategyDraftSaveRequest.model_validate({"version": 2})

    def test_strategy_routes_are_authenticated_and_source_is_not_in_list_schema(self) -> None:
        app = create_app(
            Settings(
                api_token=API_TOKEN,
                database_password="test-secret",
                _env_file=None,
            )
        )
        paths = app.openapi()["paths"]
        strategy_paths = {
            path: paths[path]
            for path in paths
            if path.startswith("/api/admin/strategies")
        }
        self.assertEqual(
            set(strategy_paths),
            {
                "/api/admin/strategies",
                "/api/admin/strategies/{strategy_id}",
                "/api/admin/strategies/{strategy_id}/draft",
                "/api/admin/strategies/{strategy_id}/validate",
                "/api/admin/strategies/{strategy_id}/publish",
                "/api/admin/strategies/{strategy_id}/backtests",
                "/api/admin/strategies/{strategy_id}/revisions",
                "/api/admin/strategies/{strategy_id}/revisions/{revision_number}",
            },
        )
        list_schema = paths["/api/admin/strategies"]["get"]["responses"]["200"]
        list_model_ref = list_schema["content"]["application/json"]["schema"]["items"]["$ref"]
        self.assertTrue(list_model_ref.endswith("StrategySummaryResponse"))
        summary_properties = app.openapi()["components"]["schemas"][
            "StrategySummaryResponse"
        ]["properties"]
        self.assertNotIn("source_code", summary_properties)
        for path, operations in strategy_paths.items():
            for operation in operations.values():
                self.assertEqual(operation.get("security"), [{"API Token": []}])


class StrategyServiceApiErrorTestCase(unittest.TestCase):
    """Keep lifecycle errors mapped to stable status codes and safe messages."""

    def test_expected_errors_are_mapped_without_echoing_private_source(self) -> None:
        self.assertEqual(
            _http_error(StrategyMetadataConflictError("private details")).status_code,
            409,
        )
        validation_error = _http_error(
            StrategyDraftValidationError(
                (
                    # The message intentionally contains no source text; the
                    # router's mapping must preserve only structured diagnostics.
                    StrategyValidationIssue(
                        code="syntax_error", message="语法错误", line=1, column=2
                    ),
                )
            )
        )
        self.assertEqual(validation_error.status_code, 422)
        self.assertEqual(validation_error.detail["issues"][0]["code"], "syntax_error")

    def test_validation_detects_a_tampered_stored_draft_hash(self) -> None:
        session = Mock()
        service = StrategyStorageService(session)
        service.repository = Mock()
        strategy_id = uuid4()
        strategy = Strategy(id=strategy_id, name="Private", state="active", version=1)
        draft = StrategyDraft(
            strategy_id=strategy_id,
            source_code=SOURCE,
            source_hash="0" * 64,
            parameter_schema={},
            default_parameters={},
            version=1,
        )
        service.repository.get_strategy.return_value = strategy
        service.repository.get_draft.return_value = draft

        with self.assertRaises(StrategyDraftIntegrityError):
            service.validate_draft(strategy_id)

    def test_metadata_and_archive_use_separate_strategy_versions(self) -> None:
        session = Mock()
        service = StrategyStorageService(session)
        service.repository = Mock()
        strategy_id = uuid4()
        strategy = Strategy(id=strategy_id, name="Private", state="active", version=2)
        service.repository.get_strategy.return_value = strategy

        changed = service.update_strategy_metadata(
            strategy_id, expected_version=2, description="updated"
        )
        self.assertIs(changed, strategy)
        self.assertEqual(strategy.description, "updated")
        self.assertEqual(strategy.version, 3)

        archived = service.archive_strategy(strategy_id, expected_version=3)
        self.assertIs(archived, strategy)
        self.assertEqual(strategy.state, "archived")
        self.assertEqual(strategy.version, 4)

        with self.assertRaises(StrategyArchivedError):
            service.update_strategy_metadata(
                strategy_id, expected_version=4, name="cannot update"
            )


if __name__ == "__main__":
    unittest.main()
