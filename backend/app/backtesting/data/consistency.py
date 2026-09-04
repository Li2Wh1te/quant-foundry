"""Provider-neutral consistency token contracts and scope construction.

This module is deliberately free of I/O.  Providers implement
``ConsistencyTokenBackend`` and keep their private token handles out of DTOs;
the pure helpers here only derive bounded, non-sensitive scope metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from collections import OrderedDict
import sys
from datetime import date, datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.backtesting.data.protocols import CoverageEnvelope, ConsistencyTokenStatus
from app.backtesting.data.requests import (
    ConsistencyMode, ConsistencyValidation, DataCapability, DataRequest,
    MAX_LOOKBACK_SESSIONS, _aware_datetime,
)
from app.backtesting.data.reports import canonical_hash
from app.backtesting.calendar_axis import SessionPoint

__all__ = [
    "ConsistencyTokenBackend", "ConsistencyTokenScope", "ConsistencyTokenLease",
    "build_consistency_scope", "fixed_chunk_plan", "short_read_context",
    "BoundedChunkCache",
]


@contextmanager
def short_read_context(backend: ConsistencyTokenBackend, private_handle: Any, query: object):
    """Open one token-bound short read and always release its resources.

    Providers may return a context manager or a plain resource exposing
    ``close``/``rollback``.  Cleanup is deliberately best-effort and runs in
    ``finally`` so exceptions raised by query consumers cannot leak cursors,
    transactions, or connections.
    """
    resource = backend.open_short_read(private_handle, query)
    entered = resource
    exc_info = (None, None, None)
    try:
        if hasattr(resource, "__enter__"):
            entered = resource.__enter__()
        yield entered
    except BaseException:
        exc_info = sys.exc_info()
        raise
    finally:
        try:
            if hasattr(resource, "__exit__"):
                resource.__exit__(*exc_info)
            else:
                rollback = getattr(resource, "rollback", None)
                if callable(rollback):
                    rollback()
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
        finally:
            # A backend may expose an explicit release hook for pooled handles.
            release = getattr(backend, "release_short_read", None)
            if callable(release):
                release(resource)


class BoundedChunkCache:
    """Small scope-aware LRU cache; entries never span consistency scopes."""

    def __init__(self, *, max_entries: int = 128) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[object, object], object] = OrderedDict()
        self._scope: object | None = None

    def _key(self, scope: object, key: object) -> tuple[object, object]:
        if scope is None:
            raise ValueError("cache scope is required")
        if self._scope is not None and scope != self._scope:
            # Scope transition starts a fresh chunk cache, preventing cross-token reuse.
            self.clear()
        self._scope = scope
        return scope, key

    def get(self, scope: object, key: object, default: object = None) -> object:
        if scope is None:
            raise ValueError("cache scope is required")
        # A stale lookup must not evict the active scope; callers may probe
        # an older token while retaining valid entries for the current chunk.
        if self._scope is not None and scope != self._scope:
            return default
        cache_key = self._key(scope, key)
        if cache_key not in self._entries:
            return default
        value = self._entries.pop(cache_key)
        self._entries[cache_key] = value
        return value

    def put(self, scope: object, key: object, value: object) -> object:
        cache_key = self._key(scope, key)
        self._entries.pop(cache_key, None)
        self._entries[cache_key] = value
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        self._entries.clear()
        self._scope = None

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True, slots=True)
class ConsistencyTokenScope:
    """Finite token scope; chunk range is a half-open interval."""
    covered_chunk_start: int
    covered_chunk_end: int
    envelope: CoverageEnvelope
    fact_types: tuple[DataCapability, ...]
    dependency_fact_types: tuple[DataCapability, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyTokenLease:
    """Provider-issued public metadata.  The private handle is never a field."""
    public_digest: str | None
    issued_at: datetime
    covered_scope: Mapping[str, object]
    data_watermark: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", _aware_datetime(self.issued_at, "issued_at"))
        if self.data_watermark is not None:
            object.__setattr__(self, "data_watermark", _aware_datetime(self.data_watermark, "data_watermark"))


@runtime_checkable
class ConsistencyTokenBackend(Protocol):
    """Minimal provider-neutral token backend implemented by a DataProvider."""
    def issue_token(self, scope: ConsistencyTokenScope) -> Any: ...
    def validate_token(self, private_handle: Any, scope: ConsistencyTokenScope) -> ConsistencyTokenStatus: ...
    def open_short_read(self, private_handle: Any, query: object) -> Any: ...


def fixed_chunk_plan(resolved_sessions: Sequence[SessionPoint], *, chunk_size: int = 20) -> tuple[tuple[int, int, tuple[SessionPoint, ...]], ...]:
    """Split frozen formal sessions into deterministic contiguous chunks."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size != 20:
        raise ValueError("chunk_size must be exactly 20 for fixed_trading_sessions@1")
    sessions = tuple(resolved_sessions)
    if any(not isinstance(item, SessionPoint) for item in sessions):
        raise TypeError("resolved_sessions must contain SessionPoint values")
    return tuple((start // chunk_size, start, sessions[start : start + chunk_size]) for start in range(0, len(sessions), chunk_size))


def build_consistency_scope(*, request: DataRequest, resolved_sessions: Sequence[SessionPoint], warmup_sessions: Sequence[SessionPoint] = (), chunk_range: tuple[int, int] = (0, 1), fact_types: Sequence[DataCapability] = (), dependency_fact_types: Sequence[DataCapability] = ()) -> ConsistencyTokenScope:
    """Construct a bounded scope from frozen metadata without reading data."""
    formal, warmup = tuple(resolved_sessions), tuple(warmup_sessions)
    start, end = chunk_range
    chunks = fixed_chunk_plan(formal)
    if start < 0 or end <= start or end > len(chunks):
        raise ValueError("chunk_range must identify a finite contiguous chunk interval")
    chunk_sessions = tuple(item for _, _, members in chunks[start:end] for item in members)
    if not chunk_sessions:
        raise ValueError("chunk range cannot be empty")
    capabilities = tuple(dict.fromkeys(fact_types))
    dependencies = tuple(dict.fromkeys(dependency_fact_types))
    axis_sig = canonical_hash([item.session_id for item in formal])
    warm_sig = canonical_hash([item.session_id for item in warmup])
    history_sig = canonical_hash(
        {
            "lookback_sessions": request.max_lookback_sessions,
            "formal": [item.session_id for item in formal],
            "warmup": [item.session_id for item in warmup],
        }
    )
    fact_sig = canonical_hash(
        {
            "fact_types": [item.value for item in capabilities],
            "dependency_fact_types": [item.value for item in dependencies],
        }
    )
    envelope = CoverageEnvelope(
        chunk_first_session_date=chunk_sessions[0].session_date,
        chunk_last_session_date=chunk_sessions[-1].session_date,
        fact_types=capabilities,
        warmup_first_session_date=warmup[0].session_date if warmup else None,
        warmup_session_count=len(warmup),
        lookback_envelope_sessions=request.max_lookback_sessions,
        axis_session_signature=axis_sig,
        warmup_session_signature=warm_sig,
        history_envelope_signature=history_sig,
        fact_coverage_signature=fact_sig,
        covered_chunk_start=start,
        covered_chunk_end=end,
        dependency_fact_types=dependencies,
        data_cutoff=request.query_boundary.data_cutoff,
        knowledge_as_of=request.query_boundary.knowledge_as_of,
    )
    return ConsistencyTokenScope(start, end, envelope, capabilities, dependencies)
