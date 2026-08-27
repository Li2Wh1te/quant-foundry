"""Immutable analysis input snapshots for the metric analyzer subsystem.

Every object in this module is a validated, immutable fact that has already
been determined by the accounting, valuation, or data layer.  The analyzer
engine only accumulates these facts; it never re-derives prices, fills,
fees, or FX conversions from orders or raw market data.

Frozen contracts enforced here (task package 06):

* :class:`InitialEquitySnapshot` -- the E0 evidence frozen before the first
  formal session opens; ``valuation_as_of`` must be strictly earlier than
  ``market_open_at`` and ``equity_e0`` must be strictly positive;
* :class:`EquityObservation` -- one end-of-day equity fact with a
  data-source-declared PIT ``data_cutoff_at`` (never inferred by the VALUE
  phase);
* :class:`AppliedFillFact` / :class:`FillObservation` -- accounting-applied
  fill facts whose ``gross_traded_notional`` is accepted as-is; the
  analyzer only sums it;
* :class:`PitRateSnapshot` -- the complete pre-fetched PIT daily risk-free
  rate series for Sharpe B, frozen once at run admission with source
  versions, deterministic ``missing_ranges``, and a snapshot hash.

Evidence hashes and the input-evidence signature are computed over a
canonical JSON rendering (sorted keys, decimals as strings) so identical
inputs always produce identical signatures across full-run and
chunked-run executions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Any
from uuid import UUID

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import DomainValidationError

__all__ = [
    "ANALYSIS_EVIDENCE_HASH_ALGORITHM",
    "AppliedFillFact",
    "CanonicalEvidenceValue",
    "EquityObservation",
    "FillObservation",
    "FormalSessionTimeline",
    "InitialEquitySnapshot",
    "InitialHolding",
    "PIT_RATE_CUTOFF_BOUNDARY",
    "PitRateSnapshot",
    "canonical_evidence_json",
    "compute_input_evidence_signature",
    "compute_formal_timeline_hash",
    "compute_rate_snapshot_hash",
    "evidence_digest",
]


#: Hash algorithm used for every analysis evidence digest.
ANALYSIS_EVIDENCE_HASH_ALGORITHM = "sha256"
PIT_RATE_VALUE_UNIT = "decimal_fraction"
PIT_RATE_CONVENTION = "simple_daily_rate"
PIT_RATE_EFFECTIVE_AT = "session_date"
PIT_RATE_SESSION_MAPPING = "exact_formal_session_date"
PIT_RATE_CUTOFF_SEMANTICS = "data_cutoff_at_not_after_session_open"
PIT_RATE_CUTOFF_BOUNDARY = "data_cutoff_at_not_after_session_open"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a numeric input to an exact finite ``Decimal``."""

    if isinstance(value, bool) or isinstance(value, float) or not isinstance(
        value, (Decimal, int, str)
    ):
        raise DomainValidationError(
            f"{field_name} must be Decimal, int, or str; binary floats are "
            "rejected everywhere in analysis inputs"
        )
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise DomainValidationError(
            f"{field_name} is not a valid decimal: {value!r}"
        ) from exc
    if not normalized.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return normalized


@contextmanager
def _exact_context():
    """Return the fixed analysis arithmetic context without mutating globals."""

    # Construct a fresh context from Decimal's specification defaults.  Using
    # ``localcontext()`` without an argument would inherit process-global
    # exponent limits, flags and traps, making identical evidence dependent on
    # unrelated code that previously changed ``getcontext()``.
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)) as context:
        yield context


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value


def _plain_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a calendar date")
    return value


