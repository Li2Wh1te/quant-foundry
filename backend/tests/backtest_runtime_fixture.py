"""Shared deterministic fixtures for backtesting runtime tests.

The fixtures implement the runner collaborator protocols directly over
in-memory dictionaries: a :class:`TradingDayAxis` over fixed Shanghai
sessions, a dictionary-backed engine market-data source, a counting
strategy data view, and a scripted strategy program.  No ORM, provider,
or database dependency is involved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo

from app.backtesting.calendar_axis import SessionPoint, SessionWindow
from app.backtesting.time_axis import TradingDayAxis
from app.strategy_protocol.context import DecisionContext
from app.strategy_protocol.data_view import (
    AdjustmentBasis,
    BarDTO,
    InstrumentCandidateDTO,
)
from app.backtesting.runtime import (
    EngineMarketData,
    InstrumentFacts,
    SessionQuote,
)

TEST_TIMEZONE = "Asia/Shanghai"

MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))

INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")


def build_axis(session_dates: Sequence[date]) -> TradingDayAxis:
    """Build a daily axis with one full A-share session shape per date."""

    points = [
        SessionPoint(
            session_date=day,
            session_id=f"session-{day.isoformat()}",
            timezone=TEST_TIMEZONE,
            sessions=(SessionWindow(*MORNING), SessionWindow(*AFTERNOON)),
        )
        for day in session_dates
    ]
    return TradingDayAxis(points)


def session_open(day: date) -> datetime:
    """The aware open instant of one fixture session."""

    return datetime.combine(day, MORNING[0], tzinfo=ZoneInfo(TEST_TIMEZONE))


def session_close(day: date) -> datetime:
    """The aware close instant of one fixture session."""

    return datetime.combine(day, AFTERNOON[1], tzinfo=ZoneInfo(TEST_TIMEZONE))


class DictMarketData:
    """Engine market data backed by ``{date: {instrument: (open, close)}}``."""

    def __init__(
        self,
        quotes_by_day: Mapping[date, Mapping[UUID, tuple[str, str]]],
        *,
        price_tick: str = "0.01",
        board_lot: int = 100,
        calendar_id: str = "XSHG",
        calendar_by_instrument: Mapping[UUID, str] | None = None,
        suspended_instruments: Sequence[UUID] = (),
    ) -> None:
        self._quotes_by_day = {
            day: {
                instrument_id: SessionQuote(
                    instrument_id=instrument_id,
                    session_date=day,
                    # An empty string models a missing price so fixtures can
                    # express incomplete bars without extra plumbing.
                    open_price=Decimal(open_price) if open_price else None,
                    close_price=Decimal(close_price) if close_price else None,
                )
                for instrument_id, (open_price, close_price) in quotes.items()
            }
            for day, quotes in quotes_by_day.items()
        }
        self._facts: dict[UUID, InstrumentFacts] = {}
        self._price_tick = price_tick
        self._board_lot = board_lot
        self._calendar_id = calendar_id
        self._calendar_by_instrument = dict(calendar_by_instrument or {})
        self._suspended = set(suspended_instruments)

    def session_quotes(
        self,
        instrument_ids: Sequence[UUID],
        session_date: date,
    ) -> Mapping[UUID, SessionQuote]:
        quotes = self._quotes_by_day.get(session_date, {})
        return {
            instrument_id: quotes[instrument_id]
            for instrument_id in instrument_ids
            if instrument_id in quotes
        }

    def instrument_facts(
        self, instrument_ids: Sequence[UUID]
    ) -> Mapping[UUID, InstrumentFacts]:
        for instrument_id in instrument_ids:
            if instrument_id not in self._facts:
                # Trading-status facts are always explicit; nothing defaults
                # to "tradable".
                self._facts[instrument_id] = InstrumentFacts(
                    instrument_id=instrument_id,
                    price_tick=Decimal(self._price_tick),
                    calendar_id=self._calendar_by_instrument.get(
                        instrument_id, self._calendar_id
                    ),
                    suspended=instrument_id in self._suspended,
                    buy_allowed=True,
                    sell_allowed=True,
                    board_lot=Decimal(self._board_lot),
                )
        return MappingProxyType(
            {instrument_id: self._facts[instrument_id] for instrument_id in instrument_ids}
        )


class SessionListSettlementCalendar:
    """Settlement gateway over explicit per-calendar official sessions."""

    def __init__(
        self, sessions_by_calendar: Mapping[str, Sequence[date]]
    ) -> None:
        self._sessions = {
            calendar_id: sorted(dates)
            for calendar_id, dates in sessions_by_calendar.items()
        }
        self.resolved_requests: list[tuple[str, date]] = []

    def next_open_session(
        self, calendar_id: str, *, after_session: date
    ) -> date | None:
        self.resolved_requests.append((calendar_id, after_session))
        for candidate in self._sessions.get(calendar_id, ()):
            if candidate > after_session:
                return candidate
        return None


class CountingStrategyView:
    """Strategy data view serving bars up to the requested window only.

    Every underlying read increments ``read_count`` so tests can prove a
    rejected query never touched the data source.
    """

    def __init__(
        self,
        closes_by_day: Mapping[date, str],
        instrument_id: UUID = INSTRUMENT_ID,
    ) -> None:
        self._closes_by_day = dict(closes_by_day)
        self._instrument_id = instrument_id
        self.read_count = 0

    def bars(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> Sequence[BarDTO]:
        self.read_count += 1
        rows = []
        for day in sorted(self._closes_by_day):
            if start_date is not None and day < start_date:
                continue
            if end_date is not None and day > end_date:
                continue
            rows.append(
                BarDTO(
                    instrument_id=instrument_id,
                    trade_date=day,
                    values={
                        "close": Decimal(self._closes_by_day[day]),
                    },
                )
            )
        return tuple(rows)

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
        basis: AdjustmentBasis,
    ) -> Sequence[object]:
        self.read_count += 1
        return ()


def make_candidate(
    instrument_id: UUID = INSTRUMENT_ID,
) -> InstrumentCandidateDTO:
    """One PIT candidate carrying display identity for portfolio DTOs."""

    return InstrumentCandidateDTO(
        instrument_id=instrument_id,
        trading_code="510300",
        name="沪深300ETF",
        display_name="沪深300ETF",
        asset_class="etf",
        exchange="SSE",
    )


class _StaticUniverseQuery:
    """A :class:`UniverseQuery` returning one fixed candidate tuple."""

    def __init__(self, candidates: Sequence[InstrumentCandidateDTO]) -> None:
        self._candidates = tuple(candidates)

    def query(self, *, exchanges=None, asset_classes=None):
        return self._candidates


def universe_query(candidates: Sequence[InstrumentCandidateDTO]):
    """Build a :class:`UniverseQuery` over one fixed candidate list."""

    return _StaticUniverseQuery(candidates)


class ScriptedStrategy:
    """Strategy program returning pre-scripted target weights per step.

    Steps without a script default to holding everything (empty targets),
    which the interpreter reads as "flatten nothing / stay as-is".
    """

    def __init__(self, targets_by_step: Mapping[int, Mapping[str, str]]) -> None:
        self.targets_by_step = dict(targets_by_step)
        self.observed_contexts: list[DecisionContext] = []

    def on_step(self, context: DecisionContext) -> object:
        self.observed_contexts.append(context)
        from app.strategy_protocol.decisions import StrategyDecision

        return StrategyDecision(
            step_sequence=context.step_sequence,
            decision_time=context.decision_time,
            mode="target_weights",
            targets=dict(self.targets_by_step.get(context.step_sequence, {})),
        )


class RecordingExecutionModel:
    """Decorator around an execution model that records produced fills."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.recorded_fills = []

    def __getattr__(self, name: str):
        # Unhandled attributes (model_key, model_version, ...) proxy to the
        # wrapped model so decorators stay transparent to auditors.
        return getattr(self._inner, name)

    def match(self, orders, market_states, context):
        result = self._inner.match(orders, market_states, context)
        self.recorded_fills.extend(result.fills)
        return result


