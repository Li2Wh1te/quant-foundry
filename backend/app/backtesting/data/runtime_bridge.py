"""Adapt protocol-backed chunks to the deterministic engine's two data views.

Trading status remains an explicit dependency: bars alone never imply that a
market is tradable. Production adapters and named test fixtures can supply the
same resolver boundary without importing their storage implementations here.
"""

from datetime import date
from app.backtesting.runtime import BacktestViewFactory, InstrumentFacts, SessionQuote
from app.backtesting.data.requests import BarQuery, DateRange, InstrumentQuery, QueryBoundary
from app.backtesting.data.views import ChunkStrategyDataView
from app.backtesting.data.errors import ProviderContractViolationError


class ChunkBacktestViewFactory(BacktestViewFactory):
    def __init__(self, *, request, universe_query, trading_status_resolver):
        self.request = request
        self.chunk = None
        self.step = None
        self._trading_status_resolver = trading_status_resolver
        super().__init__(strategy_view=self, universe_query=universe_query,
                         engine_market_data=self, scope_instrument_ids=request.fixed_instrument_ids)

    def bind_chunk(self, chunk):
        self.chunk = chunk

    def unbind_chunk(self, _chunk):
        self.chunk = None

    def _chunk(self):
        if self.chunk is None or self.step is None:
            raise ProviderContractViolationError("runtime view is outside an active data chunk")
        return self.chunk

    def for_phase(self, instruction, step, *, next_step):
        self.step = step
        return super().for_phase(instruction, step, next_step=next_step)

    def for_step(self, *, effective_date, data_cutoff):
        return ChunkStrategyDataView(chunk=self._chunk(), frequency=self.request.frequency,
                                     data_cutoff=data_cutoff, include_cutoff_day=True,
                                     effective_date=effective_date)

    def session_quotes(self, instrument_ids, session_date):
        # Only the engine receives full same-session bars for matching and
        # valuation. Strategy reads remain bounded by the phase timestamp.
        rows = self._chunk().bars(BarQuery(
            instrument_ids=tuple(instrument_ids), frequency=self.request.frequency,
            window=DateRange(session_date, session_date),
            boundary=QueryBoundary(self.step.end_time, include_cutoff_day=True),
        ))
        return {row.instrument_id: SessionQuote(row.instrument_id, row.trade_date, row.open, row.close,
                {"source": row.evidence.source, "source_revision": row.evidence.source_revision,
                 "known_at": row.evidence.known_at.isoformat() if row.evidence.known_at else None}) for row in rows}

    def instrument_facts(self, instrument_ids):
        chunk = self._chunk()
        specs = chunk.instruments(InstrumentQuery(tuple(instrument_ids), self.step.end_time, QueryBoundary(self.step.end_time, include_cutoff_day=True)))
        results = {}
        for spec in specs:
            status = self._trading_status_resolver(chunk, spec.instrument_id, date.fromisoformat(self.step.metadata["session_date"]))
            results[spec.instrument_id] = InstrumentFacts(
                instrument_id=spec.instrument_id, price_tick=spec.price_tick,
                board_lot=spec.lot_size, contract_multiplier=spec.contract_multiplier,
                calendar_id=spec.calendar_id, suspended=status["suspended"],
                buy_allowed=status["buy_allowed"], sell_allowed=status["sell_allowed"],
                fee_applicability_context={"asset_class": spec.asset_class, "exchange": spec.exchange, "currency": spec.currency},
            )
        if set(results) != set(instrument_ids):
            raise ProviderContractViolationError("chunk lacks required instrument facts")
        return results