def _currency(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return value.strip().upper()


def _uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID")
    return value


def _sequence_number(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{field_name} must be non-negative")
    return value


def _ordered_sequence(value: Any, field_name: str) -> Sequence[Any]:
    """Require an explicitly ordered sequence, never a set or mapping."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise DomainValidationError(f"{field_name} must be an ordered sequence")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(
            f"{field_name} must be non-blank text when provided"
        )
    return value.strip()


def _frozen_mapping(
    value: Mapping[str, Any] | None, field_name: str
) -> Mapping[str, Any]:
    """Validate and deep-freeze one canonical JSON-evidence mapping.

    Mapping keys are normalized to their canonical JSON strings only after
    collision detection.  Values use the same closed type vocabulary as
    :func:`canonical_evidence_json`, so malformed evidence fails at DTO
    construction rather than much later while computing a signature.
    """

    def freeze_key(key: Any, path: str) -> str:
        if isinstance(key, bool) or isinstance(key, float):
            raise DomainValidationError(
                f"{path} contains an unsupported mapping key type "
                f"{type(key).__name__}"
            )
        if isinstance(key, str):
            return key
        if isinstance(key, int):
            return str(key)
        if isinstance(key, date) and not isinstance(key, datetime):
            return key.isoformat()
        if isinstance(key, UUID):
            return str(key)
        raise DomainValidationError(
            f"{path} contains an unsupported mapping key type "
            f"{type(key).__name__}"
        )

    def freeze(item: Any, path: str) -> Any:
        if isinstance(item, Mapping):
            frozen: dict[str, Any] = {}
            for key, inner in item.items():
                normalized_key = freeze_key(key, path)
                if normalized_key in frozen:
                    raise DomainValidationError(
                        f"{path} contains mapping keys that collide after "
                        f"canonical normalization: {normalized_key!r}"
                    )
                frozen[normalized_key] = freeze(
                    inner, f"{path}[{normalized_key!r}]"
                )
            return MappingProxyType(frozen)
        if isinstance(item, (list, tuple)):
            return tuple(
                freeze(entry, f"{path}[{index}]")
                for index, entry in enumerate(item)
            )
        if item is None or isinstance(item, (bool, str, UUID)):
            return item
        if isinstance(item, float):
            raise DomainValidationError(
                f"{path} must not contain binary floats"
            )
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise DomainValidationError(f"{path} must contain finite decimals")
            return item
        if isinstance(item, datetime):
            return _aware(item, path)
        if isinstance(item, date):
            return item
        if isinstance(item, int):
            return item
        raise DomainValidationError(
            f"{path} contains unsupported evidence type "
            f"{type(item).__name__}"
        )

    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field_name} must be a mapping")
    return freeze(value, field_name)


# ---------------------------------------------------------------------------
# Canonical evidence serialization
# ---------------------------------------------------------------------------


#: Type alias documenting what canonical evidence payloads may contain.
CanonicalEvidenceValue = Any


def canonical_evidence_json(value: Any) -> str:
    """Render one evidence payload as deterministic canonical JSON.

    Mappings are sorted by key, sequences keep their declared order,
    Decimals render as plain strings, datetimes/dates render as ISO-8601,
    UUIDs render in their canonical hyphenated form, and binary floats are
    rejected so no precision can silently disappear.
    """

    def normalize(item: Any) -> Any:
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, float):
            raise DomainValidationError(
                "canonical evidence payloads must not contain binary floats"
            )
        if isinstance(item, Decimal):
            # Decimal formatting must not consult the process context.  Strip
            # insignificant trailing zeroes and canonicalize both +0 and -0
            # so equivalent NUMERIC values hash identically.
            if not item.is_finite():
                raise DomainValidationError(
                    "canonical evidence decimals must be finite"
                )
            if item == 0:
                return "0"
            rendered = format(item, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise DomainValidationError(
                    "canonical evidence datetimes must be timezone-aware"
                )
            utc_value = item.astimezone(timezone.utc)
            rendered = utc_value.isoformat(timespec="microseconds")
            return rendered[:-6] + "Z"
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, int):
            return item
        if isinstance(item, str):
            return item
        if isinstance(item, Mapping):
            # Reuse the DTO boundary validator here because callers may hash
            # ad-hoc payloads that have not already passed _frozen_mapping().
            frozen = _frozen_mapping(item, "canonical evidence payload")
            return {
                key: normalize(inner)
                for key, inner in sorted(frozen.items())
            }
        if isinstance(item, (list, tuple)):
            return [normalize(entry) for entry in item]
        raise DomainValidationError(
            f"canonical evidence payload contains unsupported type "
            f"{type(item).__name__}"
        )

    return json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def evidence_digest(payload: CanonicalEvidenceValue) -> str:
    """Hash one canonical evidence payload with the fixed algorithm."""

    rendered = canonical_evidence_json(payload).encode("utf-8")
    return f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:{hashlib.sha256(rendered).hexdigest()}"


def compute_formal_timeline_hash(sessions: Sequence[date]) -> str:
    """Hash the exact ordered formal session sequence used by every analyzer."""

    ordered = _ordered_sequence(sessions, "formal sessions")
    normalized = tuple(_plain_date(day, "formal session") for day in ordered)
    if not normalized:
        raise DomainValidationError(
            "formal session timeline must contain at least one session"
        )
    for index in range(1, len(normalized)):
        if normalized[index] <= normalized[index - 1]:
            raise DomainValidationError(
                "formal sessions must be unique and strictly increasing"
            )
    return evidence_digest(
        {
            "contract": "formal_timeline_v1",
            "sessions": normalized,
        }
    )


@dataclass(frozen=True, slots=True)
class FormalSessionTimeline:
    """Immutable ordered formal-session contract shared by all analyzers.

    The session sequence and its digest are one admission input.  Keeping
    them together prevents a caller from passing a list from one calendar
    and a hash from another component, or from recording the timeline only
    after analysis has already run.
    """

    sessions: Sequence[date]
    timeline_hash: str | None = None

    def __post_init__(self) -> None:
        ordered = _ordered_sequence(self.sessions, "formal timeline sessions")
        normalized = tuple(
            _plain_date(day, "formal timeline session") for day in ordered
        )
        if not normalized:
            raise DomainValidationError(
                "formal session timeline must contain at least one session"
            )
        for index in range(1, len(normalized)):
            if normalized[index] <= normalized[index - 1]:
                raise DomainValidationError(
                    "formal session timeline must be unique and strictly increasing"
                )
        expected_hash = compute_formal_timeline_hash(normalized)
        if self.timeline_hash is not None and self.timeline_hash != expected_hash:
            raise DomainValidationError(
                "formal session timeline hash does not match its sessions"
            )
        object.__setattr__(self, "sessions", normalized)
        object.__setattr__(self, "timeline_hash", expected_hash)

    @property
    def first_session(self) -> date:
        return self.sessions[0]

    @property
    def last_session(self) -> date:
        return self.sessions[-1]

    def as_payload(self) -> dict[str, Any]:
        """Return the stable JSON-shaped representation used in summaries."""

        return {
            "contract": "formal_timeline_v1",
            "sessions": tuple(day.isoformat() for day in self.sessions),
            "timeline_hash": self.timeline_hash,
        }


def compute_rate_snapshot_hash(snapshot: "PitRateSnapshot") -> str:
    """Compute the frozen snapshot hash of a PIT rate snapshot."""

    payload = {
        "algorithm": ANALYSIS_EVIDENCE_HASH_ALGORITHM,
        "source_key": snapshot.source_key,
        "source_version": snapshot.source_version,
        "rate_unit": snapshot.rate_unit,
        "rate_convention": snapshot.rate_convention,
        "effective_at": snapshot.effective_at,
        "session_mapping": snapshot.session_mapping,
        "data_cutoff_semantics": snapshot.data_cutoff_semantics,
        "cutoff_boundary": snapshot.cutoff_boundary,
        "query_parameters": dict(snapshot.query_parameters),
        "coverage_start": (
            snapshot.coverage_start.isoformat()
            if snapshot.coverage_start is not None
            else None
        ),
        "coverage_end": (
            snapshot.coverage_end.isoformat()
            if snapshot.coverage_end is not None
            else None
        ),
        "rates": [
            [day.isoformat(), rate]
            for day, rate in sorted(snapshot.rates.items())
        ],
        "missing_ranges": [
            {
                "start_session": start.isoformat(),
                "end_session": end.isoformat(),
            }
            for start, end in snapshot.missing_ranges
        ],
        # Per-fact provenance participates: identical rates with different
        # knowledge times must never share a snapshot hash.
        "fact_evidence": {
            day_key: dict(provenance)
            for day_key, provenance in sorted(
                snapshot.fact_evidence.items()
            )
        },
    }
    # ``query_parameters`` and provenance are deep-frozen mappings and may
    # contain Decimal/date/UUID values.  The shared canonical serializer
    # handles those types deterministically; raw ``json.dumps`` would fail
    # on nested query values or silently depend on caller container types.
    rendered = canonical_evidence_json(payload).encode("utf-8")
    return (
        f"{ANALYSIS_EVIDENCE_HASH_ALGORITHM}:"
        f"{hashlib.sha256(rendered).hexdigest()}"
    )


def compute_input_evidence_signature(
    *,
    initial_equity_snapshot: "InitialEquitySnapshot",
    equity_observations: Sequence["EquityObservation"] = (),
    fill_facts: Sequence["FillObservation"] = (),
    rate_snapshot_hash: str | None = None,
) -> str:
    """Compute the run's input-evidence signature.

    The signature describes only the actual input evidence (E0 price
    snapshot, equity observations, applied fill facts, PIT rate snapshot
    hash); logical formula identity lives exclusively in the formula
    signature so the two classes of signature are never conflated.
    """

    payload = {
        "kind": "input_evidence_v1",
        "initial_equity": initial_equity_snapshot.evidence_payload(),
        "formal_timeline": (
            initial_equity_snapshot.formal_timeline.as_payload()
            if initial_equity_snapshot.formal_timeline is not None
            else None
        ),
        "equity_observations": [
            observation.evidence_payload()
            for observation in equity_observations
        ],
        "fill_facts": [
            fact.evidence_payload()
            for fact in sorted(
                fill_facts, key=lambda item: str(item.fact.fill_id)
            )
        ],
        "rate_snapshot_hash": rate_snapshot_hash,
    }
    return evidence_digest(payload)


# ---------------------------------------------------------------------------
# Initial equity (E0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InitialHolding:
    """One initial position with its strict-PIT raw close mark.

    ``mark_evidence`` carries the provenance of the selected source fact
    (session, knowledge/observation times, source identity and revision)
    so the input-evidence signature is derived from actual evidence rather
    than a caller-declared hash.
    """

    instrument_id: UUID
    quantity: Decimal | int | str
    currency: str
    close_price: Decimal | int | str
    mark_evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _uuid(self.instrument_id, "instrument_id")
        )
        quantity = _decimal(self.quantity, "quantity")
        if quantity <= 0:
            raise DomainValidationError("quantity must be positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))
        price = _decimal(self.close_price, "close_price")
        if price <= 0:
            # A missing or non-positive mark is MISSING_INITIAL_MARK at
            # admission; the DTO itself refuses to carry an invalid price.
            raise DomainValidationError(
                "close_price must be strictly positive; a missing initial "
                f"mark blocks the run with reason MISSING_INITIAL_MARK"
            )
        object.__setattr__(self, "close_price", price)
        if self.mark_evidence is not None:
            if not isinstance(self.mark_evidence, Mapping):
                raise DomainValidationError(
                    "mark_evidence must be a mapping when provided"
                )
            object.__setattr__(
                self,
                "mark_evidence",
                _frozen_mapping(self.mark_evidence, "mark_evidence"),
            )


@dataclass(frozen=True, slots=True)
class InitialEquitySnapshot:
    """E0 evidence frozen before the first formal session opens.

    ``valuation_as_of`` is the last official close strictly earlier than
    ``market_open_at``; ``data_cutoff_at`` is the explicit PIT cutoff the
    valuation data source returned for this query.  Neither timestamp may
    be guessed by the runtime.
    """

    run_id: str
    session_date: date
    market_open_at: datetime
    valuation_as_of: datetime
    data_cutoff_at: datetime
    reporting_currency: str
    cash: Decimal | int | str
    holdings: Sequence[InitialHolding] = ()
    market_value: Decimal | int | str | None = None
    equity_e0: Decimal | int | str | None = None
    source_versions: Mapping[str, Any] | None = None
    evidence_hash: str | None = None
    # Populated by the admission coordinator after comparing E0 with the
    # authoritative starting portfolio.  These fields bind value to
    # composition, not merely to the total equity denominator.
    portfolio_snapshot_id: str | None = None
    portfolio_snapshot_hash: str | None = None
    formal_timeline: FormalSessionTimeline | None = None
    # Compatibility aliases retained for callers that still pass primitive
    # session/hash fields; they are normalized from ``formal_timeline``.
    formal_sessions: Sequence[date] = ()
    timeline_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "session_date", _plain_date(self.session_date, "session_date"))
        object.__setattr__(self, "market_open_at", _aware(self.market_open_at, "market_open_at"))
        object.__setattr__(
            self, "valuation_as_of", _aware(self.valuation_as_of, "valuation_as_of")
        )
        object.__setattr__(
            self, "data_cutoff_at", _aware(self.data_cutoff_at, "data_cutoff_at")
        )
        # Strict precedence: a valuation at exactly the open instant would
        # already require knowledge that only exists after the open.
        if self.valuation_as_of >= self.market_open_at:
            raise DomainValidationError(
                "valuation_as_of must be strictly earlier than market_open_at"
            )
        # Look-ahead gate: the declared data cutoff must also sit strictly
        # before the open; a post-open cutoff would admit facts the run
        # could not have known at valuation time.
        if self.data_cutoff_at >= self.market_open_at:
            raise DomainValidationError(
                "data_cutoff_at must be strictly earlier than market_open_at; "
                "a cutoff at or after the open introduces look-ahead bias"
            )
        object.__setattr__(
            self,
            "reporting_currency",
            _currency(self.reporting_currency, "reporting_currency"),
        )
        cash = _decimal(self.cash, "cash")
        object.__setattr__(self, "cash", cash)

        holdings = tuple(_ordered_sequence(self.holdings, "holdings"))
        seen_instruments: set[UUID] = set()
        computed_market_value = Decimal("0")
        with _exact_context() as context:
            for holding in holdings:
                if holding.instrument_id in seen_instruments:
                    raise DomainValidationError(
                        f"initial holdings declare instrument "
                        f"{holding.instrument_id} twice"
                    )
                seen_instruments.add(holding.instrument_id)
                if holding.currency != self.reporting_currency:
                    raise DomainValidationError(
                        f"initial holding {holding.instrument_id} carries currency "
                        f"{holding.currency} but the reporting currency is "
                        f"{self.reporting_currency}; no FX conversion exists"
                    )
                computed_market_value += holding.quantity * holding.close_price
        object.__setattr__(self, "holdings", holdings)

        market_value = (
            computed_market_value
            if self.market_value is None
            else _decimal(self.market_value, "market_value")
        )
        if market_value != computed_market_value:
            raise DomainValidationError(
                f"declared market_value {market_value} does not equal the sum "
                f"of quantity x close_price over the holdings "
                f"({computed_market_value})"
            )
        object.__setattr__(self, "market_value", market_value)

        with _exact_context() as context:
            computed_equity = cash + market_value
        equity = computed_equity if self.equity_e0 is None else _decimal(
            self.equity_e0, "equity_e0"
        )
        if equity != computed_equity:
            raise DomainValidationError(
                "equity_e0 must equal cash plus market_value"
            )
        if equity <= 0:
            # NON_POSITIVE_INITIAL_EQUITY at admission: the DTO refuses to
            # represent a non-positive E0 so a blocked run can never look
            # like an admitted one.
            raise DomainValidationError(
                "equity_e0 must be strictly positive; a non-positive E0 "
                "blocks run creation with reason NON_POSITIVE_INITIAL_EQUITY"
            )
        object.__setattr__(self, "equity_e0", equity)
        object.__setattr__(
            self, "source_versions", _frozen_mapping(self.source_versions, "source_versions")
        )
        portfolio_id = _optional_text(
            self.portfolio_snapshot_id, "portfolio_snapshot_id"
        )
        portfolio_hash = _optional_text(
            self.portfolio_snapshot_hash, "portfolio_snapshot_hash"
        )
        if (portfolio_id is None) != (portfolio_hash is None):
            raise DomainValidationError(
                "portfolio_snapshot_id and portfolio_snapshot_hash must be "
                "provided together"
            )
        object.__setattr__(self, "portfolio_snapshot_id", portfolio_id)
        object.__setattr__(self, "portfolio_snapshot_hash", portfolio_hash)
        supplied_timeline = self.formal_timeline
        if supplied_timeline is not None and not isinstance(
            supplied_timeline, FormalSessionTimeline
        ):
            raise DomainValidationError(
                "formal_timeline must be a FormalSessionTimeline"
            )
        ordered_sessions = _ordered_sequence(self.formal_sessions, "formal_sessions")
        supplied_sessions = tuple(
            _plain_date(day, "formal_sessions entry") for day in ordered_sessions
        )
        if supplied_timeline is None and supplied_sessions:
            supplied_timeline = FormalSessionTimeline(
                supplied_sessions, timeline_hash=self.timeline_hash
            )
        elif supplied_timeline is not None:
            if supplied_sessions and supplied_sessions != supplied_timeline.sessions:
                raise DomainValidationError(
                    "formal_sessions does not match formal_timeline.sessions"
                )
            if self.timeline_hash is not None and self.timeline_hash != supplied_timeline.timeline_hash:
                raise DomainValidationError(
                    "timeline_hash does not match formal_timeline.timeline_hash"
                )
        if supplied_timeline is not None:
            formal_sessions = supplied_timeline.sessions
            if formal_sessions[0] != self.session_date:
                raise DomainValidationError(
                    "formal_sessions must start at the E0 session_date"
                )
            object.__setattr__(self, "formal_timeline", supplied_timeline)
            object.__setattr__(self, "formal_sessions", formal_sessions)
            object.__setattr__(self, "timeline_hash", supplied_timeline.timeline_hash)
        else:
            if self.timeline_hash is not None:
                raise DomainValidationError(
                    "timeline_hash requires a non-empty formal_sessions sequence"
                )
            object.__setattr__(self, "formal_timeline", None)
            object.__setattr__(self, "formal_sessions", ())
            object.__setattr__(self, "timeline_hash", None)
        # The evidence hash is always recomputed from this DTO's full
        # payload (holdings include per-mark provenance); a caller-supplied
        # value that does not match is rejected instead of trusted.
        # The digest is derived from the source facts, never from its own
        # caller-supplied representation.  Including ``evidence_hash`` in
        # the payload would make the value self-referential and would allow
        # two equivalent source snapshots to acquire different signatures.
        expected_hash = evidence_digest(self._evidence_payload_without_hash())
        if self.evidence_hash is None:
            object.__setattr__(self, "evidence_hash", expected_hash)
        else:
            supplied_hash = _optional_text(self.evidence_hash, "evidence_hash")
            if supplied_hash != expected_hash:
                raise DomainValidationError(
                    "evidence_hash does not match the recomputed input-evidence "
                    f"digest {expected_hash}"
                )
            object.__setattr__(self, "evidence_hash", supplied_hash)

    def evidence_payload(self) -> dict[str, Any]:
        """Canonical payload feeding the input-evidence signature."""

        payload = self._evidence_payload_without_hash()
        # Keep the derived digest visible to callers that persist the DTO,
        # but it is deliberately not an input to the digest computation.
        payload["evidence_hash"] = self.evidence_hash
        return payload

    def _evidence_payload_without_hash(self) -> dict[str, Any]:
        """Return the complete source-evidence payload before its digest."""

        return {
            "run_id": self.run_id,
            "session_date": self.session_date,
            "market_open_at": self.market_open_at,
            "valuation_as_of": self.valuation_as_of,
            "data_cutoff_at": self.data_cutoff_at,
            "reporting_currency": self.reporting_currency,
            "cash": self.cash,
            "portfolio_snapshot_id": self.portfolio_snapshot_id,
            "portfolio_snapshot_hash": self.portfolio_snapshot_hash,
            "formal_timeline": (
                self.formal_timeline.as_payload()
                if self.formal_timeline is not None
                else None
            ),
            "formal_sessions": self.formal_sessions,
            "timeline_hash": self.timeline_hash,
            "source_versions": dict(self.source_versions),
            "holdings": [
                {
                    "instrument_id": holding.instrument_id,
                    "quantity": holding.quantity,
                    "currency": holding.currency,
                    "close_price": holding.close_price,
                    "mark_evidence": (
                        dict(holding.mark_evidence)
                        if holding.mark_evidence is not None
                        else None
                    ),
                }
                for holding in self.holdings
            ],
            # Source identity/revision/schema are carried by each selected
            # holding's ``mark_evidence``.  The declared source-version map
            # also participates so revisions cannot silently collide.
        }


# ---------------------------------------------------------------------------
# Equity observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquityObservation:
    """One end-of-day equity fact handed to the analyzer engine.

    ``data_cutoff_at`` comes exclusively from the valuation data source.
    Blocked observations carry no equity but must state their reason; they
    invalidate the equity series instead of being silently skipped.
    """

    run_id: str
    step_sequence: int
    session_date: date
    as_of: datetime
    valuation_status: str
    data_cutoff_at: datetime
    reporting_currency: str
    cash: Decimal | int | str
    market_value: Decimal | int | str | None = None
    equity: Decimal | int | str | None = None
    cumulative_fees: Decimal | int | str = Decimal("0")
    valuation_reason: str | None = None
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(
            self, "step_sequence", _sequence_number(self.step_sequence, "step_sequence")
        )
        object.__setattr__(self, "session_date", _plain_date(self.session_date, "session_date"))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        if self.valuation_status not in ("valid", "blocked"):
            raise DomainValidationError(
                "valuation_status must be 'valid' or 'blocked'"
            )
        object.__setattr__(
            self, "data_cutoff_at", _aware(self.data_cutoff_at, "data_cutoff_at")
        )
        # Look-ahead gate: the cutoff declares what the source knew at
        # valuation time and must never sit after the observation instant.
        if self.data_cutoff_at > self.as_of:
            raise DomainValidationError(
                "data_cutoff_at must not be later than as_of"
            )
        object.__setattr__(
            self,
            "reporting_currency",
            _currency(self.reporting_currency, "reporting_currency"),
        )
        object.__setattr__(self, "cash", _decimal(self.cash, "cash"))
        fees = _decimal(self.cumulative_fees, "cumulative_fees")
        if fees < 0:
            raise DomainValidationError("cumulative_fees must be non-negative")
        object.__setattr__(self, "cumulative_fees", fees)
        normalized_reason = _optional_text(self.valuation_reason, "valuation_reason")
        if self.valuation_status == "blocked":
            if self.equity is not None:
                raise DomainValidationError(
                    "blocked observations must not carry an equity value"
                )
            if normalized_reason is None:
                raise DomainValidationError(
                    "blocked observations require a valuation_reason"
                )
        else:
            if self.equity is None:
                raise DomainValidationError(
                    "valid observations must carry an equity value"
                )
            equity = _decimal(self.equity, "equity")
            object.__setattr__(self, "equity", equity)
            if self.market_value is None:
                with _exact_context():
                    derived_market_value = equity - self.cash
                object.__setattr__(self, "market_value", derived_market_value)
            else:
                market_value = _decimal(self.market_value, "market_value")
                with _exact_context():
                    recomputed_equity = market_value + self.cash
                if recomputed_equity != equity:
                    raise DomainValidationError(
                        "market_value plus cash must equal equity on valid "
                        "observations"
                    )
        object.__setattr__(self, "market_value", (
            None
            if self.market_value is None
            else _decimal(self.market_value, "market_value")
        ))
        object.__setattr__(self, "valuation_reason", normalized_reason)
        object.__setattr__(
            self, "evidence_hash", _optional_text(self.evidence_hash, "evidence_hash")
        )

    @property
    def is_valid(self) -> bool:
        return self.valuation_status == "valid"

    def evidence_payload(self) -> dict[str, Any]:
        """Canonical payload feeding the input-evidence signature."""

        return {
            "run_id": self.run_id,
            "step_sequence": self.step_sequence,
            "session_date": self.session_date,
            "as_of": self.as_of,
            "valuation_status": self.valuation_status,
            "data_cutoff_at": self.data_cutoff_at,
            "cash": self.cash,
            "market_value": self.market_value,
            "equity": self.equity,
            "cumulative_fees": self.cumulative_fees,
            "valuation_reason": self.valuation_reason,
            "reporting_currency": self.reporting_currency,
            "evidence_hash": self.evidence_hash,
        }


# ---------------------------------------------------------------------------
# Applied fill facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppliedFillFact:
    """One accounting-applied fill fact consumed by the analyzer.

    ``gross_traded_notional`` is the confirmed accounting result of
    ``fill_price x fill_quantity x contract_multiplier``; the analyzer
    validates the identity once and then only aggregates.  Fees come from
    the accounting layer unchanged.
    """

    fill_id: UUID
    run_id: str
    session_date: date
    timestamp: datetime
    instrument_id: UUID
    side: OrderSide | str
    fill_price: Decimal | int | str
    fill_quantity: Decimal | int | str
    contract_multiplier: Decimal | int | str
    currency: str
    reporting_currency: str
    fees: Decimal | int | str
    gross_traded_notional: Decimal | int | str
    source_versions: Mapping[str, Any] | None = None
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _uuid(self.fill_id, "fill_id"))
        object.__setattr__(
            self, "instrument_id", _uuid(self.instrument_id, "instrument_id")
        )
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise DomainValidationError("run_id must be non-blank text")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "session_date", _plain_date(self.session_date, "session_date"))
        object.__setattr__(self, "timestamp", _aware(self.timestamp, "timestamp"))
        try:
            side = OrderSide(getattr(self.side, "value", self.side))
        except ValueError as exc:
            raise DomainValidationError("side must be buy or sell") from exc
        object.__setattr__(self, "side", side)
        price = _decimal(self.fill_price, "fill_price")
        if price <= 0:
            raise DomainValidationError("fill_price must be strictly positive")
        object.__setattr__(self, "fill_price", price)
        quantity = _decimal(self.fill_quantity, "fill_quantity")
        if quantity <= 0:
            raise DomainValidationError("fill_quantity must be strictly positive")
        object.__setattr__(self, "fill_quantity", quantity)
        multiplier = _decimal(self.contract_multiplier, "contract_multiplier")
        if multiplier <= 0:
            raise DomainValidationError(
                "contract_multiplier must be strictly positive"
            )
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))
        object.__setattr__(
            self, "reporting_currency", _currency(self.reporting_currency, "reporting_currency")
        )
        if self.currency != self.reporting_currency:
            raise DomainValidationError(
                f"fill currency {self.currency} differs from the reporting "
                f"currency {self.reporting_currency}; no FX conversion exists"
            )
        fees = _decimal(self.fees, "fees")
        if fees < 0:
            raise DomainValidationError("fees must be non-negative")
        object.__setattr__(self, "fees", fees)
        # The accounting layer owns this confirmed amount.  The analyzer
        # accepts and aggregates it as evidence; it must never derive a
        # replacement value from price, quantity, or multiplier here.
        declared_notional = _decimal(
            self.gross_traded_notional, "gross_traded_notional"
        )
        if declared_notional < 0:
            raise DomainValidationError(
                "gross_traded_notional must be non-negative"
            )
        object.__setattr__(self, "gross_traded_notional", declared_notional)
        object.__setattr__(
            self, "source_versions", _frozen_mapping(self.source_versions, "source_versions")
        )
        object.__setattr__(
            self, "evidence_hash", _optional_text(self.evidence_hash, "evidence_hash")
        )

    def evidence_payload(self) -> dict[str, Any]:
        """Canonical payload feeding the input-evidence signature."""

        return {
            "fill_id": self.fill_id,
            "run_id": self.run_id,
            "session_date": self.session_date,
            "timestamp": self.timestamp,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "contract_multiplier": self.contract_multiplier,
            "currency": self.currency,
            "reporting_currency": self.reporting_currency,
            "fees": self.fees,
            "gross_traded_notional": self.gross_traded_notional,
            "source_versions": dict(self.source_versions),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class FillObservation:
    """Read-only analyzer view of one applied fill fact.

    ``data_cutoff_at`` is optional by contract: only carried (and then
    required to be timezone-aware) when the fact's source explicitly
    declares a PIT cutoff.  A missing cutoff never rejects the fact.
    """

    fact: AppliedFillFact
    data_cutoff_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact, AppliedFillFact):
            raise DomainValidationError(
                "fact must be an AppliedFillFact"
            )
        if self.data_cutoff_at is not None:
            object.__setattr__(
                self,
                "data_cutoff_at",
                _aware(self.data_cutoff_at, "data_cutoff_at"),
            )

    def __getattr__(self, name: str) -> Any:
        # Delegate attribute access to the wrapped fact so callers read
        # fill fields directly from the observation.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(object.__getattribute__(self, "fact"), name)
        except AttributeError:
            raise AttributeError(name) from None

    @property
    def content_identity(self) -> tuple[Any, ...]:
        """Content tuple used to detect same-id-different-content inputs."""

        return (
            self.fact.fill_id,
            self.fact.instrument_id,
            self.fact.side.value,
            self.fact.fill_price,
            self.fact.fill_quantity,
            self.fact.contract_multiplier,
            self.fact.fees,
            self.fact.gross_traded_notional,
            self.fact.session_date,
            self.fact.timestamp,
            self.fact.reporting_currency,
            self.data_cutoff_at,
            tuple(sorted(self.fact.source_versions.items())),
            self.fact.evidence_hash,
        )

    def evidence_payload(self) -> dict[str, Any]:
        """Payload including the optional source-declared PIT cutoff."""

        payload = dict(self.fact.evidence_payload())
        payload["data_cutoff_at"] = self.data_cutoff_at
        return payload


# ---------------------------------------------------------------------------
# PIT risk-free rate snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PitRateSnapshot:
    """Complete frozen PIT daily risk-free rate series for Sharpe B.

    The series covers the whole formal backtest window and is fetched once
    at run admission.  Missing sessions become deterministic contiguous
    ``missing_ranges``; nothing is ever forward-filled or zero-filled.  Each
    retained fact also carries its source ``data_cutoff_at`` and that cutoff
    is required not later than the corresponding formal session open.
    """

    rates: Mapping[date, Decimal | int | str]
    source_key: str
    source_version: int
    rate_unit: str = PIT_RATE_VALUE_UNIT
    rate_convention: str = PIT_RATE_CONVENTION
    effective_at: str = PIT_RATE_EFFECTIVE_AT
    session_mapping: str = PIT_RATE_SESSION_MAPPING
    data_cutoff_semantics: str = PIT_RATE_CUTOFF_SEMANTICS
    cutoff_boundary: str = PIT_RATE_CUTOFF_BOUNDARY
    query_parameters: Mapping[str, Any] | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None
    expected_sessions: Sequence[date] = ()
    snapshot_hash: str | None = None
    missing_ranges: Sequence[tuple[date, date]] | None = None
    # Per-session provenance of the selected source facts (known_at,
    # observed_at, source revision, quality); part of the snapshot hash so
    # identical rates with different PIT evidence produce different hashes.
    fact_evidence: Mapping[date, Mapping[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise DomainValidationError("source_key must be non-blank text")
        object.__setattr__(self, "source_key", self.source_key.strip())
        if (
            isinstance(self.source_version, bool)
            or not isinstance(self.source_version, int)
            or self.source_version <= 0
        ):
            raise DomainValidationError("source_version must be a positive integer")
        frozen_contract = {
            "rate_unit": PIT_RATE_VALUE_UNIT,
            "rate_convention": PIT_RATE_CONVENTION,
            "effective_at": PIT_RATE_EFFECTIVE_AT,
            "session_mapping": PIT_RATE_SESSION_MAPPING,
            "data_cutoff_semantics": PIT_RATE_CUTOFF_SEMANTICS,
            "cutoff_boundary": PIT_RATE_CUTOFF_BOUNDARY,
        }
        for field_name, expected_value in frozen_contract.items():
            if getattr(self, field_name) != expected_value:
                raise DomainValidationError(
                    f"{field_name} must be the frozen Sharpe B value "
                    f"{expected_value!r}"
                )
        normalized_rates: dict[date, Decimal] = {}
        for day, raw_rate in dict(self.rates).items():
            day = _plain_date(day, "rate date key")
            rate = _decimal(raw_rate, f"rate[{day.isoformat()}]")
            normalized_rates[day] = rate
        object.__setattr__(
            self, "rates", MappingProxyType(dict(sorted(normalized_rates.items())))
        )
        object.__setattr__(
            self,
            "query_parameters",
            _frozen_mapping(self.query_parameters, "query_parameters"),
        )
        if self.coverage_start is not None:
            object.__setattr__(
                self,
                "coverage_start",
                _plain_date(self.coverage_start, "coverage_start"),
            )
        if self.coverage_end is not None:
            object.__setattr__(
                self, "coverage_end", _plain_date(self.coverage_end, "coverage_end")
            )
        ordered_expected = _ordered_sequence(
            self.expected_sessions, "expected_sessions"
        )
        expected = tuple(
            _plain_date(day, "expected_sessions entry")
            for day in ordered_expected
        )
        for index in range(1, len(expected)):
            if expected[index] <= expected[index - 1]:
                raise DomainValidationError(
                    "expected_sessions must be unique and strictly increasing"
                )
        object.__setattr__(self, "expected_sessions", expected)
        if expected:
            if self.coverage_start != expected[0] or self.coverage_end != expected[-1]:
                raise DomainValidationError(
                    "coverage_start/coverage_end must exactly bound the formal "
                    "expected_sessions window"
                )
            unexpected_rates = set(normalized_rates) - set(expected)
            if unexpected_rates:
                raise DomainValidationError(
                    "rates contain sessions outside expected_sessions"
                )
        frozen_fact_evidence = _frozen_mapping(self.fact_evidence, "fact_evidence")
        normalized_evidence: dict[str, Any] = {}
        for day_key, provenance in frozen_fact_evidence.items():
            normalized_evidence[str(day_key)] = provenance
        object.__setattr__(
            self,
            "fact_evidence",
            MappingProxyType(normalized_evidence)
            if normalized_evidence
            else MappingProxyType({}),
        )
        # ``missing_ranges`` is a derived coverage fact.  Recompute it from
        # the frozen expected/available dates and reject any caller-declared
        # value that disagrees, rather than silently accepting or ignoring a
        # malformed audit payload.
        derived_missing_ranges = tuple(
            deterministic_missing_ranges(expected, set(normalized_rates))
        )
        if self.missing_ranges is not None:
            supplied_ranges: list[tuple[date, date]] = []
            for item in self.missing_ranges:
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    raise DomainValidationError(
                        "missing_ranges entries must be (start_date, end_date)"
                    )
                start = _plain_date(item[0], "missing range start")
                end = _plain_date(item[1], "missing range end")
                if end < start:
                    raise DomainValidationError(
                        "missing range end must not precede its start"
                    )
                supplied_ranges.append((start, end))
            if tuple(supplied_ranges) != derived_missing_ranges:
                raise DomainValidationError(
                    "missing_ranges does not match the derived PIT coverage"
                )
        object.__setattr__(self, "missing_ranges", derived_missing_ranges)
        if self.snapshot_hash is None:
            object.__setattr__(
                self, "snapshot_hash", compute_rate_snapshot_hash(self)
            )
        else:
            # A caller-supplied hash is never trusted: the DTO recomputes
            # the digest from its full normalized content and rejects any
            # mismatch so the frozen snapshot cannot be forged.
            expected = compute_rate_snapshot_hash(self)
            supplied = _optional_text(self.snapshot_hash, "snapshot_hash")
            if supplied != expected:
                raise DomainValidationError(
                    "snapshot_hash does not match the recomputed content "
                    f"digest {expected}"
                )
            object.__setattr__(self, "snapshot_hash", supplied)

    def rate_for(self, session_date: date) -> Decimal | None:
        """The frozen rate of one session, or ``None`` when missing."""

        return self.rates.get(session_date)


def deterministic_missing_ranges(
    expected_sessions: Sequence[date],
    available_dates: set[date],
) -> list[tuple[date, date]]:
    """Collapse expected-but-missing sessions into contiguous date ranges."""

    expected = tuple(expected_sessions)
    missing = [day for day in expected if day not in available_dates]
    expected_index = {day: index for index, day in enumerate(expected)}
    ranges: list[tuple[date, date]] = []
    for day in missing:
        if ranges and expected_index[day] == expected_index[ranges[-1][1]] + 1:
            start, _ = ranges[-1]
            ranges[-1] = (start, day)
        else:
            ranges.append((day, day))
    return ranges
