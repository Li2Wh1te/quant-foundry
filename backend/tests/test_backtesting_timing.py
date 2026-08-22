"""Tests for the after_close_to_next_open@1 timing policy."""

import unittest
from dataclasses import fields
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.backtesting.domain import DomainValidationError
from app.backtesting.time_axis import TimeStep
from app.backtesting.timing import (
    TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN,
    AfterCloseToNextOpenV1,
    DataViewKind,
    TimingInstruction,
    TimingPhase,
    TimingPolicy,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

EXPECTED_NON_FINAL_PHASES = (
    TimingPhase.PRE_OPEN_SETTLE,
    TimingPhase.OBSERVE,
    TimingPhase.MATCH,
    TimingPhase.ACCOUNT,
    TimingPhase.CASH_ACTIONS,
    TimingPhase.VALUE,
    TimingPhase.ANALYZE,
    TimingPhase.DECIDE,
    TimingPhase.SUBMIT,
)
EXPECTED_FINAL_PHASES = EXPECTED_NON_FINAL_PHASES[:-2]


def make_step(sequence: int) -> TimeStep:
    day = date(2026, 8, 17) + timedelta(days=sequence)
    return TimeStep(
        sequence=sequence,
        start_time=datetime(day.year, day.month, day.day, 9, 30, tzinfo=SHANGHAI),
        end_time=datetime(day.year, day.month, day.day, 15, 0, tzinfo=SHANGHAI),
        session_id=day.isoformat(),
        timezone="Asia/Shanghai",
        metadata={"session_date": day.isoformat()},
    )


class AfterCloseToNextOpenV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AfterCloseToNextOpenV1()

    # ------------------------------------------------------------------
    # Policy identity and structural contract
    # ------------------------------------------------------------------

    def test_policy_identity(self) -> None:
        self.assertEqual(self.policy.policy_key, "after_close_to_next_open")
        self.assertEqual(self.policy.policy_version, 1)
        self.assertEqual(TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN, "after_close_to_next_open")
        # The policy must satisfy the TimingPolicy protocol shape.
        self.assertTrue(hasattr(TimingPolicy, "phases"))

    # ------------------------------------------------------------------
    # Non-final step: the full nine-phase order
    # ------------------------------------------------------------------

    def test_non_final_phase_order_matches_contract_exactly(self) -> None:
        d1 = make_step(0)
        d2 = make_step(1)
        instructions = self.policy.phases(d1, next_step=d2)
        self.assertEqual(
            tuple(instruction.phase for instruction in instructions),
            EXPECTED_NON_FINAL_PHASES,
        )

    def test_non_final_timestamps(self) -> None:
        d1 = make_step(0)
        instructions = self.policy.phases(d1, next_step=make_step(1))
        expected = {
            TimingPhase.PRE_OPEN_SETTLE: d1.start_time,
            TimingPhase.OBSERVE: d1.start_time,
            TimingPhase.MATCH: d1.start_time,
            TimingPhase.ACCOUNT: d1.start_time,
            TimingPhase.CASH_ACTIONS: d1.start_time,
            TimingPhase.VALUE: d1.end_time,
            TimingPhase.ANALYZE: d1.end_time,
            TimingPhase.DECIDE: d1.end_time,
            TimingPhase.SUBMIT: d1.end_time,
        }
        for instruction in instructions:
            self.assertEqual(instruction.timestamp, expected[instruction.phase])

    def test_data_views_match_contract(self) -> None:
        instructions = self.policy.phases(make_step(0), next_step=make_step(1))
        expected = {
            TimingPhase.PRE_OPEN_SETTLE: None,
            TimingPhase.OBSERVE: DataViewKind.ENGINE,
            TimingPhase.MATCH: DataViewKind.ENGINE,
            TimingPhase.ACCOUNT: None,
            TimingPhase.CASH_ACTIONS: DataViewKind.ENGINE,
            TimingPhase.VALUE: DataViewKind.ENGINE,
            TimingPhase.ANALYZE: None,
            TimingPhase.DECIDE: DataViewKind.STRATEGY,
            TimingPhase.SUBMIT: None,
        }
        for instruction in instructions:
            self.assertEqual(instruction.data_view, expected[instruction.phase])
        for instruction in instructions:
            if instruction.phase is not TimingPhase.SUBMIT:
                self.assertIsNone(instruction.effective_from)

    def test_submit_effective_from_is_next_open(self) -> None:
        d1 = make_step(0)
        d2 = make_step(1)
        submit = self.policy.phases(d1, next_step=d2)[-1]
        self.assertIs(submit.phase, TimingPhase.SUBMIT)
        self.assertEqual(submit.effective_from, d2.start_time)

    def test_decide_uses_current_close_as_cutoff(self) -> None:
        d1 = make_step(0)
        decide = self.policy.phases(d1, next_step=make_step(1))[-2]
        self.assertIs(decide.phase, TimingPhase.DECIDE)
        self.assertIs(decide.data_view, DataViewKind.STRATEGY)
        self.assertEqual(decide.timestamp, d1.end_time)

    def test_next_step_not_injected_into_any_instruction_payload(self) -> None:
        # next_step only fixes the order effective time; no instruction
        # may carry any other field derived from it.
        d1 = make_step(0)
        d2 = make_step(1)
        for instruction in self.policy.phases(d1, next_step=d2):
            payload = {
                field.name: getattr(instruction, field.name)
                for field in fields(TimingInstruction)
            }
            self.assertIn(payload["timestamp"], (d1.start_time, d1.end_time))
            if payload["effective_from"] is not None:
                self.assertEqual(payload["effective_from"], d2.start_time)
                self.assertIs(payload["phase"], TimingPhase.SUBMIT)

    # ------------------------------------------------------------------
    # Final step of the official timeline
    # ------------------------------------------------------------------

    def test_final_step_has_no_decide_or_submit(self) -> None:
        last = make_step(2)
        instructions = self.policy.phases(last, next_step=None)
        phases = tuple(instruction.phase for instruction in instructions)
        self.assertEqual(phases, EXPECTED_FINAL_PHASES)
        self.assertNotIn(TimingPhase.DECIDE, phases)
        self.assertNotIn(TimingPhase.SUBMIT, phases)

    def test_first_and_last_steps_of_three_day_timeline(self) -> None:
        d1 = make_step(0)
        d3 = make_step(2)
        first_run = self.policy.phases(d1, next_step=make_step(1))
        final_run = self.policy.phases(d3, next_step=None)
        self.assertIs(first_run[-1].phase, TimingPhase.SUBMIT)
        self.assertNotIn(TimingPhase.SUBMIT, [step.phase for step in final_run])

    # ------------------------------------------------------------------
    # Chunk boundaries never truncate the phase sequence
    # ------------------------------------------------------------------

    def test_chunk_tail_but_not_run_tail_still_decides_and_submits(self) -> None:
        # sequence 19 would be the last step of chunk 0 under
        # fixed_trading_sessions@1, yet its successor lives in chunk 1.
        chunk_tail = make_step(19)
        chunk_head = make_step(20)
        instructions = self.policy.phases(chunk_tail, next_step=chunk_head)
        phases = tuple(instruction.phase for instruction in instructions)
        self.assertEqual(phases, EXPECTED_NON_FINAL_PHASES)
        submit = instructions[-1]
        self.assertEqual(submit.effective_from, chunk_head.start_time)

    # ------------------------------------------------------------------
    # Determinism and strict ordering
    # ------------------------------------------------------------------

    def test_repeated_calls_return_identical_results(self) -> None:
        step = make_step(0)
        successor = make_step(1)
        first = self.policy.phases(step, next_step=successor)
        second = self.policy.phases(step, next_step=successor)
        self.assertEqual(first, second)
        final_first = self.policy.phases(step, next_step=None)
        final_second = self.policy.phases(step, next_step=None)
        self.assertEqual(final_first, final_second)

    def test_same_timestamp_keeps_tuple_order_without_datetime_sorting(self) -> None:
        # Five open phases share one timestamp and four close phases share
        # another; the emitted order must stay the contract order.
        instructions = self.policy.phases(make_step(0), next_step=make_step(1))
        seen_timestamps: list[datetime] = []
        for instruction in instructions:
            if seen_timestamps and instruction.timestamp == seen_timestamps[-1]:
                continue
            seen_timestamps.append(instruction.timestamp)
        self.assertEqual(len(seen_timestamps), 2)
        self.assertEqual(seen_timestamps[0], make_step(0).start_time)
        self.assertEqual(seen_timestamps[1], make_step(0).end_time)

    # ------------------------------------------------------------------
    # Sequence continuation guard
    # ------------------------------------------------------------------

    def test_rejects_non_contiguous_next_step(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.policy.phases(make_step(0), next_step=make_step(5))

    def test_rejects_backwards_next_step(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.policy.phases(make_step(1), next_step=make_step(0))

    def test_rejects_non_step_arguments(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.policy.phases("2026-08-17", next_step=None)  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            self.policy.phases(make_step(0), next_step="2026-08-18")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
