"""Versioned timing policies that turn one time step into phase instructions.

The first version implements ``after_close_to_next_open@1``: within every
official step the engine settles at the open, matches orders submitted at
the previous close, values and analyzes at the close, and lets the
strategy decide and submit orders that become effective at the *next*
step's open.

``next_step`` is supplied by the engine from the complete official
timeline; a policy never queries the axis or a data provider to find it,
and it must reject a ``next_step`` that does not continue the sequence
instead of looking ahead, reordering, or skipping steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.backtesting.time_axis import TimeStep

__all__ = [
    "TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN",
    "TIMING_POLICY_VERSION_V1",
    "AfterCloseToNextOpenV1",
    "DataViewKind",
    "TimingInstruction",
    "TimingPhase",
    "TimingPolicy",
]


class DataViewKind(StrEnum):
    """Which data view a phase may read.

    Engine phases see raw engine facts only; the decide phase is the sole
    reader of the strategy view with its point-in-time cutoff.
    """

    ENGINE = "engine"
    STRATEGY = "strategy"


class TimingPhase(StrEnum):
    """Ordered execution phases inside one official time step."""

    PRE_OPEN_SETTLE = "pre_open_settle"
    OBSERVE = "observe"
    MATCH = "match"
    ACCOUNT = "account"
    CASH_ACTIONS = "cash_actions"
    VALUE = "value"
    ANALYZE = "analyze"
    DECIDE = "decide"
    SUBMIT = "submit"


@dataclass(frozen=True, slots=True)
class TimingInstruction:
    """One immutable phase instruction emitted by a timing policy."""

    phase: TimingPhase
    timestamp: datetime
    data_view: DataViewKind | None
    effective_from: datetime | None = None


class TimingPolicy(Protocol):
    """Structural contract every timing policy implementation satisfies."""

    policy_key: str
    policy_version: int

    def phases(
        self,
        step: TimeStep,
        *,
        next_step: TimeStep | None,
    ) -> tuple[TimingInstruction, ...]:
        """Return the ordered phase instructions for one step.

        ``next_step is None`` means this step is the final step of the
        complete official timeline.
        """
        ...


TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN = "after_close_to_next_open"
TIMING_POLICY_VERSION_V1 = 1

# Phase order of every non-final step.  Instructions execute strictly in
# tuple order even when several share one timestamp; no datetime sorting
# happens downstream.
_NON_FINAL_PHASE_ORDER: tuple[TimingPhase, ...] = (
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

# A final step ends the run: valuation and analysis still happen, but no
# strategy decision and no new order may be produced.
_FINAL_PHASE_ORDER: tuple[TimingPhase, ...] = _NON_FINAL_PHASE_ORDER[:-2]

_DATA_VIEW_BY_PHASE: dict[TimingPhase, DataViewKind | None] = {
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


class AfterCloseToNextOpenV1:
    """The ``after_close_to_next_open@1`` timing policy.

    Open-time phases settle, observe, match, account, and process cash
    actions at ``step.start_time``; close-time phases value, analyze,
    decide, and submit at ``step.end_time``.  Submitted orders become
    effective at ``next_step.start_time``, which is why the final step --
    having no next step -- emits neither a decision nor a submission.
    Chunk boundaries never truncate the sequence: a chunk-tail step that
    still has a successor keeps its decide/submit phases.
    """

    policy_key: str = TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN
    policy_version: int = TIMING_POLICY_VERSION_V1

    def phases(
        self,
        step: TimeStep,
        *,
        next_step: TimeStep | None,
    ) -> tuple[TimingInstruction, ...]:
        if not isinstance(step, TimeStep):
            raise DomainValidationError("step must be a TimeStep")
        if next_step is not None:
            if not isinstance(next_step, TimeStep):
                raise DomainValidationError("next_step must be a TimeStep or None")
            if next_step.sequence != step.sequence + 1:
                raise DomainValidationError(
                    "next_step must continue the timeline: expected sequence "
                    f"{step.sequence + 1}, got {next_step.sequence}"
                )
        phase_order = (
            _FINAL_PHASE_ORDER if next_step is None else _NON_FINAL_PHASE_ORDER
        )
        return tuple(
            self._instruction(step, phase, next_step) for phase in phase_order
        )

    @staticmethod
    def _instruction(
        step: TimeStep,
        phase: TimingPhase,
        next_step: TimeStep | None,
    ) -> TimingInstruction:
        timestamp = (
            step.start_time
            if phase
            in (
                TimingPhase.PRE_OPEN_SETTLE,
                TimingPhase.OBSERVE,
                TimingPhase.MATCH,
                TimingPhase.ACCOUNT,
                TimingPhase.CASH_ACTIONS,
            )
            else step.end_time
        )
        effective_from = None
        if phase is TimingPhase.SUBMIT:
            # Only reachable on non-final steps where next_step is a
            # validated TimeStep by the sequence check above.
            assert next_step is not None
            effective_from = _aware_datetime(next_step.start_time, "effective_from")
        return TimingInstruction(
            phase=phase,
            timestamp=timestamp,
            data_view=_DATA_VIEW_BY_PHASE[phase],
            effective_from=effective_from,
        )
