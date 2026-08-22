"""Deterministic synthetic data fixture for the startup contract check.

The fixture builds a minimal ``DecisionContext`` whose DTO shape is identical
to a real run's context while depending on no real account, no real market
data, no user files, no network services, or any other mutable external
state.  Every value is derived from fixed constants so repeated checks are
bit-for-bit reproducible.

Per the approved design, the run's ``static_instrument_ids`` and every
non-zero ``initial_position`` are injected as synthetic identity rows with
valid bars, so legitimate strategies are never failed for "missing synthetic
market data".  Identities outside this table keep the official unknown-
identity error semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.backtesting.domain import PositionSide

from .context import (
    DecisionContext,
    DeterministicClockDTO,
    PortfolioDTO,
    PositionDTO,
    PreviousStepDTO,
)
from .contract import UnknownInstrumentError
from .data_view import (
    AdjustmentBasis,
    AdjustedSeriesPointDTO,
    BarDTO,
    InstrumentCandidateDTO,
    StrategyDataDTO,
    StrategyDataView,
    UniverseQueryDTO,
)

SYNTHETIC_TIMEZONE = "Asia/Shanghai"
"""Fixed timezone of the synthetic session; never read from the host."""

SYNTHETIC_SESSION_OFFSETS = (4, 3, 2, 1, 0)
"""Day offsets of the five synthetic sessions, ending at the decision day."""

SYNTHETIC_BAR_CLOSE = Decimal("10.0000")
SYNTHETIC_BAR_OPEN = Decimal("9.9000")
SYNTHETIC_BAR_HIGH = Decimal("10.1000")
SYNTHETIC_BAR_LOW = Decimal("9.8000")

_VIRTUAL_CANDIDATE_NAMES = ("virtual-alpha", "virtual-beta", "virtual-gamma")


def synthetic_instrument_id(name: str) -> UUID:
    """Deterministically derive a stable synthetic instrument identity."""

    return uuid5(NAMESPACE_URL, f"https://synthetic.quant-foundry.local/{name}")


@dataclass(frozen=True, slots=True)
class SyntheticIdentityRow:
    """One injected synthetic identity with its display metadata."""

    instrument_id: UUID
    trading_code: str
    name: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ContractCheckParameters:
    """Frozen inputs of one synthetic contract-check scenario."""

    session_date: date
    decision_time: datetime
    data_cutoff: datetime
    static_instrument_ids: tuple[UUID, ...]
    initial_positions: tuple[Mapping[str, object], ...]
    parameters: Mapping[str, object] = field(default_factory=dict)


class SyntheticDataView(StrategyDataView):
    """In-memory read side serving only the injected synthetic bars."""

    def __init__(self, bars_by_instrument: Mapping[UUID, Sequence[BarDTO]]) -> None:
        self._bars = {
            instrument_id: tuple(rows) for instrument_id, rows in bars_by_instrument.items()
        }

    def _require_known(self, instrument_id: UUID) -> tuple[BarDTO, ...]:
        rows = self._bars.get(instrument_id)
        if rows is None:
            # Same error type and semantics as the real run path.
            raise UnknownInstrumentError(
                f"instrument_id {instrument_id} has no synthetic identity row"
            )
        return rows

    def bars(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> Sequence[BarDTO]:
        rows = self._require_known(instrument_id)
        selected = [
            row
            for row in rows
            if (start_date is None or row.trade_date >= start_date)
            and (end_date is None or row.trade_date <= end_date)
        ]
        if lookback_sessions is not None:
            selected = selected[-lookback_sessions:]
        return tuple(selected)

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
        basis: AdjustmentBasis,
    ) -> Sequence[AdjustedSeriesPointDTO]:
        rows = self._require_known(instrument_id)
        if basis is AdjustmentBasis.RAW:
            # Raw series carry no adjustment factors by definition.
            return ()
        selected = [
            row
            for row in rows
            if (start_date is None or row.trade_date >= start_date)
            and (end_date is None or row.trade_date <= end_date)
        ]
        if lookback_sessions is not None:
            selected = selected[-lookback_sessions:]
        return tuple(
            AdjustedSeriesPointDTO(
                instrument_id=row.instrument_id,
                trade_date=row.trade_date,
                adj_factor=Decimal("1"),
            )
            for row in selected
        )


class SyntheticUniverse:
    """Fixed virtual candidates plus the injected synthetic identity rows."""

    def __init__(self, candidates: Sequence[InstrumentCandidateDTO]) -> None:
        self._candidates = tuple(candidates)

    def query(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        asset_classes: Iterable[str] | None = None,
    ) -> Sequence[InstrumentCandidateDTO]:
        selected = self._candidates
        if exchanges is not None:
            wanted = set(exchanges)
            selected = [row for row in selected if row.exchange in wanted]
        if asset_classes is not None:
            wanted = set(asset_classes)
            selected = [row for row in selected if row.asset_class in wanted]
        return tuple(selected)


def build_synthetic_context(
    request: ContractCheckParameters,
) -> tuple[DecisionContext, tuple[SyntheticIdentityRow, ...]]:
    """Build the full synthetic decision context and its identity evidence.

    Returns the context plus the injected identity-row summary that becomes
    part of the contract-check evidence.
    """

    # Deterministic five-session calendar ending at the decision session.
    base = request.session_date
    sessions = [base - timedelta(days=offset) for offset in SYNTHETIC_SESSION_OFFSETS]

    identities: list[SyntheticIdentityRow] = []
    known_ids: set[UUID] = set()

    def register_identity(instrument_id: UUID, label: str) -> SyntheticIdentityRow:
        row = SyntheticIdentityRow(
            instrument_id=instrument_id,
            trading_code=f"SYN.{label}",
            name=f"合成标的 {label}",
            display_name=f"Synthetic {label}",
        )
        if instrument_id not in known_ids:
            identities.append(row)
            known_ids.add(instrument_id)
        return row

    for name in _VIRTUAL_CANDIDATE_NAMES:
        register_identity(synthetic_instrument_id(name), name)
    for instrument_id in request.static_instrument_ids:
        register_identity(instrument_id, f"static-{str(instrument_id)[:8]}")

    bars_by_instrument: dict[UUID, list[BarDTO]] = {}
    for instrument_id in known_ids:
        bars_by_instrument[instrument_id] = [
            BarDTO(
                instrument_id=instrument_id,
                trade_date=day,
                values={
                    "open": SYNTHETIC_BAR_OPEN,
                    "high": SYNTHETIC_BAR_HIGH,
                    "low": SYNTHETIC_BAR_LOW,
                    "close": SYNTHETIC_BAR_CLOSE,
                },
            )
            for day in sessions
        ]

    # Non-zero initial positions become portfolio rows backed by synthetic
    # identity rows and bars, per V5-1.
    positions: list[PositionDTO] = []
    for index, position in enumerate(request.initial_positions):
        instrument_id = position.get("instrument_id")
        if not isinstance(instrument_id, UUID):
            raise ValueError("initial position instrument_id must be a UUID")
        quantity = Decimal(str(position.get("quantity", "0")))
        if quantity <= 0:
            continue
        row = register_identity(instrument_id, f"position-{index}")
        positions.append(
            PositionDTO(
                instrument_id=instrument_id,
                trading_code=row.trading_code,
                name=row.name,
                display_name=row.display_name,
                side=PositionSide(position.get("side", PositionSide.LONG)),
                quantity=quantity,
                available_quantity=Decimal(str(position.get("available_quantity", quantity))),
                average_price=Decimal(str(position.get("average_price", SYNTHETIC_BAR_CLOSE))),
                mark_price=SYNTHETIC_BAR_CLOSE,
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        )
        if instrument_id not in bars_by_instrument:
            bars_by_instrument[instrument_id] = [
                BarDTO(
                    instrument_id=instrument_id,
                    trade_date=day,
                    values={
                        "open": SYNTHETIC_BAR_OPEN,
                        "high": SYNTHETIC_BAR_HIGH,
                        "low": SYNTHETIC_BAR_LOW,
                        "close": SYNTHETIC_BAR_CLOSE,
                    },
                )
                for day in sessions
            ]

    clock = DeterministicClockDTO(
        decision_time=request.decision_time,
        session_date=request.session_date,
    )
    portfolio = PortfolioDTO(
        cash_balances={"CNY": Decimal("1000000.00")},
        available_cash=Decimal("1000000.00"),
        frozen_cash=Decimal("0"),
        margin_used=Decimal("0"),
        margin_available=Decimal("1000000.00"),
        equity=Decimal("1000000.00") + sum(p.quantity * p.mark_price for p in positions),
        positions=tuple(positions),
    )
    candidates = tuple(
        InstrumentCandidateDTO(
            instrument_id=row.instrument_id,
            trading_code=row.trading_code,
            name=row.name,
            display_name=row.display_name,
            asset_class="etf",
            exchange="SSE",
        )
        for row in identities
    ) + tuple(
        # The dynamic scope keeps using the fixed virtual candidates only;
        # injected identity rows exist so static ids and held positions are
        # never failed for missing synthetic market data (V5-1).
        InstrumentCandidateDTO(
            instrument_id=synthetic_instrument_id(name),
            trading_code=f"SYN.{name.upper()}",
            name=f"虚拟标的 {name}",
            display_name=f"Virtual {name}",
            asset_class="etf",
            exchange="SSE",
        )
        for name in _VIRTUAL_CANDIDATE_NAMES
        if synthetic_instrument_id(name) not in known_ids
    )
    context = DecisionContext(
        step_sequence=1,
        session_date=request.session_date,
        decision_time=request.decision_time,
        data_cutoff=request.data_cutoff,
        timezone=SYNTHETIC_TIMEZONE,
        clock=clock,
        portfolio=portfolio,
        previous_step=PreviousStepDTO(step_sequence=0),
        data=StrategyDataDTO(
            SyntheticDataView(bars_by_instrument),
            data_cutoff=request.data_cutoff,
            adjustment_gate=None,  # synthetic qfq/hfq stay blocked like an unverified policy
        ),
        universe=UniverseQueryDTO(SyntheticUniverse(candidates)),
    )
    evidence = tuple(identities)
    return context, evidence


__all__ = [
    "ContractCheckParameters",
    "SYNTHETIC_TIMEZONE",
    "SyntheticDataView",
    "SyntheticIdentityRow",
    "SyntheticUniverse",
    "build_synthetic_context",
    "synthetic_instrument_id",
]
