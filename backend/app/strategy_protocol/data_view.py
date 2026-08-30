"""Data and candidate-set query contract for page strategies.

The protocols are deliberately generic: they key every query by the stable
``instrument_id`` and never expose ETF trading codes, ORM sessions, FastAPI
types, or Tushare clients to strategy code.  Concrete adapters (including the
synthetic fixture) implement the read side; this module owns the boundary
rules that every implementation shares:

* queries strictly respect ``data_cutoff`` and fail on overflow instead of
  silently trimming;
* ``lookback_sessions`` is capped before any data is read;
* cross-code history is stitched from PIT identity segments in stable date
  order, and incomplete mapping evidence or missing bars block the query;
* ``raw`` never applies adjustment factors while ``qfq`` / ``hfq`` require an
  active verified adjustment source.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from inspect import Parameter, signature
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.backtesting.data.errors import (
    DataContractError,
    freeze_json,
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    InvalidDataRequestError,
)
from app.backtesting.data.facts import Bar
from app.backtesting.data.requests import QualityStatus

from .contract import (
    MAX_LOOKBACK_SESSIONS,
    AdjustmentNotActiveError,
    DataCutoffViolationError,
    IdentityMappingMissingError,
    IncompleteHistoryError,
    InvalidProviderResultError,
    LookbackLimitExceededError,
)


class AdjustmentBasis(StrEnum):
    """Adjustment bases accepted by ``adjusted_series``."""

    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"


_BAR_VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _json_safe_detail(value: object) -> object:
    """Render diagnostic values before handing them to ``freeze_json``.

    Provider failures frequently carry typed coordinates such as UUIDs,
    dates, ``Decimal`` values, enums, and nested containers.  The stable
    error contract intentionally accepts JSON values only; converting these
    values here keeps every strategy/PIT error branch serializable without
    weakening that contract.
    """

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_detail(item) for item in value]
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _json_safe_detail(enum_value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    return repr(value)


def _json_safe_details(details: Mapping[str, object]) -> dict[str, object]:
    """Normalize one details mapping into JSON-safe values."""

    return {
        str(key): _json_safe_detail(value)
        for key, value in details.items()
    }


@dataclass(frozen=True, slots=True)
class BarDTO:
    """One immutable daily bar identified by the stable instrument id.

    ``values`` holds only the requested fields as finite ``Decimal`` values;
    binary floats, booleans, and non-finite numbers are rejected.  A missing
    source bar stays absent instead of being forward-filled.
    """

    instrument_id: UUID
    trade_date: date
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ValueError("trade_date must be a calendar date")
        if not isinstance(self.values, Mapping):
            raise ValueError("values must be a mapping")
        normalized: dict[str, Decimal] = {}
        for key, value in self.values.items():
            if not isinstance(key, str):
                raise ValueError("bar field names must be strings")
            normalized[key] = _finite_decimal(value, f"bar values[{key!r}]")
        object.__setattr__(self, "values", MappingProxyType(normalized))


def _finite_decimal(value: object, field_name: str) -> Decimal:
    """Normalize one numeric DTO value, rejecting float/bool/non-finite input."""

    from decimal import Decimal as _Decimal, InvalidOperation

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(
            f"{field_name} must be a decimal string or Decimal; "
            "binary floats are unsupported"
        )
    if isinstance(value, _Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")
        return value
    if not isinstance(value, (str, int)):
        raise ValueError(f"{field_name} must be a decimal string or Decimal")
    try:
        normalized = _Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


@dataclass(frozen=True, slots=True)
class AdjustedSeriesPointDTO:
    """One point of an adjustment series keyed by the stable instrument id."""

    instrument_id: UUID
    trade_date: date
    adj_factor: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ValueError("trade_date must be a calendar date")
        factor = _finite_decimal(self.adj_factor, "adj_factor")
        if factor <= 0:
            raise ValueError("adj_factor must be positive")
        object.__setattr__(self, "adj_factor", factor)


@dataclass(frozen=True, slots=True)
class InstrumentCandidateDTO:
    """Read-only candidate with its point-in-time display identity.

    ``instrument_id`` must be a real UUID (the stable identity strategies
    submit targets with); trading code, name, and display name are non-blank
    display-only strings.  The six fields below are the complete strategy
    surface.  Provider evidence, source codes, rule references, and raw fact
    identifiers stay on the engine-side qualification object and are never
    attached as an open-ended metadata mapping.
    """

    instrument_id: UUID
    trading_code: str
    name: str
    display_name: str
    asset_class: str
    exchange: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise ValueError("instrument_id must be a UUID")
        for field_name in (
            "trading_code",
            "name",
            "display_name",
            "asset_class",
            "exchange",
        ):
            value = getattr(self, field_name)
            # Exact str only: a str subclass could smuggle mutable attributes
            # from the provider into strategy-visible objects.
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")


def _candidate_projection_key(candidate: InstrumentCandidateDTO) -> tuple[str, ...]:
    """Return the stable tie-break key used for duplicate identities.

    Only the six strategy-visible fields participate.  This keeps duplicate
    handling deterministic without allowing engine evidence or source-code
    metadata to influence the strategy projection.
    """

    return (
        candidate.trading_code,
        candidate.name,
        candidate.display_name,
        candidate.asset_class,
        candidate.exchange,
    )


@runtime_checkable
class StrategyDataView(Protocol):
    """Raw read side implemented by real adapters and synthetic fixtures.

    Implementations receive already-validated windows (never past
    ``data_cutoff``, never over the lookback cap) and must return immutable
    DTOs keyed by the queried ``instrument_id``.
    """

    def bars(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> Sequence[BarDTO]: ...

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
        basis: AdjustmentBasis,
    ) -> Sequence[AdjustedSeriesPointDTO]: ...


@runtime_checkable
class UniverseQuery(Protocol):
    """Raw candidate-set read side bounded by permission and PIT eligibility."""

    def query(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        asset_classes: Iterable[str] | None = None,
    ) -> Sequence[InstrumentCandidateDTO]: ...


class AdjustmentPolicyGate:
    """Immutable gate backed by a named, versioned adjustment policy.

    Adjusted series may only be served after the native adjustment-factor
    source has passed real-source verification and been marked active.  The
    gate rejects all attribute writes so strategy code cannot flip it open.

    ``from_policy`` is the strict construction path used by production
    callers: an inactive or otherwise invalid policy can never activate the
    gate.  ``active_gate()`` remains as a compatibility constructor for the
    pre-policy strategy protocol tests; ETF adapters must pass an
    :class:`AdjustmentSeriesPolicy` instead of using that legacy hook.
    """

    POLICY_KEY = "tushare_adj_factor_native@1"

    __slots__ = (
        "_AdjustmentPolicyGate__active",
        "_AdjustmentPolicyGate__policy",
    )

    def __init__(self, active: bool, policy=None) -> None:
        if policy is not None:
            try:
                from app.backtesting.data.adjustment_policy import AdjustmentSeriesPolicy

                if not isinstance(policy, AdjustmentSeriesPolicy):
                    raise TypeError
                policy.validate_activation() if policy.is_active() else None
                active = policy.is_active()
            except (ImportError, TypeError) as exc:
                raise ValueError("policy must be an AdjustmentSeriesPolicy") from exc
        object.__setattr__(self, "_AdjustmentPolicyGate__policy", policy)
        object.__setattr__(self, "_AdjustmentPolicyGate__active", bool(active))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the adjustment policy gate is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the adjustment policy gate is read-only")

    @classmethod
    def from_policy_key(
        cls, policy_key: str | None, *, policy=None
    ) -> "AdjustmentPolicyGate":
        """Construct from a policy reference, validating a supplied policy.

        The no-policy form is retained for old strategy protocol callers that
        only had a key.  New data adapters should provide ``policy`` so a key
        alone cannot stand in for real-source evidence.
        """

        if policy is not None:
            if policy_key != cls.POLICY_KEY:
                return cls.inactive_gate()
            return cls.from_policy(policy)

        return cls(active=policy_key == cls.POLICY_KEY)

    @classmethod
    def from_policy(cls, policy) -> "AdjustmentPolicyGate":
        """Build a gate from an immutable, evidence-validated policy."""

        try:
            from app.backtesting.data.adjustment_policy import AdjustmentSeriesPolicy
        except ImportError as exc:  # pragma: no cover - import wiring failure
            raise ValueError("adjustment policy support is unavailable") from exc
        if not isinstance(policy, AdjustmentSeriesPolicy):
            raise ValueError("policy must be an AdjustmentSeriesPolicy")
        if policy.policy_key != cls.POLICY_KEY:
            return cls.inactive_gate()
        if policy.is_active():
            policy.validate_activation()
        return cls(active=policy.is_active(), policy=policy)

    @classmethod
    def active_gate(cls, policy=None) -> "AdjustmentPolicyGate":
        """Gate for a verified and active native adjustment policy.

        Passing a policy uses the strict path.  Calling without one is kept
        solely for compatibility with the original strategy protocol; it is
        not an ETF-adapter activation mechanism.
        """

        if policy is not None:
            return cls.from_policy(policy)

        return cls(active=True)

    @classmethod
    def inactive_gate(cls) -> "AdjustmentPolicyGate":
        """Gate used while the policy is unverified or inactive."""

        return cls(active=False)

    def is_active(self) -> bool:
        return self._AdjustmentPolicyGate__active

    @property
    def policy(self):
        """The immutable policy fact, when this gate was policy-backed."""

        return self._AdjustmentPolicyGate__policy


@dataclass(frozen=True, slots=True)
class _ResolvedWindow:
    start_date: date | None
    end_date: date | None
    lookback_sessions: int | None


class _ReadOnlyFacade:
    """Base that makes the strategy-facing facades fully read-only.

    Strategies must not be able to widen ``data_cutoff``, lift the lookback
    cap, or swap the underlying data provider, so every attribute write is
    rejected regardless of name mangling or slot availability.
    """

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be modified"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            "strategy query facades are read-only; query conditions cannot be deleted"
        )


class StrategyDataDTO(_ReadOnlyFacade):
    """Strategy-facing facade that enforces the shared query boundaries.

    The facade validates the window against ``data_cutoff`` and the lookback
    cap *before* delegating to the injected view, so oversized or future-facing
    requests can never reach a partial read.  Provider results are validated
    again on the way out (identity, cutoff, ordering, decimal types), so a
    broken provider cannot leak future rows or mutable floats to strategies.
    The injected view and cutoff are fixed at construction; the underlying
    provider objects are stored under name-mangled attributes so they are not
    part of the strategy-facing surface.  qfq/hfq requests are additionally
    gated on the adjustment policy being active.

    Note: this is the non-adversarial read-only boundary approved for this
    phase.  CPython cannot hide objects from deliberate in-process
    introspection; hostile-code isolation remains the subprocess boundary.
    """

    __slots__ = (
        "__view",
        "__data_cutoff",
        "__max_lookback_sessions",
        "__adjustment_gate",
        "__resolved_sessions",
        "__session_resolver",
        "__pit_reader",
        "__pit_source",
        "__universe",
    )

    def __init__(
        self,
        view: StrategyDataView,
        *,
        data_cutoff: datetime,
        max_lookback_sessions: int = MAX_LOOKBACK_SESSIONS,
        adjustment_gate: AdjustmentPolicyGate | None = None,
        resolved_sessions: Sequence[date] | object | None = None,
        session_resolver: object | None = None,
        pit_reader: object | None = None,
        pit_source: str | None = None,
        universe: "UniverseQuery | UniverseQueryDTO | None" = None,
    ) -> None:
        if (
            isinstance(max_lookback_sessions, bool)
            or not isinstance(max_lookback_sessions, int)
            or not 1 <= max_lookback_sessions <= MAX_LOOKBACK_SESSIONS
        ):
            # The protocol limit itself can never be raised by configuration;
            # callers may only lower it.
            raise ValueError(
                "max_lookback_sessions must be between 1 and "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        if not isinstance(data_cutoff, datetime) or data_cutoff.tzinfo is None or data_cutoff.utcoffset() is None:
            raise ValueError("data_cutoff must be timezone-aware")
        object.__setattr__(self, "_StrategyDataDTO__view", view)
        object.__setattr__(self, "_StrategyDataDTO__data_cutoff", data_cutoff)
        object.__setattr__(
            self, "_StrategyDataDTO__max_lookback_sessions", max_lookback_sessions
        )
        # Conservative default: without an explicit active gate, adjusted
        # series stay blocked so unverified factors can never be served.
        object.__setattr__(
            self,
            "_StrategyDataDTO__adjustment_gate",
            adjustment_gate or AdjustmentPolicyGate.inactive_gate(),
        )
        object.__setattr__(
            self,
            "_StrategyDataDTO__resolved_sessions",
            self._freeze_resolved_sessions(resolved_sessions),
        )
        object.__setattr__(
            self,
            "_StrategyDataDTO__session_resolver",
            session_resolver,
        )
        object.__setattr__(
            self,
            "_StrategyDataDTO__pit_reader",
            pit_reader,
        )
        if pit_source is not None and (
            not isinstance(pit_source, str) or not pit_source.strip()
        ):
            raise ValueError("pit_source must be non-blank text")
        object.__setattr__(
            self,
            "_StrategyDataDTO__pit_source",
            pit_source.strip() if isinstance(pit_source, str) else None,
        )
        # The candidate facade is optional for compatibility with strategy
        # providers that only expose historical bars.  When supplied it is
        # wrapped exactly once so repeated calls inside one decision receive
        # the same immutable candidate view and cannot replace its bound PIT
        # query.  We deliberately do not manufacture a candidate set here:
        # only an already-bound provider query may supply dynamic identities.
        if universe is not None and not isinstance(
            universe, (UniverseQuery, UniverseQueryDTO)
        ):
            raise ValueError("universe must implement UniverseQuery")
        if isinstance(universe, UniverseQueryDTO):
            universe_dto = universe
        elif universe is not None:
            universe_dto = UniverseQueryDTO(universe)
        else:
            universe_dto = None
        object.__setattr__(self, "_StrategyDataDTO__universe", universe_dto)

    @property
    def universe(self) -> "UniverseQueryDTO":
        """Return the immutable candidate query bound to this decision.

        A strategy data facade may be constructed without a universe by old
        callers.  In that case, fail explicitly instead of returning an empty
        set which could be mistaken for a valid dynamic result.
        """

        universe = self.__universe
        if universe is None:
            candidate_provider = getattr(self.__view, "universe", None)
            if not callable(candidate_provider):
                from app.backtesting.data.errors import UnsupportedCapabilityError

                raise UnsupportedCapabilityError(
                    "this strategy data view does not expose a PIT universe"
                )
            candidate_provider = candidate_provider()
            if isinstance(candidate_provider, UniverseQueryDTO):
                universe = candidate_provider
            elif isinstance(candidate_provider, UniverseQuery):
                universe = UniverseQueryDTO(candidate_provider)
            else:
                raise InvalidProviderResultError(
                    "strategy data view returned an invalid universe query"
                )
            # Cache the facade on this immutable object using the same
            # construction-time-only ``object.__setattr__`` convention as the
            # other private slots.  The underlying per-step view remains the
            # owner of PIT data; this only avoids replacing the wrapper on
            # repeated strategy access.
            object.__setattr__(self, "_StrategyDataDTO__universe", universe)
        return universe

    def bars(
        self,
        instrument_id: UUID | str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
    ) -> tuple[BarDTO, ...]:
        """Read raw bars ending no later than ``data_cutoff``."""

        resolved_id = self._require_uuid(instrument_id)
        window = self._resolve_window(
            instrument_id=resolved_id,
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        result = self._read_bars(
            resolved_id,
            window,
        )
        return self._validate_bars_result(resolved_id, result)

    def adjusted_series(
        self,
        instrument_id: UUID | str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        lookback_sessions: int | None = None,
        basis: str | AdjustmentBasis = AdjustmentBasis.RAW,
    ) -> tuple[AdjustedSeriesPointDTO, ...]:
        """Read an adjustment-bounded series for one stable instrument id."""

        try:
            resolved_basis = AdjustmentBasis(basis)
        except ValueError as exc:
            raise ValueError(f"unknown adjustment basis {basis!r}") from exc
        if resolved_basis is not AdjustmentBasis.RAW and not self.__adjustment_gate.is_active():
            raise AdjustmentNotActiveError(
                "qfq/hfq series require tushare_adj_factor_native@1 to be "
                "verified and active"
            )
        resolved_id = self._require_uuid(instrument_id)
        window = self._resolve_window(
            instrument_id=resolved_id,
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )
        result = tuple(
            self.__view.adjusted_series(
                resolved_id,
                start_date=window.start_date,
                end_date=window.end_date,
                lookback_sessions=window.lookback_sessions,
                basis=resolved_basis,
            )
        )
        return self._validate_series_result(resolved_id, result)

    # ------------------------------------------------------------------
    # Optional sessions-only PIT socket
    # ------------------------------------------------------------------

    def _read_bars(
        self, instrument_id: UUID, window: _ResolvedWindow
    ) -> tuple[BarDTO, ...]:
        """Read through the optional PIT adapter without leaking lookback.

        The old strategy provider remains the default path, including its
        unbounded-call behavior.  When a PIT reader is injected, the
        compatibility layer first resolves a concrete session tuple and
        then calls only sessions-based adapter methods.  In particular,
        ``lookback_sessions`` is never passed to a mapping provider or a Bar
        reader; the 512 check in ``_resolve_window`` has already completed.
        """

        pit = self.__pit_reader
        if pit is None or (
            window.start_date is None
            and window.end_date is None
            and window.lookback_sessions is None
        ):
            return tuple(
                self.__view.bars(
                    instrument_id,
                    start_date=window.start_date,
                    end_date=window.end_date,
                    lookback_sessions=window.lookback_sessions,
                )
            )
        resolved = self._resolve_pit_sessions(instrument_id, window)
        value = self._read_pit_history(instrument_id, resolved)
        return self._coerce_bars(
            value,
            instrument_id,
            expected_sessions=resolved.sessions,
        )

    def _resolve_pit_sessions(
        self, instrument_id: UUID, window: _ResolvedWindow
    ):
        """Resolve and validate calendar sessions before a PIT read.

        A resolver receives only the fixed cutoff.  Selection of a lookback
        window is done here, after the public cap and date checks, so a
        rejected request cannot reach calendar, mapping, or Bar providers.
        """

        from app.backtesting.data.pit_history import (
            ResolvedSessions,
            resolve_resolved_sessions,
        )

        source = self.__resolved_sessions
        if source is None:
            source = self.__session_resolver
        if source is None and self.__pit_reader is not None:
            # A complete PIT adapter may carry its already-resolved calendar
            # as an immutable property.  We deliberately inspect only
            # explicit names; no current catalogue/ETF snapshot is queried.
            for name in ("resolved_sessions", "session_dates", "sessions"):
                candidate = getattr(self.__pit_reader, name, None)
                if candidate is not None and not callable(candidate):
                    source = candidate
                    break
        if source is None:
            raise InvalidDataRequestError(
                "a PIT reader requires resolved_sessions or a session_resolver",
                details={
                    "instrument_id": str(instrument_id),
                    "source": None,
                    "session_date": None,
                    "expected": "resolved session sequence or resolver",
                    "actual": None,
                    "data_cutoff": self.__data_cutoff.isoformat(),
                    "fact_version": None,
                },
            )

        try:
            if isinstance(source, ResolvedSessions):
                raw_sessions = source.sessions
            elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                raw_sessions = source
            else:
                raw_sessions = self._invoke_session_resolver(source)
            sessions = tuple(
                self._session_date(value, instrument_id=instrument_id)
                for value in raw_sessions
            )
        except InvalidDataRequestError:
            raise
        except DataContractError:
            raise
        except Exception as exc:
            raise InvalidDataRequestError(
                "resolved session provider must return a sequence of calendar dates",
                details={
                    "instrument_id": str(instrument_id),
                    "source": None,
                    "session_date": None,
                    "expected": "sequence[date]",
                    "actual": type(raw_sessions).__name__
                    if "raw_sessions" in locals()
                    else type(source).__name__,
                    "data_cutoff": self.__data_cutoff.isoformat(),
                    "fact_version": None,
                    "reason": str(exc),
                },
            ) from exc
        try:
            validated = resolve_resolved_sessions(
                sessions, data_cutoff=self.__data_cutoff
            )
        except Exception as exc:
            # Keep the public strategy contract's stable legacy error type
            # for cutoff violations while preserving all other data errors.
            from app.backtesting.data.errors import DataCutoffExceededError

            if isinstance(exc, DataCutoffExceededError):
                future = next(
                    day for day in sessions if day > self.__data_cutoff.date()
                )
                raise DataCutoffViolationError(
                    future,
                    self.__data_cutoff.date(),
                    instrument_id=instrument_id,
                    source=self.__pit_source,
                    session_date=future,
                    expected=f"<= {self.__data_cutoff.date().isoformat()}",
                    actual=future,
                    data_cutoff=self.__data_cutoff,
                ) from exc
            raise
        available = validated.sessions
        if window.lookback_sessions is not None:
            if len(available) < window.lookback_sessions:
                from app.backtesting.data.errors import HistoryIncompleteError

                raise HistoryIncompleteError(
                    "the resolved trading sessions do not cover the requested "
                    "lookback window",
                    details={
                        "instrument_id": str(instrument_id),
                        "requested": window.lookback_sessions,
                        "available": len(available),
                        "data_cutoff": self.__data_cutoff.isoformat(),
                    },
                )
            selected = available[-window.lookback_sessions :]
        else:
            selected = tuple(
                day
                for day in available
                if (window.start_date is None or day >= window.start_date)
                and (window.end_date is None or day <= window.end_date)
            )
        if len(selected) > self.__max_lookback_sessions:
            # Explicit date ranges bypass the public lookback field, so apply
            # the same frozen run-level cap before any PIT provider read.
            raise LookbackLimitExceededError(
                len(selected),
                self.__max_lookback_sessions,
                instrument_id=instrument_id,
                source=self.__pit_source,
                session_date=selected[-1] if selected else None,
                expected=f"<= {self.__max_lookback_sessions} sessions",
                actual=len(selected),
                data_cutoff=self.__data_cutoff,
            )
        if not selected:
            # A PIT history query cannot prove an empty calendar range.  Do
            # not fall back to a current code or return a fabricated short
            # history.
            from app.backtesting.data.errors import HistoryIncompleteError

            raise HistoryIncompleteError(
                "the requested window contains no resolved trading sessions",
                details={
                    "instrument_id": str(instrument_id),
                    "data_cutoff": self.__data_cutoff.isoformat(),
                },
            )
        return ResolvedSessions(selected)

    def _session_date(self, value: object, *, instrument_id: UUID) -> date:
        """Extract a calendar date from a date or a calendar session point."""

        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        candidate = getattr(value, "session_date", None)
        if isinstance(candidate, date) and not isinstance(candidate, datetime):
            return candidate
        raise InvalidDataRequestError(
            "resolved sessions must contain calendar dates",
            details={
                "instrument_id": str(instrument_id),
                "source": self.__pit_source,
                "session_date": repr(value),
                "expected": "date",
                "actual": type(value).__name__,
                "data_cutoff": self.__data_cutoff.isoformat(),
                "fact_version": None,
            },
        )

    def _invoke_session_resolver(self, source: object) -> Sequence[object]:
        """Invoke the injected calendar resolver with the cutoff only."""

        resolver = source if callable(source) else None
        if resolver is None:
            for name in (
                "resolve_sessions",
                "resolve_resolved_sessions",
                "trading_sessions",
                "sessions",
            ):
                candidate = getattr(source, name, None)
                if callable(candidate):
                    resolver = candidate
                    break
        if resolver is None:
            raise ValueError(
                "session_resolver must be a sequence, callable, or expose "
                "resolve_sessions()"
            )
        try:
            params: Mapping[str, Parameter] = signature(resolver).parameters
        except (TypeError, ValueError):
            params = {}
        has_var_kwargs = any(
            parameter.kind is Parameter.VAR_KEYWORD for parameter in params.values()
        )
        kwargs: dict[str, object] = {}
        if "data_cutoff" in params or has_var_kwargs:
            kwargs["data_cutoff"] = self.__data_cutoff
        elif "cutoff" in params:
            kwargs["cutoff"] = self.__data_cutoff
        elif "end_at" in params:
            kwargs["end_at"] = self.__data_cutoff
        elif "end_date" in params:
            kwargs["end_date"] = self.__data_cutoff.date()
        elif "cutoff_date" in params:
            kwargs["cutoff_date"] = self.__data_cutoff.date()
        required_positional = [
            parameter
            for parameter in params.values()
            if parameter.default is Parameter.empty
            and parameter.kind
            in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if not kwargs and required_positional:
            if len(required_positional) != 1:
                raise ValueError(
                    "session_resolver must accept data_cutoff as its only required argument"
                )
            return resolver(self.__data_cutoff)
        return resolver(**kwargs)

    @staticmethod
    def _freeze_resolved_sessions(value: object) -> object:
        """Snapshot a supplied session collection at facade construction.

        ``resolved_sessions`` is a concrete calendar result, not a resolver;
        retaining a caller-owned list would let later mutation change a
        formal PIT request.  Resolver objects remain opaque dependencies and
        are intentionally invoked at query time.
        """

        if value is None or callable(value):
            return value
        if isinstance(value, (str, bytes)):
            return value
        try:
            return tuple(value)  # type: ignore[arg-type]
        except TypeError:
            # Keep malformed values available for the normal stable request
            # validation path instead of changing constructor timing.
            return value

    def _coerce_bars(
        self,
        value: object,
        instrument_id: UUID,
        *,
        expected_sessions: Sequence[date] | None = None,
    ) -> tuple[BarDTO, ...]:
        """Project generic PIT Bar facts into the public immutable DTO."""

        rows = getattr(value, "bars", value)
        if rows is None or isinstance(rows, (str, bytes)):
            raise InvalidProviderResultError(
                "PIT reader returned no bar sequence",
                details=self._pit_provider_error_details(
                    instrument_id=instrument_id,
                    sessions=expected_sessions or (),
                    source=None,
                    expected="sequence[Bar|BarDTO]",
                    actual=type(rows).__name__,
                    method="bars",
                ),
            )
        result: list[BarDTO] = []
        try:
            iterator = tuple(rows)
        except TypeError as exc:
            raise InvalidProviderResultError(
                "PIT reader returned a non-sequence bar result",
                details=self._pit_provider_error_details(
                    instrument_id=instrument_id,
                    sessions=expected_sessions or (),
                    source=None,
                    expected="sequence[Bar|BarDTO]",
                    actual=type(rows).__name__,
                    method="bars",
                ),
            ) from exc
        for row in iterator:
            if not isinstance(row, (BarDTO, Bar)):
                # Do not duck-type arbitrary provider objects into strategy
                # DTOs.  Only the two explicit immutable bar contracts are
                # accepted; this prevents mutable ORM/provider rows from
                # crossing the strategy boundary accidentally.
                raise InvalidProviderResultError(
                    "PIT reader returned an unsupported bar row type",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=expected_sessions or (),
                        source=None,
                        expected="Bar or BarDTO",
                        actual=type(row).__name__,
                        method="bars",
                    ),
                )
            if isinstance(row, BarDTO):
                if row.instrument_id != instrument_id:
                    raise InvalidProviderResultError(
                        "PIT reader returned a bar for another instrument_id",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected_sessions or (),
                            source=None,
                            expected=str(instrument_id),
                            actual=str(row.instrument_id),
                            method="bars",
                            session_date=row.trade_date,
                        ),
                    )
                result.append(row)
                continue
            if getattr(row, "instrument_id", None) != instrument_id:
                raise InvalidProviderResultError(
                    "PIT reader returned a bar for another instrument_id",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=expected_sessions or (),
                        source=None,
                        expected=str(instrument_id),
                        actual=str(getattr(row, "instrument_id", None)),
                        method="bars",
                    ),
                )
            # Generic PIT adapters may return internal ``Bar`` facts.  Their
            # evidence is not represented in ``BarDTO``, so enforce the
            # knowledge-time and quality gates before projecting away that
            # provenance.  ``known_at=None`` remains valid for explicitly
            # non-strict adapters; a future timestamp is never acceptable.
            if isinstance(row, Bar):
                trade_date = getattr(row, "trade_date", None)
                if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
                    raise InvalidProviderResultError(
                        "PIT reader returned an invalid bar date",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected_sessions or (),
                            source=None,
                            expected="date",
                            actual=repr(trade_date),
                            method="bars",
                        ),
                    )
                evidence = getattr(row, "evidence", None)
                quality_status = getattr(evidence, "quality_status", None)
                quality_value = getattr(quality_status, "value", repr(quality_status))
                if quality_status is not QualityStatus.COMPLETE:
                    raise InvalidProviderResultError(
                        "PIT reader returned a bar that is not complete quality",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected_sessions or (),
                            source=getattr(evidence, "source", None),
                            expected=QualityStatus.COMPLETE.value,
                            actual=quality_value,
                            method="bars",
                            session_date=trade_date,
                            quality_status=quality_value,
                        ),
                    )
                known_at = getattr(evidence, "known_at", None)
                if (
                    known_at is not None
                    and known_at > self.__data_cutoff
                ):
                    raise InvalidProviderResultError(
                        "PIT reader returned a bar learned after data_cutoff",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected_sessions or (),
                            source=getattr(evidence, "source", None),
                            expected=f"known_at <= {self.__data_cutoff.isoformat()}",
                            actual=_json_safe_detail(known_at),
                            method="bars",
                            session_date=trade_date,
                            known_at=known_at,
                        ),
                    )
            else:
                trade_date = getattr(row, "trade_date", None)
            if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
                raise InvalidProviderResultError(
                    "PIT reader returned an invalid bar date",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=expected_sessions or (),
                        source=None,
                        expected="date",
                        actual=repr(trade_date),
                        method="bars",
                    ),
                )
            try:
                values = {
                    field_name: getattr(row, field_name)
                    for field_name in _BAR_VALUE_FIELDS
                    if getattr(row, field_name, None) is not None
                }
                result.append(
                    BarDTO(
                        instrument_id=instrument_id,
                        trade_date=trade_date,
                        values=values,
                    )
                )
            except DataContractError:
                raise
            except (AttributeError, TypeError, ValueError) as exc:
                raise InvalidProviderResultError(
                    "PIT reader returned an invalid bar payload",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=expected_sessions or (),
                        source=None,
                        expected="Bar fields with finite decimal values",
                        actual=str(exc),
                        method="bars",
                    ),
                ) from exc
        # Every PIT adapter socket receives a non-empty resolved session
        # tuple, so an empty result is a missing-history violation just like
        # any other short result.  Coverage is checked before the generic
        # outbound DTO validator so no partial window can escape.
        if expected_sessions is not None:
            expected = tuple(expected_sessions)
            expected_set = set(expected)
            seen: set[date] = set()
            for row in result:
                if row.trade_date not in expected_set:
                    raise HistoryBarsIncompleteError(
                        "PIT reader returned a bar outside the resolved sessions",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected,
                            source=None,
                            session_date=row.trade_date,
                            expected=expected,
                            actual=row.trade_date,
                            method="bars",
                        ),
                    )
                if row.trade_date in seen:
                    raise HistoryBarsDuplicateError(
                        "PIT reader returned more than one bar for a resolved session",
                        details=self._pit_provider_error_details(
                            instrument_id=instrument_id,
                            sessions=expected,
                            source=None,
                            session_date=row.trade_date,
                            expected="one bar per session",
                            actual="duplicate",
                            method="bars",
                        ),
                    )
                seen.add(row.trade_date)
            missing = tuple(day for day in expected if day not in seen)
            if missing:
                raise HistoryBarsIncompleteError(
                    "PIT reader did not return a bar for every resolved session",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=expected,
                        source=None,
                        session_date=missing[0],
                        expected=expected,
                        actual=tuple(seen),
                        method="bars",
                        missing_session_count=len(missing),
                    ),
                )
        return tuple(result)

    def _read_pit_history(self, instrument_id: UUID, sessions: object) -> object:
        """Drive a sessions-only adapter using signature-aware dispatch."""

        pit = self.__pit_reader
        assert pit is not None
        source = self.__pit_source
        if source is None:
            provider_source = getattr(pit, "source", None)
            if isinstance(provider_source, str) and provider_source.strip():
                source = provider_source.strip()

        def invoke(method: Callable[..., object], **values: object) -> object:
            """Translate adapter shape failures into the stable provider error.

            A sessions-only PIT socket is a provider boundary.  Missing
            required parameters, unsupported positional-only signatures, and
            a provider method rejecting the negotiated call must therefore
            never escape as a bare ``ValueError``/``TypeError``.  Preserve
            already-classified data-contract errors, while adding the fixed
            query coordinates to ordinary adapter failures.
            """

            try:
                return self._invoke_pit_method(method, **values)
            except DataContractError as exc:
                self._enrich_pit_provider_error(
                    exc,
                    instrument_id=instrument_id,
                    sessions=sessions,
                    source=source,
                    method=getattr(method, "__name__", type(method).__name__),
                )
                raise
            except (AttributeError, TypeError, ValueError) as exc:
                method_name = getattr(method, "__name__", type(method).__name__)
                raise InvalidProviderResultError(
                    f"PIT reader method {method_name!r} is incompatible with "
                    "the sessions-only contract",
                    details=self._pit_provider_error_details(
                        instrument_id=instrument_id,
                        sessions=sessions,
                        source=source,
                        expected="sessions-only PIT adapter signature",
                        actual=str(exc),
                        method=method_name,
                    ),
                ) from exc

        # Full adapters expose resolve(...) followed by bars(...).  The
        # method calls intentionally contain no lookback argument.
        resolver = getattr(pit, "resolve", None)
        bars = getattr(pit, "bars", None)
        if callable(resolver) and (callable(bars) or callable(getattr(pit, "read_history", None))):
            resolution = invoke(
                resolver,
                instrument_id=instrument_id,
                source=source,
                sessions=sessions,
                resolved_sessions=sessions,
                data_cutoff=self.__data_cutoff,
            )
            try:
                parameters = signature(bars).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "resolution" in parameters:
                return invoke(
                    bars,
                    instrument_id=instrument_id,
                    resolution=resolution,
                )
            reader = getattr(pit, "read_history", None)
            if callable(reader):
                return invoke(reader, resolution=resolution)
            raise InvalidProviderResultError(
                "PIT reader bars() must accept resolution or expose read_history()",
                details=self._pit_provider_error_details(
                    instrument_id=instrument_id,
                    sessions=sessions,
                    source=source,
                    expected="bars(resolution) or read_history(resolution)",
                    actual="incompatible bars() signature",
                    method="bars",
                ),
            )
        session_reader = getattr(pit, "bars_for_sessions", None)
        if callable(session_reader):
            return invoke(
                session_reader,
                instrument_id=instrument_id,
                sessions=sessions,
                resolved_sessions=sessions,
                data_cutoff=self.__data_cutoff,
            )
        reader = getattr(pit, "read_segmented_history", None)
        if callable(reader):
            return invoke(
                reader,
                instrument_id=instrument_id,
                source=(
                    self.__pit_source
                    if self.__pit_source is not None
                    else getattr(pit, "source", None)
                ),
                resolved_sessions=sessions,
                data_cutoff=self.__data_cutoff,
            )
        reader = getattr(pit, "read_history", None)
        if callable(reader):
            return invoke(
                reader,
                instrument_id=instrument_id,
                sessions=sessions,
                resolved_sessions=sessions,
                data_cutoff=self.__data_cutoff,
            )
        if callable(pit):
            return invoke(
                pit,
                instrument_id=instrument_id,
                resolved_sessions=sessions,
                data_cutoff=self.__data_cutoff,
            )
        raise InvalidProviderResultError(
            "pit_reader must expose resolve()+bars(), read_segmented_history(), "
            "or be callable",
            details=self._pit_provider_error_details(
                instrument_id=instrument_id,
                sessions=sessions,
                source=source,
                expected=(
                    "resolve()+bars(), read_segmented_history(), "
                    "bars_for_sessions(), read_history(), or callable"
                ),
                actual=type(pit).__name__,
                method=None,
            ),
        )

    def _pit_provider_error_details(
        self,
        *,
        instrument_id: UUID,
        sessions: object,
        source: object,
        expected: object,
        actual: object,
        method: object,
        session_date: object = None,
        **extra: object,
    ) -> dict[str, object]:
        """Build JSON-safe context for one PIT adapter contract failure."""

        resolved = getattr(sessions, "sessions", sessions)
        try:
            first_session = next(iter(resolved))
        except (StopIteration, TypeError):
            first_session = None
        if session_date is not None:
            session_value = session_date
        elif isinstance(first_session, date) and not isinstance(first_session, datetime):
            session_value = first_session
        else:
            session_value = first_session
        details = {
            "instrument_id": instrument_id,
            "source": source,
            "source_code": None,
            "session_date": session_value,
            "expected": expected,
            "actual": actual,
            "data_cutoff": self.__data_cutoff,
            "fact_version": None,
            "method": method,
        }
        details.update(extra)
        return _json_safe_details(details)

    def _enrich_pit_provider_error(
        self,
        error: DataContractError,
        *,
        instrument_id: UUID,
        sessions: object,
        source: object,
        method: object,
    ) -> None:
        """Complete provider-raised contract errors with query coordinates.

        Providers may already classify a failure with a specific data error
        (for example ``HistoryIncompleteError``).  Keep that stable code and
        message, but ensure its persisted details still identify this PIT
        request.  Existing provider fields win; only missing coordinates are
        supplied by the adapter boundary.
        """

        defaults = self._pit_provider_error_details(
            instrument_id=instrument_id,
            sessions=sessions,
            source=source,
            expected="valid sessions-only PIT provider result",
            actual=type(error).__name__,
            method=method,
        )
        details = dict(getattr(error, "details", {}) or {})
        for key, value in defaults.items():
            if key not in details or details[key] is None:
                details[key] = value
        try:
            frozen = freeze_json(details, "details")
        except ValueError:
            # A deliberately corrupted provider exception may have replaced
            # its details after construction.  Normalize it defensively so
            # even that failure remains a stable data-contract error.
            frozen = freeze_json(_json_safe_details(details), "details")
        if isinstance(frozen, Mapping):
            error.details = frozen

    @staticmethod
    def _invoke_pit_method(method: Callable[..., object], **values: object) -> object:
        """Pass only parameters explicitly declared by an adapter method."""

        try:
            params: Mapping[str, Parameter] = signature(method).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_kwargs = any(
            parameter.kind is Parameter.VAR_KEYWORD for parameter in params.values()
        )
        kwargs = {
            key: value
            for key, value in values.items()
            if key in params or accepts_kwargs
        }
        # ``sessions`` is intentionally converted to a plain tuple at this
        # boundary; the receiving PIT APIs treat it as an already-resolved
        # input and have no opportunity to interpret a lookback count.
        if "sessions" in kwargs and hasattr(kwargs["sessions"], "sessions"):
            kwargs["sessions"] = kwargs["sessions"].sessions
        if "resolved_sessions" in kwargs and hasattr(
            kwargs["resolved_sessions"], "sessions"
        ):
            kwargs["resolved_sessions"] = kwargs["resolved_sessions"].sessions
        if "price_basis" in kwargs and "basis" in params:
            kwargs.pop("price_basis", None)
        if "basis" in kwargs and "price_basis" in params:
            kwargs.pop("basis", None)
        required = [
            parameter
            for parameter in params.values()
            if parameter.default is Parameter.empty
            and parameter.kind
            in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
            and parameter.name not in kwargs
        ]
        if required:
            raise ValueError(
                f"PIT adapter method {getattr(method, '__name__', method)!r} "
                "has unsupported required parameters"
            )
        return method(**kwargs)

    def _validate_bars_result(
        self, requested_id: UUID, result: tuple[BarDTO, ...]
    ) -> tuple[BarDTO, ...]:
        """Reject provider rows that violate the identity or cutoff contract."""

        cutoff_date = self.__data_cutoff.date()
        previous: date | None = None
        for bar in result:
            if not isinstance(bar, BarDTO):
                raise InvalidProviderResultError(
                    "provider returned a non-BarDTO row; only immutable bar "
                    "DTOs may reach strategy code",
                    details=self._pit_provider_error_details(
                        instrument_id=requested_id,
                        sessions=(),
                        source=None,
                        expected="BarDTO",
                        actual=type(bar).__name__,
                        method="bars",
                    ),
                )
            if bar.instrument_id != requested_id:
                raise InvalidProviderResultError(
                    f"provider returned instrument_id {bar.instrument_id} "
                    f"for a query on {requested_id}",
                    details=self._pit_provider_error_details(
                        instrument_id=requested_id,
                        sessions=(),
                        source=None,
                        session_date=bar.trade_date,
                        expected=requested_id,
                        actual=bar.instrument_id,
                        method="bars",
                    ),
                )
            if bar.trade_date > cutoff_date:
                raise InvalidProviderResultError(
                    f"provider returned bar {bar.trade_date} later than "
                    f"data_cutoff {cutoff_date}",
                    details=self._pit_provider_error_details(
                        instrument_id=requested_id,
                        sessions=(),
                        source=None,
                        session_date=bar.trade_date,
                        expected=f"trade_date <= {cutoff_date.isoformat()}",
                        actual=bar.trade_date,
                        method="bars",
                    ),
                )
            if previous is not None and bar.trade_date <= previous:
                raise InvalidProviderResultError(
                    "provider returned bars out of ascending date order",
                    details=self._pit_provider_error_details(
                        instrument_id=requested_id,
                        sessions=(),
                        source=None,
                        session_date=bar.trade_date,
                        expected=f"> {previous.isoformat()}",
                        actual=bar.trade_date,
                        method="bars",
                        previous_trade_date=previous,
                    ),
                )
            previous = bar.trade_date
        return result

    def _validate_series_result(
        self, requested_id: UUID, result: tuple[AdjustedSeriesPointDTO, ...]
    ) -> tuple[AdjustedSeriesPointDTO, ...]:
        """Apply the same outbound checks to adjustment series."""

        cutoff_date = self.__data_cutoff.date()
        previous: date | None = None
        for point in result:
            if not isinstance(point, AdjustedSeriesPointDTO):
                raise InvalidProviderResultError(
                    "provider returned a non-AdjustedSeriesPointDTO row; only "
                    "immutable series DTOs may reach strategy code"
                )
            if point.instrument_id != requested_id:
                raise InvalidProviderResultError(
                    f"provider returned instrument_id {point.instrument_id} "
                    f"for a query on {requested_id}"
                )
            if point.trade_date > cutoff_date:
                raise InvalidProviderResultError(
                    f"provider returned series point {point.trade_date} later "
                    f"than data_cutoff {cutoff_date}"
                )
            if previous is not None and point.trade_date <= previous:
                raise InvalidProviderResultError(
                    "provider returned series points out of ascending date order"
                )
            previous = point.trade_date
        return result

    def _require_uuid(self, instrument_id: UUID | str) -> UUID:
        """Accept UUID or canonical string form, nothing else."""

        if isinstance(instrument_id, UUID):
            return instrument_id
        if isinstance(instrument_id, str):
            try:
                return UUID(instrument_id)
            except ValueError as exc:
                raise ValueError(
                    f"instrument_id {instrument_id!r} is not a valid UUID"
                ) from exc
        raise ValueError("instrument_id must be a UUID or UUID string")

    def _resolve_window(
        self,
        *,
        instrument_id: UUID | None = None,
        start_date: date | None,
        end_date: date | None,
        lookback_sessions: int | None,
    ) -> _ResolvedWindow:
        """Validate the requested window before any data access happens."""

        for field_name, value in (
            ("start_date", start_date),
            ("end_date", end_date),
        ):
            if value is not None and (
                not isinstance(value, date) or isinstance(value, datetime)
            ):
                raise ValueError(f"{field_name} must be a calendar date")
        if lookback_sessions is not None:
            if (
                isinstance(lookback_sessions, bool)
                or not isinstance(lookback_sessions, int)
                or lookback_sessions <= 0
            ):
                raise ValueError("lookback_sessions must be a positive integer")
            if lookback_sessions > self.__max_lookback_sessions:
                # Fail before touching any data source.
                raise LookbackLimitExceededError(
                    lookback_sessions,
                    self.__max_lookback_sessions,
                    instrument_id=instrument_id,
                    source=self.__pit_source,
                    expected=f"<= {self.__max_lookback_sessions} sessions",
                    actual=lookback_sessions,
                    data_cutoff=self.__data_cutoff,
                )
        cutoff_date = self.__data_cutoff.date()
        if end_date is not None and end_date > cutoff_date:
            raise DataCutoffViolationError(
                end_date,
                cutoff_date,
                instrument_id=instrument_id,
                source=self.__pit_source,
                session_date=end_date,
                expected=f"<= {cutoff_date.isoformat()}",
                actual=end_date,
                data_cutoff=self.__data_cutoff,
            )
        if start_date is not None and start_date > cutoff_date:
            raise DataCutoffViolationError(
                start_date,
                cutoff_date,
                instrument_id=instrument_id,
                source=self.__pit_source,
                session_date=start_date,
                expected=f"<= {cutoff_date.isoformat()}",
                actual=start_date,
                data_cutoff=self.__data_cutoff,
            )
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        if lookback_sessions is not None and (start_date is not None or end_date is not None):
            # Lookback windows are resolved by the trading calendar against
            # the cutoff alone.  Mixing them with an explicit date range
            # would let different providers interpret the combination
            # differently, so the combination is a caller error, always.
            raise ValueError(
                "pass either lookback_sessions or an explicit date range, "
                "never both"
            )
        return _ResolvedWindow(
            start_date=start_date,
            end_date=end_date,
            lookback_sessions=lookback_sessions,
        )


class UniverseQueryDTO(_ReadOnlyFacade):
    """Strategy-facing candidate-set facade returning validated results.

    Provider output is re-checked on the way out (type, duplicate identity)
    and returned in stable ``instrument_id`` order as an immutable tuple, so
    strategies can never receive mutable or duplicated candidates.
    """

    __slots__ = ("__query", "__cache", "__queried")

    def __init__(self, query: UniverseQuery) -> None:
        if not isinstance(query, UniverseQuery):
            raise ValueError("query must implement UniverseQuery")
        object.__setattr__(self, "_UniverseQueryDTO__query", query)
        # The cache belongs to one DTO instance, which is one decision-step
        # view.  A subsequent step must receive a new DTO with a new bound
        # effective date/data cutoff; keeping the cache here prevents repeated
        # calls in a single step from observing mutable provider state.
        object.__setattr__(self, "_UniverseQueryDTO__cache", {})
        object.__setattr__(self, "_UniverseQueryDTO__queried", False)

    def query(
        self,
        *,
        exchanges: Iterable[str] | None = None,
        asset_classes: Iterable[str] | None = None,
    ) -> tuple[InstrumentCandidateDTO, ...]:
        """Return PIT-eligible candidates as an immutable sorted tuple."""

        def _labels(value: Iterable[str] | None, field_name: str):
            if value is None:
                return None
            if isinstance(value, (str, bytes)):
                raise InvalidDataRequestError(
                    f"{field_name} must be an iterable of strings"
                )
            try:
                labels = tuple(sorted(set(value)))
            except TypeError as exc:
                raise InvalidDataRequestError(
                    f"{field_name} must be an iterable of strings"
                ) from exc
            if any(type(item) is not str or not item.strip() for item in labels):
                raise InvalidDataRequestError(
                    f"{field_name} entries must be non-blank strings"
                )
            return tuple(item.strip() for item in labels)

        normalized_exchanges = _labels(exchanges, "exchanges")
        normalized_assets = _labels(asset_classes, "asset_classes")
        cache_key = (normalized_exchanges, normalized_assets)
        cache = self.__cache
        if cache_key in cache:
            object.__setattr__(self, "_UniverseQueryDTO__queried", True)
            return cache[cache_key]

        result = self._UniverseQueryDTO__query.query(
            exchanges=normalized_exchanges, asset_classes=normalized_assets
        )
        candidates = tuple(result)
        by_id: dict[UUID, InstrumentCandidateDTO] = {}
        for candidate in candidates:
            if not isinstance(candidate, InstrumentCandidateDTO):
                raise InvalidProviderResultError(
                    "universe provider returned a non-candidate row"
                )
            # A provider may return the same stable identity more than once
            # while source-code/display versions are being reconciled.  Pick
            # one deterministic projection by its complete six-field value;
            # never let physical input order decide which code reaches a
            # strategy.  The engine-side source remains responsible for
            # retaining the richer evidence for the discarded row.
            current = by_id.get(candidate.instrument_id)
            if current is None or _candidate_projection_key(candidate) < _candidate_projection_key(current):
                by_id[candidate.instrument_id] = candidate
        normalized = tuple(
            by_id[instrument_id]
            for instrument_id in sorted(by_id, key=str)
        )
        cache[cache_key] = normalized
        object.__setattr__(self, "_UniverseQueryDTO__queried", True)
        return normalized

    @property
    def has_queried(self) -> bool:
        """Whether this decision-step facade has served a candidate query."""

        return self.__queried

    @property
    def candidate_ids(self) -> frozenset[UUID]:
        """Stable identities returned by this facade's successful queries."""

        return frozenset(
            candidate.instrument_id
            for rows in self.__cache.values()
            for candidate in rows
        )

    @property
    def effective_date(self):
        """Read-only effective date when the provider exposes one."""

        return getattr(self.__query, "effective_date", None)

    @property
    def boundary(self):
        """Read-only QueryBoundary when the provider exposes one."""

        return getattr(self.__query, "boundary", None)

    @property
    def scope_mode(self):
        """Read-only fixed/dynamic/hybrid mode when declared by the provider."""

        value = getattr(self.__query, "scope_mode", None)
        if value is not None:
            return value
        # Runtime's immutable step snapshot may wrap the original bound
        # query in a private engine object.  Inspect only the structural
        # source/mode marker; do not invoke a query or enumerate candidates.
        source = getattr(self.__query, "source", None)
        if source is None:
            source = getattr(self.__query, "_source", None)
        return getattr(source, "scope_mode", None)

    @property
    def scope_snapshot_hash(self):
        """Read-only admission hash for audit and runtime checks."""

        return getattr(
            self.__query,
            "universe_scope_snapshot_hash",
            getattr(self.__query, "scope_snapshot_hash", None),
        )

    def for_step(self, **coordinates: object) -> "UniverseQueryDTO":
        """Create a fresh DTO when the underlying provider supports step binding.

        A bound query is the unit of PIT isolation.  This forwarding helper is
        intentionally optional for legacy static providers; those providers
        simply report that no step-aware operation exists instead of allowing
        callers to mutate the existing DTO.
        """

        binder = getattr(self.__query, "for_step", None)
        if not callable(binder):
            raise AttributeError("this universe provider is not step-bindable")
        bound = binder(**coordinates)
        if not isinstance(bound, UniverseQuery):
            raise InvalidProviderResultError(
                "universe provider returned an invalid step-bound query"
            )
        return UniverseQueryDTO(bound)


