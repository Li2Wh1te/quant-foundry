"""Focused acceptance checks for task-13 identity/display/mapping PIT APIs."""

from datetime import UTC, date, datetime
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import (
    InstrumentCodeMapping,
    InstrumentDisplayFact,
    InstrumentIdentityFact,
    MappingCoverageGapError,
)
from app.instruments.identity_repository import (
    InstrumentDisplayFactRepository,
    InstrumentIdentityFactRepository,
    InstrumentIdentityRepository,
)
from app.instruments.models import (
    Instrument,
    InstrumentCodeMappingRecord,
    InstrumentDisplayFactRecord,
    InstrumentIdentityFactRecord,
)


class IdentityPITQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Instrument.__table__.create(self.engine)
        InstrumentCodeMappingRecord.__table__.create(self.engine)
        InstrumentIdentityFactRecord.__table__.create(self.engine)
        InstrumentDisplayFactRecord.__table__.create(self.engine)
        self.instrument_id = uuid4()
        self.known_at = datetime(2026, 1, 2, tzinfo=UTC)
        with Session(self.engine) as session:
            session.add(Instrument(id=self.instrument_id, asset_class="etf"))
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_named_pit_methods_keep_effective_and_cutoff_separate(self) -> None:
        with Session(self.engine) as session:
            identity = InstrumentIdentityFact(
                instrument_id=self.instrument_id,
                fact_version=1,
                asset_class="etf",
                exchange="SSE",
                currency="CNY",
                calendar_id="XSHG",
                valid_from=date(2026, 1, 1),
                known_at=self.known_at,
                observed_at=self.known_at,
                evidence="registry://identity/1",
            )
            InstrumentIdentityFactRepository(session).append_fact(identity)
            display = InstrumentDisplayFact(
                instrument_id=self.instrument_id,
                fact_version=1,
                valid_from=date(2026, 1, 1),
                known_at=self.known_at,
                observed_at=self.known_at,
                source="registry",
                evidence="registry://display/1",
                trading_code="510300",
                display_name="沪深300ETF",
            )
            InstrumentDisplayFactRepository(session).append_fact(display)
            session.flush()

            identity_at = InstrumentIdentityRepository(session).resolve_identity_at(
                self.instrument_id,
                effective_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 3, tzinfo=UTC),
            )
            display_at = InstrumentDisplayFactRepository(session).resolve_display_at(
                self.instrument_id,
                effective_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
                data_cutoff=datetime(2026, 1, 3, tzinfo=UTC),
            )
            self.assertEqual(identity_at.exchange, "SSE")
            self.assertEqual(display_at.display_name, "沪深300ETF")

            # The same facts are invisible before their knowledge cutoff.
            self.assertIsNone(
                InstrumentIdentityFactRepository(session).resolve_identity_at(
                    self.instrument_id,
                    effective_at=datetime(2026, 1, 3, tzinfo=UTC),
                    data_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )

    def test_mapping_gap_error_contains_json_safe_requested_window(self) -> None:
        with Session(self.engine) as session:
            repository = InstrumentIdentityRepository(session)
            with self.assertRaises(MappingCoverageGapError) as context:
                repository.resolve_code_mappings(
                    self.instrument_id,
                    source="tushare",
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 1, 3),
                    data_cutoff=datetime(2026, 1, 3, tzinfo=UTC),
                )
            self.assertEqual(context.exception.details["start_date"], "2026-01-01")
            self.assertEqual(context.exception.details["end_date"], "2026-01-03")
            self.assertEqual(context.exception.code, "identity_mapping_incomplete")

    def test_mapping_query_rejects_non_datetime_cutoff(self) -> None:
        with Session(self.engine) as session:
            with self.assertRaises(DomainValidationError):
                InstrumentIdentityRepository(session).resolve_code_mappings(
                    self.instrument_id,
                    source="tushare",
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 1, 1),
                    data_cutoff="2026-01-03T00:00:00Z",  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
