"""Tests for PIT instrument code-mapping persistence and query semantics."""

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import (
    InstrumentCodeMapping,
    MappingConflictError,
    MappingCoverageGapError,
)
from app.instruments.repository import InstrumentCodeMappingRepository


CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)


def make_row(
    *,
    source_code: str,
    valid_from: date,
    valid_to: date | None = None,
    known_at: datetime = datetime(2024, 6, 1, tzinfo=UTC),
    instrument_id=None,
) -> SimpleNamespace:
    """A stand-in for an ORM mapping row; projection re-validates every field."""

    return SimpleNamespace(
        id=uuid4(),
        instrument_id=instrument_id or uuid4(),
        source="tushare",
        source_code=source_code,
        trading_code=source_code.split(".")[0],
        valid_from=valid_from,
        valid_to=valid_to,
        source_revision=None,
        mapping_source="exchange_announcement",
        evidence="https://example.invalid/notice",
        known_at=known_at,
        observed_at=known_at,
    )


def make_session(rows) -> object:
    """A fake session that records executed statements for SQL assertions."""

    result = SimpleNamespace(all=lambda: rows)
    statements: list = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalars=lambda: result)

    return SimpleNamespace(execute=execute, statements=statements)


class ResolveCodeMappingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.instrument_id = uuid4()

    def test_query_filters_by_window_and_data_cutoff(self) -> None:
        # One fully covering segment keeps validation happy so the test can
        # assert on the generated SQL filters themselves.
        session = make_session(
            [
                make_row(
                    instrument_id=self.instrument_id,
                    source_code="OLD.SH",
                    valid_from=date(2024, 1, 1),
                    valid_to=None,
                )
            ]
        )
        repository = InstrumentCodeMappingRepository(session)

        repository.resolve_code_mappings(
            self.instrument_id,
            source="tushare",
            start_date=date(2024, 1, 1),
            end_date=date(2025, 12, 31),
            data_cutoff=CUTOFF,
        )

        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        self.assertIn("instrument_id =", sql)
        self.assertIn("source =", sql)
        self.assertIn("valid_from <=", sql)
        self.assertIn("valid_to >", sql)
        self.assertIn("known_at <=", sql)
        self.assertIn("ORDER BY", sql)

    def test_returns_segments_ordered_by_valid_from(self) -> None:
        old_row = make_row(
            instrument_id=self.instrument_id,
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 1, 2),
        )
        new_row = make_row(
            instrument_id=self.instrument_id,
            source_code="NEW.SH",
            valid_from=date(2025, 1, 2),
            valid_to=None,
        )
        repository = InstrumentCodeMappingRepository(make_session([new_row, old_row]))

        segments = repository.resolve_code_mappings(
            self.instrument_id,
            source="tushare",
            start_date=date(2024, 1, 1),
            end_date=date(2025, 12, 31),
            data_cutoff=CUTOFF,
        )

        self.assertEqual([s.source_code for s in segments], ["OLD.SH", "NEW.SH"])

    def test_gap_between_rows_raises_an_explicit_error(self) -> None:
        rows = [
            make_row(
                instrument_id=self.instrument_id,
                source_code="OLD.SH",
                valid_from=date(2024, 1, 1),
                valid_to=date(2025, 1, 2),
            ),
            make_row(
                instrument_id=self.instrument_id,
                source_code="NEW.SH",
                valid_from=date(2025, 1, 5),
                valid_to=None,
            ),
        ]
        repository = InstrumentCodeMappingRepository(make_session(rows))

        with self.assertRaises(MappingCoverageGapError):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=CUTOFF,
            )

    def test_overlapping_rows_raise_an_explicit_error(self) -> None:
        rows = [
            make_row(
                instrument_id=self.instrument_id,
                source_code="OLD.SH",
                valid_from=date(2024, 1, 1),
                valid_to=date(2025, 1, 2),
            ),
            make_row(
                instrument_id=self.instrument_id,
                source_code="NEW.SH",
                valid_from=date(2024, 12, 1),
                valid_to=None,
            ),
        ]
        repository = InstrumentCodeMappingRepository(make_session(rows))

        with self.assertRaises(MappingConflictError):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=CUTOFF,
            )

    def test_empty_result_is_a_coverage_gap(self) -> None:
        repository = InstrumentCodeMappingRepository(make_session([]))

        with self.assertRaises(MappingCoverageGapError):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=CUTOFF,
            )

    def test_leading_window_gap_is_an_explicit_error(self) -> None:
        # The only known mapping starts after the requested window begins.
        rows = [
            make_row(
                instrument_id=self.instrument_id,
                source_code="NEW.SH",
                valid_from=date(2024, 1, 2),
                valid_to=None,
            ),
        ]
        repository = InstrumentCodeMappingRepository(make_session(rows))

        with self.assertRaisesRegex(MappingCoverageGapError, "start at"):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=CUTOFF,
            )

    def test_trailing_window_gap_is_an_explicit_error(self) -> None:
        # The only known mapping ends before the requested window ends.
        rows = [
            make_row(
                instrument_id=self.instrument_id,
                source_code="OLD.SH",
                valid_from=date(2024, 1, 1),
                valid_to=date(2025, 12, 30),
            ),
        ]
        repository = InstrumentCodeMappingRepository(make_session(rows))

        with self.assertRaisesRegex(MappingCoverageGapError, "end at"):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=CUTOFF,
            )

    def test_rejects_naive_cutoff_and_inverted_window(self) -> None:
        repository = InstrumentCodeMappingRepository(make_session([]))
        with self.assertRaises(DomainValidationError):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
                data_cutoff=datetime(2026, 8, 22),  # naive
            )
        with self.assertRaises(DomainValidationError):
            repository.resolve_code_mappings(
                self.instrument_id,
                source="tushare",
                start_date=date(2025, 12, 31),
                end_date=date(2024, 1, 1),
                data_cutoff=CUTOFF,
            )


class AddMappingTestCase(unittest.TestCase):
    def test_appends_immutable_row_with_generated_identity(self) -> None:
        session = SimpleNamespace(add=lambda row: None)
        added: list[InstrumentCodeMapping] = []
        session.add = added.append  # type: ignore[method-assign]
        repository = InstrumentCodeMappingRepository(session)
        mapping = InstrumentCodeMapping(
            instrument_id=uuid4(),
            source="tushare",
            source_code="510300.SH",
            trading_code="510300",
            valid_from=date(2024, 1, 1),
            valid_to=None,
            mapping_source="exchange_announcement",
            evidence="https://example.invalid/notice",
            known_at=datetime(2024, 6, 1, tzinfo=UTC),
            observed_at=datetime(2024, 6, 1, tzinfo=UTC),
        )

        mapping_id = repository.add_mapping(mapping)

        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertEqual(row.id, mapping_id)
        self.assertEqual(row.source_code, "510300.SH")
        self.assertEqual(row.trading_code, "510300")
        self.assertIsNone(row.valid_to)


if __name__ == "__main__":
    unittest.main()