def build_runner(
    *,
    run_id: str,
    axis,
    market_data: DictMarketData,
    strategy_view: CountingStrategyView,
    strategy: ScriptedStrategy,
    scope_instrument_ids: Sequence[UUID] = (INSTRUMENT_ID,),
    candidates: Sequence[InstrumentCandidateDTO] | None = None,
    corporate_actions=None,
    initial_cash: str = "10000",
    settlement_calendar: SessionListSettlementCalendar | None = None,
    interpreter=None,
    component_parameters=None,
    accounting_currency: str = "CNY",
    execution_model=None,
    settlement_policy=None,
):
    """Assemble a fully wired runner with zero slippage and zero fees.

    The settlement gateway is built from the axis's official sessions for
    the fixture calendar unless an explicit gateway is supplied.
    """

    from app.backtesting.accounting import (
        AccountState,
        AccountingPolicy,
        PortfolioState,
        SettlementPolicy,
    )
    from app.backtesting.execution import BarMarketExecutionModel
    from app.backtesting.fees import FeeCalculator, FeeSchedule
    from app.backtesting.runtime import (
        BacktestViewFactory,
        DeterministicBacktestRunner,
        TargetWeightsInterpreter,
    )
    from app.backtesting.slippage import BpsSlippageModel

    if settlement_calendar is None:
        session_dates = [
            date.fromisoformat(step.metadata["session_date"]) for step in axis
        ]
        settlement_calendar = SessionListSettlementCalendar({"XSHG": session_dates})

    view_factory = BacktestViewFactory(
        strategy_view=strategy_view,
        universe_query=universe_query(
            candidates if candidates is not None else [make_candidate()]
        ),
        engine_market_data=market_data,
        scope_instrument_ids=tuple(scope_instrument_ids),
    )
    execution_model = RecordingExecutionModel(
        execution_model
        if execution_model is not None
        else BarMarketExecutionModel(
            slippage_model=BpsSlippageModel.none(price_tick="0.01"),
            fee_calculator=FeeCalculator(
                FeeSchedule(key="runtime_fixture", version=1, fee_rules=())
            ),
            model_key="bar_market",
            model_version=1,
        )
    )
    accounting = AccountingPolicy(
        currency=accounting_currency,
        settlement_policy=(
            settlement_policy
            if settlement_policy is not None
            else SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH
        ),
    )
    first_day = axis.at(0).metadata["session_date"]
    portfolio = PortfolioState(
        account=AccountState(
            cash_balances={"CNY": initial_cash},
            available_cash=initial_cash,
            frozen_cash="0",
            margin_used="0",
            margin_available="0",
            equity=initial_cash,
        ),
        as_of=session_open(date.fromisoformat(first_day)),
    )
    return DeterministicBacktestRunner(
        run_id=run_id,
        axis=axis,
        timing_policy=_timing_policy(),
        view_factory=view_factory,
        strategy=strategy,
        interpreter=(
            interpreter
            if interpreter is not None
            else TargetWeightsInterpreter(board_lot=100)
        ),
        execution_model=execution_model,
        accounting=accounting,
        initial_portfolio=portfolio,
        settlement_calendar=settlement_calendar,
        corporate_actions=corporate_actions,
        component_parameters=component_parameters,
    )


def _timing_policy():
    from app.backtesting.timing import AfterCloseToNextOpenV1

    return AfterCloseToNextOpenV1()
