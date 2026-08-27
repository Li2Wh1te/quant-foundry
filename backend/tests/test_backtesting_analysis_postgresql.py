"""PostgreSQL-only analyzer persistence and migration acceptance tests.

These tests are skipped for ordinary SQLite/local runs. CI enables them with
``POSTGRES_TEST_ENABLED=1`` against its disposable PostgreSQL service so row locks,
unique-insert races, and Alembic downgrade guards are exercised by the actual
production dialect.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.backtesting.result_models import BacktestAnalysisSummaryRecord
from app.backtesting.result_records import BacktestAnalysisSummaryRecord as SummaryOrm
from app.backtesting.result_repository import (
    BacktestResultRepository,
    ResultRecordConflictError,
)


ENABLED = os.getenv("POSTGRES_TEST_ENABLED") == "1"
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _native_psycopg_dsn(database: str = "postgres") -> str:
    """Render the configured SQLAlchemy URL with psycopg's native scheme."""

    return get_settings().database_url.set(
        drivername="postgresql", database=database
    ).render_as_string(hide_password=False)


def _summary(
    run_id,
    *,
    sequence: int,
    completed_session: date | None = None,
    token_digit: str | None = None,
    valid_day_count: int | None = None,
) -> BacktestAnalysisSummaryRecord:
    now = datetime.now(timezone.utc)
    return BacktestAnalysisSummaryRecord(
        run_id=run_id,
        status="partial",
        analyzer_snapshot={"specs": []},
        formula_signature="sha256:" + "1" * 64,
        input_evidence_signature="sha256:" + "2" * 64,
        reporting_currency="CNY",
        initial_equity=Decimal("10000"),
        valid_day_count=(sequence if valid_day_count is None else valid_day_count),
        fill_count=0,
        gross_traded_notional=Decimal("0"),
        cumulative_fees=Decimal("0"),
        last_chunk_sequence=sequence,
        last_chunk_token="sha256:" + (
            token_digit * 64 if token_digit is not None else format(sequence, "064x")
        ),
        completed_through_session=(
            completed_session or date(2026, 7, 6 + sequence)
        ),
        created_at=now,
        updated_at=now,
    )


class PostgreSqlDsnTestCase(unittest.TestCase):
    def test_admin_dsn_uses_a_psycopg_native_parseable_scheme(self) -> None:
        dsn = _native_psycopg_dsn()
        parsed = conninfo_to_dict(dsn)
        self.assertTrue(dsn.startswith("postgresql://"))
        self.assertEqual(parsed["dbname"], "postgres")


@unittest.skipUnless(ENABLED, "requires the disposable PostgreSQL CI service")
class PostgreSqlAnalysisPersistenceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(get_settings().database_url)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(SummaryOrm.__table__.delete())

    def test_competing_insert_leaves_exactly_one_summary(self) -> None:
        run_id = uuid4()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        class BarrierSession(Session):
            """Force both repository reads to observe the absent row first."""

            def scalars(self, *args, **kwargs):
                result = super().scalars(*args, **kwargs)

                class SynchronizedResult:
                    def first(self):
                        value = result.first()
                        barrier.wait(timeout=5)
                        return value

                return SynchronizedResult()

        def write() -> None:
            with BarrierSession(self.engine) as session:
                try:
                    BacktestResultRepository(
                        session, cursor_signing_key="postgres-test-signing-key"
                    ).upsert_analysis_summary(_summary(run_id, sequence=0))
                    session.commit()
                    outcomes.append("written")
                except ResultRecordConflictError:
                    session.rollback()
                    outcomes.append("conflict")

        threads = [threading.Thread(target=write) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "written"])
        with Session(self.engine) as session:
            count = session.scalar(
                select(func.count()).select_from(SummaryOrm).where(
                    SummaryOrm.run_id == run_id
                )
            )
        self.assertEqual(count, 1)

    def test_summary_update_waits_for_postgresql_row_lock(self) -> None:
        run_id = uuid4()
        with Session(self.engine) as session:
            BacktestResultRepository(
                session, cursor_signing_key="postgres-test-signing-key"
            ).upsert_analysis_summary(_summary(run_id, sequence=0))
            session.commit()

        finished = threading.Event()
        with Session(self.engine) as locker:
            locker.scalars(
                select(SummaryOrm).where(SummaryOrm.run_id == run_id).with_for_update()
            ).one()

            def advance() -> None:
                with Session(self.engine) as session:
                    BacktestResultRepository(
                        session, cursor_signing_key="postgres-test-signing-key"
                    ).upsert_analysis_summary(_summary(run_id, sequence=1))
                    session.commit()
                finished.set()

            thread = threading.Thread(target=advance)
            thread.start()
            time.sleep(0.25)
            self.assertFalse(finished.is_set())
            locker.commit()
            thread.join(timeout=10)
        self.assertTrue(finished.is_set())

    def test_postgresql_checkpoint_monotonicity_and_idempotency(self) -> None:
        run_id = uuid4()
        with Session(self.engine) as session:
            repository = BacktestResultRepository(
                session, cursor_signing_key="postgres-test-signing-key"
            )
            first = _summary(run_id, sequence=0)
            repository.upsert_analysis_summary(first)
            repository.upsert_analysis_summary(first)
            repository.upsert_analysis_summary(_summary(run_id, sequence=1))

            for conflicting in (
                _summary(run_id, sequence=0),
                _summary(
                    run_id,
                    sequence=2,
                    completed_session=date(2026, 7, 6),
                ),
                _summary(run_id, sequence=1, token_digit="f"),
                _summary(run_id, sequence=1, valid_day_count=99),
            ):
                with self.subTest(conflicting=conflicting), self.assertRaises(
                    ResultRecordConflictError
                ):
                    repository.upsert_analysis_summary(conflicting)
            session.rollback()


