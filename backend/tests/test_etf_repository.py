import unittest
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.data_ingestion.models.etf import EtfCode, EtfEntity
from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.schemas.etf import EtfInstrumentInput
from app.instruments.models import Instrument


def make_instrument(ts_code: str = "159526.SZ") -> EtfInstrumentInput:
    return EtfInstrumentInput(
        ts_code=ts_code,
        csname="简称",
        extname="扩展简称",
        cname="全称",
        index_code="000300.SH",
        index_name="沪深300",
        setup_date=None,
        list_date=None,
        list_status="L",
        exchange="SZ",
        mgr_name="管理人",
        custod_name="托管人",
        mgt_fee=Decimal("0.15"),
        etf_type="境内",
    )


class EtfCodeRepositoryTestCase(unittest.TestCase):
    def test_upsert_updates_source_fields_but_preserves_entity_identity(self) -> None:
        session = Mock()
        entity_id = uuid4()
        session.execute.side_effect = [
            Mock(all=Mock(return_value=[("159526.SZ", entity_id)])),
            Mock(all=Mock(return_value=[("159526.SZ",)])),
            Mock(),
        ]
        observed_at = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)

        result = EtfCodeRepository(session).upsert_codes(
            [make_instrument()], source="tushare", observed_at=observed_at
        )

        self.assertEqual((result.received, result.changed, result.unchanged), (1, 1, 0))
        upsert_statement = session.execute.call_args_list[1].args[0]
        sql = str(upsert_statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (source, ts_code) DO UPDATE", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertNotIn("etf_id = excluded.etf_id", sql)
        last_seen_statement = session.execute.call_args_list[2].args[0]
        self.assertIn("last_seen_at", str(last_seen_statement))

    def test_creates_an_entity_for_a_new_code(self) -> None:
        session = Mock()
        session.execute.side_effect = [
            Mock(all=Mock(return_value=[])),
            Mock(),
            Mock(),
            Mock(all=Mock(return_value=[("159526.SZ",)])),
            Mock(),
        ]

        EtfCodeRepository(session).upsert_codes(
            [make_instrument()],
            source="tushare",
            observed_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        )

        # The generic instrument identity must be provisioned before the
        # ETF-specific entity because etf_entities.id references it.
        insert_instrument_statement = session.execute.call_args_list[1].args[0]
        self.assertIn("INSERT INTO instruments", str(insert_instrument_statement))
        self.assertIn("asset_class", str(insert_instrument_statement))
        insert_entity_statement = session.execute.call_args_list[2].args[0]
        self.assertIn("INSERT INTO etf_entities", str(insert_entity_statement))

    def test_rejects_duplicate_codes_before_executing_sql(self) -> None:
        session = Mock()

        with self.assertRaisesRegex(ValueError, "duplicate ts_code"):
            EtfCodeRepository(session).upsert_codes(
                [make_instrument(), make_instrument()],
                source="tushare",
                observed_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
            )

        session.execute.assert_not_called()

    def test_reassigns_a_code_only_with_an_explicit_target_entity_and_audit(self) -> None:
        session = Mock()
        old_entity_id, target_entity_id = uuid4(), uuid4()
        code = SimpleNamespace(etf_id=old_entity_id)
        session.get.side_effect = [code, object()]

        changed = EtfCodeRepository(session).reassign_code_entity(
            source="tushare",
            ts_code="159526.SZ",
            target_etf_id=target_entity_id,
            mapping_source="exchange_announcement",
            evidence="https://example.invalid/notice",
            actor="operator@example.com",
        )

        self.assertTrue(changed)
        self.assertEqual(code.etf_id, target_entity_id)
        audit = session.add.call_args.args[0]
        self.assertEqual(audit.old_etf_id, old_entity_id)
        self.assertEqual(audit.new_etf_id, target_entity_id)
        self.assertEqual(audit.mapping_source, "exchange_announcement")


class EtfIdentityMetadataTestCase(unittest.TestCase):
    def test_etf_entity_primary_key_references_instruments(self) -> None:
        # ORM metadata must mirror the deployed schema (migration
        # 20260822_03): etf_entities.id is also a foreign key into the
        # generic instruments table, so create_all() and future Alembic
        # autogenerate runs reproduce the constraint instead of dropping it.
        foreign_keys = {
            fk.target_fullname for fk in EtfEntity.__table__.foreign_keys
        }
        self.assertIn("instruments.id", foreign_keys)
        self.assertIn("instruments", EtfEntity.__table__.metadata.tables)
        self.assertIs(EtfEntity.__table__.c.id.primary_key, True)
