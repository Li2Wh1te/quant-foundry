from datetime import date, datetime, timezone, time

from app.backtesting.data.consistency import fixed_chunk_plan, build_consistency_scope
from app.backtesting.data.consistency import BoundedChunkCache, short_read_context
from app.backtesting.data.protocols import CoverageEnvelope


def test_coverage_envelope_exposes_extended_scope_summary():
    envelope = CoverageEnvelope(
        date(2026, 1, 1), date(2026, 1, 2),
        axis_session_signature="a" * 64,
        covered_chunk_start=2, covered_chunk_end=4,
    )
    summary = envelope.to_summary()
    assert summary["covered_chunk_start"] == 2
    assert summary["covered_chunk_end"] == 4
    assert summary["axis_session_signature"] == "a" * 64


def test_fixed_chunk_plan_is_deterministic_and_bounded():
    from app.backtesting.calendar_axis import SessionPoint, SessionWindow
    sessions = tuple(
        SessionPoint(date(2026, 1, i + 1), f"s{i+1}", "UTC", (SessionWindow(time(9), time(10)),))
        for i in range(21)
    )
    plan = fixed_chunk_plan(sessions)
    assert [len(item[2]) for item in plan] == [20, 1]


def test_short_read_context_releases_resource_on_query_error():
    class Resource:
        def __init__(self): self.closed = self.rolled_back = False
        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True
    class Backend:
        def __init__(self): self.resource = Resource()
        def open_short_read(self, private_handle, query): return self.resource
    backend = Backend()
    try:
        with short_read_context(backend, object(), object()):
            raise RuntimeError("query failed")
    except RuntimeError:
        pass
    assert backend.resource.closed and backend.resource.rolled_back


def test_bounded_chunk_cache_rejects_unscoped_and_clears_on_scope_change():
    cache = BoundedChunkCache(max_entries=1)
    cache.put("scope-a", "q", 1)
    assert cache.get("scope-a", "q") == 1
    cache.put("scope-b", "q", 2)
    assert cache.get("scope-a", "q") is None
    assert cache.get("scope-b", "q") == 2


def test_bounded_chunk_cache_evicts_the_least_recently_used_entry():
    cache = BoundedChunkCache(max_entries=2)
    cache.put("scope", "old", 1)
    cache.put("scope", "kept", 2)
    assert cache.get("scope", "old") == 1

    cache.put("scope", "new", 3)

    assert cache.get("scope", "kept") is None
    assert cache.get("scope", "old") == 1
    assert cache.get("scope", "new") == 3
    assert len(cache) == 2
