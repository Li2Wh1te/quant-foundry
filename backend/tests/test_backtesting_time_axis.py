"""Tests for the generic time axis and the fixed_trading_sessions@1 chunks."""

import unittest
from dataclasses import fields
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from types import MappingProxyType
from zoneinfo import ZoneInfo

from app.backtesting.calendar_axis import SessionPoint, SessionWindow
from app.backtesting.domain import DomainValidationError
from app.backtesting.time_axis import (
    SESSIONS_PER_CHUNK_V1,
    FixedTradingSessionsV1,
    TimeAxis,
    TimeChunk,
    TimeStep,
    TradingDayAxis,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
STANDARD_WINDOWS = (SessionWindow(time(9, 30), time(11, 30)), SessionWindow(time(13, 0), time(15, 0)))
SINGLE_WINDOW = (SessionWindow(time(9, 30), time(15, 0)),)


def make_point(
    session_date: date,
    sessions: tuple[SessionWindow, ...] = STANDARD_WINDOWS,
    **overrides: object,
) -> SessionPoint:
    kwargs = dict(
        session_date=session_date,
        session_id=session_date.isoformat(),
        timezone="Asia/Shanghai",
        sessions=sessions,
    )
    kwargs.update(overrides)
    return SessionPoint(**kwargs)


def make_step(sequence: int = 0, **overrides: object) -> TimeStep:
    day = date(2026, 8, 17) + timedelta(days=sequence)
    kwargs = dict(
        sequence=sequence,
        start_time=datetime(day.year, day.month, day.day, 9, 30, tzinfo=SHANGHAI),
        end_time=datetime(day.year, day.month, day.day, 15, 0, tzinfo=SHANGHAI),
        session_id=day.isoformat(),
        timezone="Asia/Shanghai",
        metadata={"session_date": day.isoformat()},
    )
    kwargs.update(overrides)
    return TimeStep(**kwargs)


def make_steps(count: int) -> list[TimeStep]:
    return [make_step(sequence=index) for index in range(count)]


class TimeStepValidationTests(unittest.TestCase):
    def test_rejects_negative_sequence(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_step(sequence=-1)

    def test_rejects_boolean_sequence(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=True,
                start_time=make_step().start_time,
                end_time=make_step().end_time,
                session_id="2026-08-17",
                timezone="Asia/Shanghai",
                metadata={},
            )

    def test_rejects_naive_datetimes(self) -> None:
        step = make_step()
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=0,
                start_time=step.start_time.replace(tzinfo=None),
                end_time=step.end_time,
                session_id=step.session_id,
                timezone=step.timezone,
                metadata={},
            )

    def test_rejects_timezone_mismatch_between_datetime_and_field(self) -> None:
        step = make_step()
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=0,
                start_time=step.start_time.astimezone(UTC),
                end_time=step.end_time,
                session_id=step.session_id,
                timezone=step.timezone,
                metadata={},
            )

    def test_rejects_fixed_offset_timezone_for_iana_field(self) -> None:
        fixed = timezone(timedelta(hours=8))
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=0,
                start_time=datetime(2026, 8, 17, 9, 30, tzinfo=fixed),
                end_time=datetime(2026, 8, 17, 15, 0, tzinfo=fixed),
                session_id="2026-08-17",
                timezone="Asia/Shanghai",
                metadata={},
            )

    def test_rejects_forged_tzinfo_claiming_iana_key(self) -> None:
        # A custom fixed-offset tzinfo can forge a ``key`` attribute;
        # only a real ZoneInfo instance proves the IANA rule.
        class ForgedZone(tzinfo):
            key = "Asia/Shanghai"

            def utcoffset(self, dt):  # type: ignore[override]
                return timedelta(hours=8)

            def dst(self, dt):  # type: ignore[override]
                return None

            def tzname(self, dt):  # type: ignore[override]
                return "Asia/Shanghai"

        forged = ForgedZone()
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=0,
                start_time=datetime(2026, 8, 17, 9, 30, tzinfo=forged),
                end_time=datetime(2026, 8, 17, 15, 0, tzinfo=forged),
                session_id="2026-08-17",
                timezone="Asia/Shanghai",
                metadata={},
            )

    def test_rejects_unresolvable_timezone_name(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_step(timezone="Mars/Olympus_Mons")

    def test_rejects_start_after_end(self) -> None:
        step = make_step()
        with self.assertRaises(DomainValidationError):
            TimeStep(
                sequence=0,
                start_time=step.end_time,
                end_time=step.start_time,
                session_id=step.session_id,
                timezone=step.timezone,
                metadata={},
            )

    def test_rejects_blank_session_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_step(session_id="  ")

    def test_metadata_is_copied_and_frozen(self) -> None:
        source: dict[str, object] = {"session_date": "2026-08-17"}
        step = make_step(metadata=source)
        self.assertIsInstance(step.metadata, MappingProxyType)
        # Mutating the original mapping must not affect the step.
        source["session_date"] = "tampered"
        self.assertEqual(step.metadata["session_date"], "2026-08-17")
        # Mutating the step's view itself must fail.
        with self.assertRaises(TypeError):
            step.metadata["session_date"] = "other"  # type: ignore[index]

    def test_metadata_is_deep_frozen(self) -> None:
        source: dict[str, object] = {
            "nested": {"value": 1},
            "windows": [["09:30", "11:30"]],
        }
        step = make_step(metadata=source)
        # Mutating nested containers of the original mapping must not
        # reach the step.
        source["nested"]["value"] = 2
        source["windows"][0][0] = "tampered"
        self.assertEqual(step.metadata["nested"]["value"], 1)
        self.assertEqual(step.metadata["windows"], (("09:30", "11:30"),))
        nested = step.metadata["nested"]
        self.assertIsInstance(nested, MappingProxyType)
        with self.assertRaises(TypeError):
            nested["value"] = 3  # type: ignore[index]
        windows = step.metadata["windows"]
        self.assertIsInstance(windows, tuple)
        with self.assertRaises(TypeError):
            windows[0][0] = "other"  # type: ignore[index]


class TimeAxisTests(unittest.TestCase):
    def test_empty_axis_is_allowed(self) -> None:
        axis = TimeAxis([])
        self.assertEqual(len(axis), 0)
        self.assertEqual(tuple(axis), ())

    def test_one_and_two_steps_keep_order_and_sequence(self) -> None:
        for count in (1, 2):
            steps = make_steps(count)
            axis = TimeAxis(steps)
            self.assertEqual(len(axis), count)
            self.assertEqual(tuple(step.sequence for step in axis), tuple(range(count)))
            for index, step in enumerate(axis):
                self.assertIs(axis.at(index), step)

    def test_at_out_of_range_raises(self) -> None:
        axis = TimeAxis(make_steps(2))
        with self.assertRaises(IndexError):
            axis.at(2)

    def test_rejects_out_of_order_sequence(self) -> None:
        steps = make_steps(3)
        steps.reverse()
        with self.assertRaises(DomainValidationError):
            TimeAxis(steps)

    def test_rejects_sequence_gap(self) -> None:
        steps = make_steps(3)
        steps.append(make_step(sequence=5))
        with self.assertRaises(DomainValidationError):
            TimeAxis(steps)

    def test_rejects_duplicate_session_ids(self) -> None:
        first = make_step(sequence=0)
        second = make_step(sequence=1)
        duplicate = TimeStep(
            sequence=1,
            start_time=second.start_time,
            end_time=second.end_time,
            session_id=first.session_id,
            timezone=second.timezone,
            metadata=second.metadata,
        )
        with self.assertRaises(DomainValidationError):
            TimeAxis([first, duplicate])

    def test_rejects_non_step_entries(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeAxis([make_step(), "2026-08-18"])  # type: ignore[list-item]

    def test_does_not_fill_weekends_or_holidays(self) -> None:
        monday = make_point(date(2026, 8, 17))
        friday = make_point(date(2026, 8, 21))
        axis = TradingDayAxis([monday, friday])
        self.assertEqual(len(axis), 2)
        self.assertEqual(
            [step.session_id for step in axis],
            ["2026-08-17", "2026-08-21"],
        )


class TradingDayAxisTests(unittest.TestCase):
    def test_multi_window_first_open_and_last_close(self) -> None:
        axis = TradingDayAxis([make_point(date(2026, 8, 17))])
        step = axis.at(0)
        self.assertEqual(step.sequence, 0)
        self.assertEqual(step.session_id, "2026-08-17")
        self.assertEqual(step.timezone, "Asia/Shanghai")
        self.assertEqual(
            step.start_time,
            datetime(2026, 8, 17, 9, 30, tzinfo=SHANGHAI),
        )
        self.assertEqual(
            step.end_time,
            datetime(2026, 8, 17, 15, 0, tzinfo=SHANGHAI),
        )

    def test_windows_detail_preserved_in_metadata_not_as_steps(self) -> None:
        axis = TradingDayAxis([make_point(date(2026, 8, 17))])
        step = axis.at(0)
        self.assertEqual(len(axis), 1)
        self.assertEqual(
            step.metadata["session_windows"],
            (("09:30", "11:30"), ("13:00", "15:00")),
        )
        self.assertEqual(step.metadata["session_date"], "2026-08-17")

    def test_single_window_metadata(self) -> None:
        axis = TradingDayAxis([make_point(date(2026, 8, 17), sessions=SINGLE_WINDOW)])
        self.assertEqual(
            axis.at(0).metadata["session_windows"],
            (("09:30", "15:00"),),
        )

    def test_sequences_increase_from_zero_across_sessions(self) -> None:
        sessions = [make_point(date(2026, 8, 17) + timedelta(days=offset)) for offset in range(3)]
        axis = TradingDayAxis(sessions)
        self.assertEqual(
            [(step.sequence, step.session_id) for step in axis],
            [
                (0, "2026-08-17"),
                (1, "2026-08-18"),
                (2, "2026-08-19"),
            ],
        )

    def test_rejects_out_of_order_sessions(self) -> None:
        with self.assertRaises(DomainValidationError):
            TradingDayAxis(
                [
                    make_point(date(2026, 8, 18)),
                    make_point(date(2026, 8, 17)),
                ]
            )

    def test_rejects_duplicate_sessions(self) -> None:
        point = make_point(date(2026, 8, 17))
        with self.assertRaises(DomainValidationError):
            TradingDayAxis([point, point])

    def test_rejects_non_session_point_entries(self) -> None:
        with self.assertRaises(DomainValidationError):
            TradingDayAxis(["2026-08-17"])  # type: ignore[list-item]

    def test_repeated_construction_is_stable(self) -> None:
        sessions = [make_point(date(2026, 8, 17) + timedelta(days=offset)) for offset in range(3)]
        first = tuple(TradingDayAxis(sessions))
        second = tuple(TradingDayAxis(list(sessions)))
        self.assertEqual(first, second)

    def test_constructor_takes_no_provider(self) -> None:
        import inspect

        parameter_names = set(inspect.signature(TradingDayAxis.__init__).parameters)
        self.assertEqual(parameter_names, {"self", "resolved_sessions"})


class FixedTradingSessionsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = FixedTradingSessionsV1()

    def test_policy_identity_and_fixed_size(self) -> None:
        self.assertEqual(self.policy.policy_key, "fixed_trading_sessions")
        self.assertEqual(self.policy.policy_version, 1)
        self.assertEqual(self.policy.sessions_per_chunk, 20)
        self.assertEqual(SESSIONS_PER_CHUNK_V1, 20)

    def test_sessions_per_chunk_cannot_be_changed_at_runtime(self) -> None:
        with self.assertRaises(AttributeError):
            self.policy.sessions_per_chunk = 1  # type: ignore[misc]
        # The partition size stays pinned to the version constant even
        # after a tampering attempt.
        self.assertEqual(len(self.policy.partition(make_steps(21))), 2)

    def test_empty_input_returns_empty_tuple(self) -> None:
        self.assertEqual(self.policy.partition([]), ())

    def test_chunk_counts_for_boundary_sizes(self) -> None:
        expected_counts = {
            0: 0,
            1: 1,
            19: 1,
            20: 1,
            21: 2,
            40: 2,
            41: 3,
            42: 3,
        }
        for count, chunk_count in expected_counts.items():
            with self.subTest(sessions=count):
                chunks = self.policy.partition(make_steps(count))
                self.assertEqual(len(chunks), chunk_count)

    def test_chunks_hold_at_most_twenty_steps(self) -> None:
        chunks = self.policy.partition(make_steps(42))
        self.assertEqual([len(chunk.steps) for chunk in chunks], [20, 20, 2])

    def test_chunk_sequences_are_contiguous_from_zero(self) -> None:
        chunks = self.policy.partition(make_steps(42))
        self.assertEqual([chunk.chunk_sequence for chunk in chunks], [0, 1, 2])

    def test_first_and_last_session_ids(self) -> None:
        chunks = self.policy.partition(make_steps(42))
        self.assertEqual(chunks[0].first_session_id, make_step(0).session_id)
        self.assertEqual(chunks[0].last_session_id, make_step(19).session_id)
        self.assertEqual(chunks[1].first_session_id, make_step(20).session_id)
        self.assertEqual(chunks[1].last_session_id, make_step(39).session_id)
        self.assertEqual(chunks[2].first_session_id, make_step(40).session_id)
        self.assertEqual(chunks[2].last_session_id, make_step(41).session_id)

    def test_tail_chunk_allows_short_length(self) -> None:
        chunks = self.policy.partition(make_steps(21))
        self.assertEqual(len(chunks[1].steps), 1)
        self.assertEqual(chunks[1].steps[0].sequence, 20)

    def test_rejects_sequence_gaps_before_partitioning(self) -> None:
        steps = make_steps(21)
        steps.pop(10)
        with self.assertRaises(DomainValidationError):
            self.policy.partition(steps)

    def test_rejects_non_step_entries(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.policy.partition([make_step(), None])  # type: ignore[list-item]

    def test_partition_is_deterministic(self) -> None:
        steps = make_steps(42)
        first = self.policy.partition(steps)
        second = self.policy.partition(list(steps))
        self.assertEqual(first, second)

    def test_chunk_does_not_reset_step_sequences(self) -> None:
        # Cross-chunk continuity: sequence 19 lives in chunk 0 and
        # sequence 20 in chunk 1 with no renumbering anywhere.
        chunks = self.policy.partition(make_steps(21))
        self.assertEqual(chunks[0].steps[-1].sequence, 19)
        self.assertEqual(chunks[1].steps[0].sequence, 20)

    def test_time_chunk_rejects_empty_steps(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeChunk(chunk_sequence=0, steps=())

    def test_time_chunk_rejects_negative_sequence(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeChunk(chunk_sequence=-1, steps=(make_step(),))

    def test_time_chunk_copies_mutable_input_to_tuple(self) -> None:
        steps = make_steps(2)
        chunk = TimeChunk(chunk_sequence=0, steps=steps)
        self.assertIsInstance(chunk.steps, tuple)
        # Mutating the caller's list afterwards must not reach the chunk.
        steps.append(make_step(sequence=2))
        self.assertEqual(len(chunk.steps), 2)

    def test_time_chunk_rejects_non_step_entries_before_sequence_access(self) -> None:
        # A malformed first entry must fail as a domain error, not an
        # AttributeError from reading .sequence on it.
        with self.assertRaises(DomainValidationError):
            TimeChunk(chunk_sequence=0, steps=[None])  # type: ignore[list-item]
        with self.assertRaises(DomainValidationError):
            TimeChunk(chunk_sequence=0, steps=[make_step(), None])  # type: ignore[list-item]

    def test_time_chunk_rejects_non_contiguous_sequences(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeChunk(
                chunk_sequence=0,
                steps=(make_step(0), make_step(5)),
            )

    def test_time_chunk_rejects_reordered_sequences(self) -> None:
        with self.assertRaises(DomainValidationError):
            TimeChunk(
                chunk_sequence=0,
                steps=(make_step(1), make_step(0)),
            )


class AxisDataclassShapeTests(unittest.TestCase):
    def test_time_step_has_no_provider_or_session_fields(self) -> None:
        names = {field.name for field in fields(TimeStep)}
        self.assertEqual(
            names,
            {
                "sequence",
                "start_time",
                "end_time",
                "session_id",
                "timezone",
                "metadata",
            },
        )


if __name__ == "__main__":
    unittest.main()