@unittest.skipUnless(ENABLED, "requires the disposable PostgreSQL CI service")
class PostgreSqlAnalysisMigrationTestCase(unittest.TestCase):
    def test_clean_database_round_trips_upgrade_and_downgrade(self) -> None:
        settings = get_settings()
        temporary_database = f"qf_migration_{uuid4().hex}"
        admin_dsn = _native_psycopg_dsn()
        environment = dict(os.environ)
        environment["QF_DATABASE_NAME"] = temporary_database
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(temporary_database)
                )
            )
        try:
            for command in (
                ["uv", "run", "alembic", "upgrade", "head"],
                ["uv", "run", "alembic", "downgrade", "base"],
            ):
                subprocess.run(
                    command,
                    cwd=BACKEND_ROOT,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s",
                    (temporary_database,),
                )
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(temporary_database)
                    )
                )

    def _assert_guarded_downgrade(
        self,
        *,
        inserted_values: dict,
        target_revision: str,
        expected_error: str,
    ) -> None:
        settings = get_settings()
        temporary_database = f"qf_migration_{uuid4().hex}"
        admin_dsn = _native_psycopg_dsn()
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(temporary_database)
                )
            )
        environment = dict(os.environ)
        environment["QF_DATABASE_NAME"] = temporary_database
        try:
            subprocess.run(
                ["uv", "run", "alembic", "upgrade", "head"],
                cwd=BACKEND_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            temporary_url = settings.database_url.set(database=temporary_database)
            temporary_engine = create_engine(temporary_url)
            try:
                with temporary_engine.begin() as connection:
                    base_values = dict(
                        run_id=uuid4(),
                        status="partial",
                        analyzer_snapshot={"specs": []},
                        formula_signature="sha256:" + "1" * 64,
                        input_evidence_signature="sha256:" + "2" * 64,
                        reporting_currency="CNY",
                    )
                    base_values.update(inserted_values)
                    connection.execute(
                        SummaryOrm.__table__.insert().values(**base_values)
                    )
            finally:
                temporary_engine.dispose()
            result = subprocess.run(
                ["uv", "run", "alembic", "downgrade", target_revision],
                cwd=BACKEND_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s",
                    (temporary_database,),
                )
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(temporary_database)
                    )
                )

    def test_downgrade_refuses_to_drop_populated_timeline_evidence(self) -> None:
        self._assert_guarded_downgrade(
            inserted_values={
                "formal_timeline": {
                    "sessions": ["2026-07-06"],
                    "timeline_hash": "sha256:" + "3" * 64,
                }
            },
            target_revision="20260825_03",
            expected_error="formal timeline evidence exists",
        )

    def test_downgrade_refuses_to_drop_populated_retry_evidence(self) -> None:
        self._assert_guarded_downgrade(
            inserted_values={"last_chunk_token": "checkpoint-0"},
            target_revision="20260825_01",
            expected_error="analysis retry fingerprint evidence exists",
        )
