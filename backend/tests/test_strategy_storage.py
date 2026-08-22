"""Tests for private database-backed strategy drafts and revisions."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.strategies.repository import StrategyRepository
from app.strategies.service import (
    DEFAULT_RUNTIME_MANIFEST,
    StrategyArchivedError,
    StrategyDraftConflictError,
    StrategyDraftIntegrityError,
    StrategyDraftValidationError,
    StrategyStorageService,
    StrategyStorageValidationError,
    source_hash,
)


SOURCE_V1 = "def run(context, parameters):\n    return {'mode': 'hold'}\n"
SOURCE_V2 = (
    "def run(context, parameters):\n"
    "    return {'mode': 'target_weights', 'targets': {}}\n"
)


class StrategyStorageModelTestCase(unittest.TestCase):
    """Verify the persistence contract without requiring a PostgreSQL server."""

    def test_models_store_source_as_text_and_current_revision_is_owned(self) -> None:
        strategy_sql = str(
            CreateTable(Strategy.__table__).compile(dialect=postgresql.dialect())
        )
        draft_sql = str(
            CreateTable(StrategyDraft.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        revision_sql = str(
            CreateTable(StrategyRevision.__table__).compile(
                dialect=postgresql.dialect()
            )
        )

        self.assertIn("current_revision_id UUID", strategy_sql)
        self.assertIn(
            "FOREIGN KEY(id, current_revision_id) REFERENCES strategy_revisions "
            "(strategy_id, id)",
            strategy_sql,
        )
        self.assertIn("source_code TEXT NOT NULL", draft_sql)
        self.assertIn("source_code TEXT NOT NULL", revision_sql)
        self.assertIn("parameter_schema JSONB", draft_sql)
        self.assertIn("runtime_manifest JSONB", revision_sql)
        self.assertIn(
            "UNIQUE (strategy_id, revision_number)", revision_sql
        )

    def test_revision_migration_has_a_database_immutability_trigger(self) -> None:
        migration_path = (
            Path(__file__).parents[1]
            / "app/db/migrations/versions/20260819_01_add_strategy_storage.py"
        )
        migration = migration_path.read_text(encoding="utf-8")

        self.assertIn("CREATE TRIGGER strategy_revisions_immutable", migration)
        self.assertIn("BEFORE UPDATE OR DELETE ON strategy_revisions", migration)
        self.assertIn("RAISE EXCEPTION 'strategy revisions are immutable'", migration)


class StrategyRepositoryTestCase(unittest.TestCase):
    """Cover the revision sequence query that publication depends on."""

    def test_next_revision_number_starts_at_one_and_increments_latest(self) -> None:
        session = Mock()
        repository = StrategyRepository(session)
        strategy_id = uuid4()

        session.scalar.return_value = None
        self.assertEqual(repository.next_revision_number(strategy_id), 1)

        session.scalar.return_value = 8
        self.assertEqual(repository.next_revision_number(strategy_id), 9)

        statement = session.scalar.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("max(strategy_revisions.revision_number)", sql)
        self.assertIn("strategy_revisions.strategy_id", sql)


class StrategyStorageServiceTestCase(unittest.TestCase):
    """Exercise draft locking, publish snapshots, and private source invariants."""

    def setUp(self) -> None:
        self.session = Mock()
        self.service = StrategyStorageService(self.session)
        self.service.repository = Mock()

    def test_create_strategy_creates_only_a_mutable_database_draft(self) -> None:
        schema = {"type": "object", "properties": {"window": {"type": "integer"}}}
        defaults = {"window": 20}

        strategy = self.service.create_strategy(
            name="  ETF 趋势  ",
            description="  私有测试策略  ",
            source_code=SOURCE_V1,
            parameter_schema=schema,
            default_parameters=defaults,
        )

        self.assertEqual(strategy.name, "ETF 趋势")
        self.assertEqual(strategy.description, "私有测试策略")
        self.assertEqual(strategy.state, "active")
        self.assertIsNone(strategy.current_revision_id)
        self.session.add_all.assert_called_once()
        created_strategy, draft = self.session.add_all.call_args.args[0]
        self.assertIs(created_strategy, strategy)
        self.assertEqual(draft.strategy_id, strategy.id)
        self.assertEqual(draft.source_code, SOURCE_V1)
        self.assertEqual(draft.source_hash, source_hash(SOURCE_V1))
        self.assertEqual(draft.parameter_schema, schema)
        self.assertEqual(draft.default_parameters, defaults)
        self.session.flush.assert_called_once_with()

        # Input dictionaries may belong to a web request. Subsequent mutation must
        # never alter the persisted ORM draft's JSON snapshot by object aliasing.
        schema["properties"]["window"]["minimum"] = 2
        defaults["window"] = 10
        self.assertNotIn("minimum", draft.parameter_schema["properties"]["window"])
        self.assertEqual(draft.default_parameters["window"], 20)

    def test_save_draft_uses_optimistic_version_and_rehashes_exact_source(self) -> None:
        strategy, draft = self._strategy_with_draft(version=2)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        saved = self.service.save_draft(
            strategy.id,
            expected_version=2,
            source_code=SOURCE_V2,
            parameter_schema={"type": "object"},
            default_parameters={"enabled": True},
        )

        self.assertIs(saved, draft)
        self.assertEqual(draft.source_code, SOURCE_V2)
        self.assertEqual(draft.source_hash, source_hash(SOURCE_V2))
        self.assertEqual(draft.parameter_schema, {"type": "object"})
        self.assertEqual(draft.default_parameters, {"enabled": True})
        self.assertEqual(draft.version, 3)
        self.service.repository.get_strategy.assert_called_once_with(
            strategy.id, for_update=True
        )
        self.service.repository.get_draft.assert_called_once_with(
            strategy.id, for_update=True
        )
        self.session.flush.assert_called_once_with()

    def test_save_draft_rejects_stale_editor_without_overwriting_source(self) -> None:
        strategy, draft = self._strategy_with_draft(version=3)
        before = (draft.source_code, draft.source_hash, draft.version)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        with self.assertRaises(StrategyDraftConflictError):
            self.service.save_draft(
                strategy.id,
                expected_version=2,
                source_code=SOURCE_V2,
            )

        self.assertEqual((draft.source_code, draft.source_hash, draft.version), before)
        self.session.flush.assert_not_called()

    def test_save_draft_preserves_omitted_parameter_fields(self) -> None:
        strategy, draft = self._strategy_with_draft(version=2)
        original_schema = deepcopy(draft.parameter_schema)
        original_defaults = deepcopy(draft.default_parameters)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        self.service.save_draft(
            strategy.id,
            expected_version=2,
            source_code=SOURCE_V2,
        )

        self.assertEqual(draft.parameter_schema, original_schema)
        self.assertEqual(draft.default_parameters, original_defaults)

    def test_publish_copies_every_execution_relevant_draft_value(self) -> None:
        strategy, draft = self._strategy_with_draft(version=5)
        original_schema = deepcopy(draft.parameter_schema)
        original_defaults = deepcopy(draft.default_parameters)
        manifest = {"strategy_contract_version": 1, "worker_image": "private:1"}
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft
        self.service.repository.next_revision_number.return_value = 3

        revision = self.service.publish_revision(
            strategy.id,
            expected_draft_version=5,
            runtime_manifest=manifest,
        )

        self.assertEqual(revision.strategy_id, strategy.id)
        self.assertEqual(revision.revision_number, 3)
        self.assertEqual(revision.source_code, draft.source_code)
        self.assertEqual(revision.source_hash, draft.source_hash)
        self.assertEqual(revision.parameter_schema, original_schema)
        self.assertEqual(revision.default_parameters, original_defaults)
        self.assertEqual(revision.runtime_manifest, manifest)
        self.assertEqual(strategy.current_revision_id, revision.id)
        self.assertEqual(strategy.version, 2)
        self.session.add.assert_called_once_with(revision)
        self.assertEqual(self.session.flush.call_count, 2)

        # Published records are a deep snapshot even before the database trigger
        # makes the stored revision physically immutable after commit.
        draft.parameter_schema["properties"]["window"]["minimum"] = 99
        draft.default_parameters["window"] = 5
        manifest["worker_image"] = "private:mutated"
        self.assertEqual(revision.parameter_schema, original_schema)
        self.assertEqual(revision.default_parameters, original_defaults)
        self.assertEqual(revision.runtime_manifest["worker_image"], "private:1")

    def test_publish_uses_a_contract_manifest_when_one_is_not_supplied(self) -> None:
        strategy, draft = self._strategy_with_draft(version=1)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft
        self.service.repository.next_revision_number.return_value = 1

        revision = self.service.publish_revision(
            strategy.id, expected_draft_version=1
        )

        self.assertEqual(revision.runtime_manifest, DEFAULT_RUNTIME_MANIFEST)
        self.assertIsNot(revision.runtime_manifest, DEFAULT_RUNTIME_MANIFEST)

    def test_publish_rejects_manifests_with_wrong_protocol_version(self) -> None:
        strategy, draft = self._strategy_with_draft(version=1)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        for bad_manifest in (
            {"strategy_contract_version": 2},
            {"strategy_contract_version": "1"},
            {},
        ):
            with self.assertRaises(
                StrategyStorageValidationError, msg=str(bad_manifest)
            ):
                self.service.publish_revision(
                    strategy.id,
                    expected_draft_version=1,
                    runtime_manifest=bad_manifest,
                )
        # No revision is created when the protocol metadata check fails.
        self.session.add.assert_not_called()
        self.assertIsNone(strategy.current_revision_id)

    def test_publish_rejects_a_draft_whose_stored_hash_was_tampered_with(self) -> None:
        strategy, draft = self._strategy_with_draft(version=1)
        draft.source_hash = "0" * 64
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        with self.assertRaises(StrategyDraftIntegrityError):
            self.service.publish_revision(strategy.id, expected_draft_version=1)

        self.session.add.assert_not_called()
        self.service.repository.next_revision_number.assert_not_called()
        self.assertIsNone(strategy.current_revision_id)

    def test_publish_rejects_source_that_fails_the_static_contract(self) -> None:
        strategy, draft = self._strategy_with_draft(version=1)
        draft.source_code = "def not_run():\n    return {}\n"
        draft.source_hash = source_hash(draft.source_code)
        self.service.repository.get_strategy.return_value = strategy
        self.service.repository.get_draft.return_value = draft

        with self.assertRaises(StrategyDraftValidationError):
            self.service.publish_revision(strategy.id, expected_draft_version=1)

        self.session.add.assert_not_called()
        self.service.repository.next_revision_number.assert_not_called()

    def test_storage_rejects_archived_strategies_and_invalid_private_source(self) -> None:
        strategy, draft = self._strategy_with_draft(version=1, state="archived")
        self.service.repository.get_strategy.return_value = strategy

        with self.assertRaises(StrategyArchivedError):
            self.service.save_draft(
                strategy.id, expected_version=1, source_code=SOURCE_V2
            )

        with self.assertRaises(StrategyStorageValidationError):
            self.service.create_strategy(name="Private", source_code=" \n\t ")

    @staticmethod
    def _strategy_with_draft(
        *, version: int, state: str = "active"
    ) -> tuple[Strategy, StrategyDraft]:
        strategy_id = uuid4()
        strategy = Strategy(
            id=strategy_id,
            name="Private ETF strategy",
            state=state,
            version=1,
        )
        draft = StrategyDraft(
            strategy_id=strategy_id,
            source_code=SOURCE_V1,
            source_hash=source_hash(SOURCE_V1),
            parameter_schema={
                "type": "object",
                "properties": {"window": {"type": "integer"}},
            },
            default_parameters={"window": 20},
            version=version,
        )
        return strategy, draft


if __name__ == "__main__":
    unittest.main()