@dataclass(frozen=True, slots=True)
class PitSegment:
    """One point-in-time identity segment of a stable instrument.

    A stable ``instrument_id`` may have traded under several historical codes.
    Each segment records which trading code was valid for which inclusive date
    range, providing the mapping evidence required for cross-code reads.
    """

    trading_code: str
    effective_from: date
    effective_to: date

    def __post_init__(self) -> None:
        if self.effective_from > self.effective_to:
            raise ValueError("segment effective_from cannot be after effective_to")

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to

    def clamp_window(
        self, start_date: date | None, end_date: date | None
    ) -> tuple[date, date]:
        """Intersect the segment validity with the requested window."""

        lower = self.effective_from
        upper = self.effective_to
        if start_date is not None and start_date > lower:
            lower = start_date
        if end_date is not None and end_date < upper:
            upper = end_date
        return lower, upper


SegmentReader = Callable[[str, date, date], Sequence[BarDTO]]


def stitch_segmented_history(
    segments: Sequence[PitSegment],
    *,
    sessions: Sequence[date],
    read_segment: SegmentReader,
) -> tuple[BarDTO, ...]:
    """Read per-segment windows and stitch them back into one stable series.

    ``sessions`` lists the tradable sessions of the requested window in
    ascending order without duplicates (resolved by the trading calendar);
    unsorted or duplicated input is rejected instead of silently normalized.
    Every requested session must be covered by exactly one identity segment:
    coverage gaps and overlapping segments both raise
    :class:`IdentityMappingMissingError` instead of silently returning a
    shorter window or duplicated bars.  Each segment reader must return a bar
    for every session inside its clamped range; missing bars raise
    :class:`IncompleteHistoryError` and are never forward-filled.  The result
    is sorted by trade date regardless of segment order.
    """

    ordered_sessions = list(sessions)
    if any(
        later <= earlier
        for earlier, later in zip(ordered_sessions, ordered_sessions[1:])
    ):
        raise ValueError("sessions must contain distinct ascending dates")
    if not ordered_sessions:
        return ()

    # Interval semantics are unified to half-open [from, to) everywhere:
    # the public closed PitSegment bounds are converted explicitly here so
    # exactly one interval convention enters the query path.  Sessions are
    # discrete dates, so the conversion is lossless.
    def _covers(segment: PitSegment, day: date) -> bool:
        return segment.effective_from <= day < segment.effective_to + timedelta(
            days=1
        )

    # Coverage is verified before any provider read so overlapping segments
    # can never be queried twice, and gaps never trigger partial reads.
    coverage: dict[date, list[PitSegment]] = {}
    for day in ordered_sessions:
        covering = [segment for segment in segments if _covers(segment, day)]
        if not covering:
            raise IdentityMappingMissingError(
                f"no PIT identity mapping covers session {day}"
            )
        if len(covering) > 1:
            raise IdentityMappingMissingError(
                f"PIT identity mappings overlap on session {day}: "
                f"{sorted(segment.trading_code for segment in covering)}"
            )
        coverage[day] = covering

    collected: list[BarDTO] = []
    for segment in segments:
        lower, upper = segment.clamp_window(ordered_sessions[0], ordered_sessions[-1])
        if lower > upper:
            continue
        expected = [day for day in ordered_sessions if _covers(segment, day)]
        if not expected:
            continue
        rows = tuple(read_segment(segment.trading_code, expected[0], expected[-1]))
        # Every segment row must be a real immutable BarDTO, mirroring the
        # provider validation on the direct facade path.
        for row in rows:
            if not isinstance(row, BarDTO):
                raise InvalidProviderResultError(
                    f"segment reader for {segment.trading_code} returned a "
                    "non-BarDTO row; only immutable bar DTOs are accepted"
                )
        # Each segment must return exactly the requested sessions: no
        # duplicates, no out-of-range or non-session dates, and no short
        # windows.
        expected_set = set(expected)
        seen_dates: set[date] = set()
        for row in rows:
            if row.trade_date not in expected_set:
                raise IncompleteHistoryError(
                    f"segment {segment.trading_code} returned a bar outside "
                    f"the requested sessions on {row.trade_date}"
                )
            if row.trade_date in seen_dates:
                raise IncompleteHistoryError(
                    f"segment {segment.trading_code} returned duplicate bars "
                    f"for session {row.trade_date}"
                )
            seen_dates.add(row.trade_date)
        if len(rows) != len(expected):
            missing = [day for day in expected if day not in seen_dates]
            raise IncompleteHistoryError(
                f"history bars are missing for {len(missing)} sessions, "
                f"first missing {missing[0]}"
            )
        collected.extend(rows)
    return tuple(sorted(collected, key=lambda bar: bar.trade_date))


__all__ = [
    "AdjustmentBasis",
    "AdjustmentPolicyGate",
    "AdjustedSeriesPointDTO",
    "BarDTO",
    "InstrumentCandidateDTO",
    "PitSegment",
    "StrategyDataDTO",
    "StrategyDataView",
    "UniverseQuery",
    "UniverseQueryDTO",
    "stitch_segmented_history",
]
