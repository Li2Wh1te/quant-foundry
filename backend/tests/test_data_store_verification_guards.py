"""Full verification keeps cancellation, time and measured resource bounds."""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.data_store import verify_coverage as verification
from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.registry import BY_ID
from app.data_store.budget import Reservation
from app.data_store.errors import DataStoreError
from app.data_store.pipeline import read_entry_status
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_local_pipeline import ready, Inputs, NOW
from tests.test_data_store_domain_samples import sample


def window():
    source = sample('E50')
    point = source.content['item'][0]
    return replace(source, content={**source.content, 'item': [
        dict(point, date_ms=point['date_ms'] + index * 86400000) for index in range(200)]})


def unchanged_and_released(store, entry, generation):
    assert store.catalog.dataset(entry.spec.name)['generation'] == generation
    assert not store.budget.pending_keys()
    assert 'full_coverage' not in read_entry_status(store, entry.id)
    with store.locks.read(entry.spec.name, timeout_ms=100):
        pass


def test_cancel_inside_one_historical_observation_is_not_delayed_to_file_scan(ready):
    entry = BY_ID['E50']
    ready.register(entry.spec)
    started = [False]
    checks = [0]

    class Window(Inputs):
        def iter_entry(self, entry):
            started[0] = True
            yield from super().iter_entry(entry)

    def cancelled():
        if started[0]:
            checks[0] += 1
        return checks[0] >= 10

    with pytest.raises(DataStoreError) as caught:
        verification.verify_existing(ready, entry, Window(window()), cancelled=cancelled)
    assert caught.value.code == 'OPERATION_CANCELLED'
    assert caught.value.verification['phase'] == 'native_snapshot'
    assert 0 < caught.value.verification['scanned_objects'] < 200
    unchanged_and_released(ready, entry, 0)


def test_timeout_inside_one_historical_observation_preserves_pending(ready, monkeypatch):
    entry = BY_ID['E50']
    ready.register(entry.spec)
    started = [False]
    tick = [verification.time.monotonic()]

    class Window(Inputs):
        def iter_entry(self, entry):
            started[0] = True
            yield from super().iter_entry(entry)

    def clock():
        if started[0]:
            tick[0] += 1
        return tick[0]

    # Keep other modules on their actual lock/resource clocks. Only the
    # verifier's elapsed-time input is controlled in this isolated fixture.
    monkeypatch.setattr(verification, 'time', SimpleNamespace(monotonic=clock))
    with pytest.raises(DataStoreError) as caught:
        verification.verify_existing(ready, entry, Window(window()), seconds=3)
    assert caught.value.code == 'QUERY_TIMEOUT'
    assert caught.value.verification['phase'] == 'native_snapshot'
    assert 0 < caught.value.verification['scanned_objects'] < 200
    unchanged_and_released(ready, entry, 0)


@pytest.mark.parametrize('code', ['MEMORY_PRESSURE', 'DISK_PRESSURE',
                                 'SCRATCH_BUDGET_EXCEEDED', 'UNSAFE_STORAGE_PATH'])
def test_tiny_scalar_stream_keeps_bounded_measured_resource_failures(ready, monkeypatch, code):
    entry = BY_ID['E68']
    ready.register(entry.spec)
    produced = [0]
    measured = [False]
    original = Reservation.check

    class Calendar:
        summary = {'complete': False}

        def iter_entry(self, entry):
            for index in range(200):
                produced[0] += 1
                body = {'exchange': 'SSE', 'calendar_date':
                        (date(2026, 1, 1) + timedelta(days=index)).isoformat(), 'is_open': True}
                yield LocalInput('tushare', entry.native, 'SSE', 'default', NOW, body, digest(body))
            self.summary = {'complete': True}

    def resource_check(space, **kwargs):
        if produced[0] >= 2 and not measured[0]:
            measured[0] = True
            raise DataStoreError(code)
        return original(space, **kwargs)

    # With a constant verifier clock the operation-count bound alone must
    # trigger measurement, even when no elapsed-time interval has passed.
    now = verification.time.monotonic()
    monkeypatch.setattr(verification, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(Reservation, 'check', resource_check)
    with pytest.raises(DataStoreError) as caught:
        verification.verify_existing(ready, entry, Calendar())
    assert caught.value.code == code
    assert caught.value.verification['phase'] == 'native_snapshot'
    assert 1 < produced[0] <= 128
    unchanged_and_released(ready, entry, 0)
