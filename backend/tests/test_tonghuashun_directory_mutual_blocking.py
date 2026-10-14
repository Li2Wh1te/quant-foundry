"""Synthetic same-snapshot swaps expose the real batch's publication cycle."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_ingestion.tonghuashun.contracts import CollectionError, CollectionParameters
from app.data_ingestion.tonghuashun.service import collect
from tests.test_tonghuashun_collections import NOW, Observation, State, Ticker, engine, reply, ticker
from tests.test_tonghuashun_directory_reuse import (
    OLD_SNAPSHOT, NEW_SNAPSHOT, publish_directory, assert_originals, receipts,
)

A = ticker('158025.SZ', 'fund-otc')
B = ticker('158037.SZ', 'fund-etf')
NEW_A = {**A, 'asset_type': 'fund-etf'}
NEW_B = {**B, 'asset_type': 'fund-otc'}
OTHER_ETF = ticker('510300.SH', 'fund-etf')
OTHER_OTC = ticker('000001.OF', 'fund-otc')


def swap_fixture(engine, first, extra=None):
    from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
    TonghuashunWorkUnit.__table__.create(engine)
    with Session(engine) as session:
        for asset, rows in [('fund-etf', [B, OTHER_ETF]), ('fund-otc', [A, OTHER_OTC])]:
            publish_directory(session, asset, rows + (extra or {}).get(asset, []), snapshot=OLD_SNAPSHOT)
        second = 'fund-otc' if first == 'fund-etf' else 'fund-etf'
        session.get(State, ('tickers', second, 'default')).attempted_at = NOW + timedelta(seconds=1)
        session.commit()
        originals = {str(v.id): (v.data_json, v.request_json, v.content_hash)
                     for v in session.scalars(select(Observation))}
    return originals


@pytest.mark.parametrize('first', ['fund-etf', 'fund-otc'])
def test_same_snapshot_mutual_category_swap_publishes_both_without_new_requests(engine, first):
    originals = swap_fixture(engine, first)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    result = collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                     client, engine, now=NOW + timedelta(days=1))
    assert result['succeeded'] == 2 and result['failed'] == 0
    assert client.request.call_count == 2
    assert [v.args[1]['offset'] for v in client.request.call_args_list] == [0, 0]
    with Session(engine) as session:
        assert_originals(session, originals)
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-etf'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-otc'
        for asset in ['fund-etf', 'fund-otc']:
            state = session.get(State, ('tickers', asset, 'default'))
            assert state.status == 'succeeded'
            original = session.get(Observation, state.observation_id)
            assert original.request_json and original.data_json


@pytest.mark.parametrize('defect', ['different_snapshot', 'overlapping_membership',
                                  'wrong_old_claim', 'ambiguous_old_claim', 'missing_receipt'])
def test_mutual_pair_cannot_relax_missing_or_contradictory_original_proof(engine, monkeypatch, defect):
    from app.data_ingestion.tonghuashun import directory_proof
    from app.data_ingestion.tonghuashun.contracts import exact_json
    originals = swap_fixture(engine, 'fund-etf')
    with Session(engine) as session:
        if defect == 'wrong_old_claim':
            session.get(Ticker, B['thscode']).raw_json = exact_json({**B, 'name': 'wrong native claim'})
        if defect == 'ambiguous_old_claim':
            original = session.scalar(select(Observation).where(Observation.subject == 'fund-etf'))
            session.add(Observation(dataset=original.dataset, subject=original.subject, variant=original.variant,
                observed_at=original.observed_at, request_json=original.request_json, data_json=original.data_json,
                content_hash=original.content_hash, row_count=original.row_count, chain_depth=0))
        session.commit()
        original_count = len(list(session.scalars(select(Observation))))
    if defect == 'missing_receipt':
        retain = directory_proof.DirectoryReplays.retain
        def lose_receipt(self, subject, *args):
            result = retain(self, subject, *args)
            if result and subject == 'fund-etf':
                self.entries[subject].requests.clear()
            return result
        monkeypatch.setattr(directory_proof.DirectoryReplays, 'retain', lose_receipt)
    client = Mock(interval_ms=0)
    def response(interface, parameters):
        etf = parameters['asset_type'] == 'fund-etf'
        rows = [NEW_A, OTHER_ETF] if etf else [NEW_B, OTHER_OTC]
        if defect == 'overlapping_membership' and etf:
            rows.append(B)
        snapshot = NEW_SNAPSHOT + (1 if defect == 'different_snapshot' and etf else 0)
        return reply(rows, timestamp=snapshot)
    client.request.side_effect = response
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    assert client.request.call_count == 2
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == original_count
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
        assert all(session.get(State, ('tickers', asset, 'default')).status == 'failed'
                   for asset in ['fund-etf', 'fund-otc'])


def test_pair_second_publication_error_rolls_back_both_and_releases_cache(engine, monkeypatch):
    from app.data_ingestion.tonghuashun import directory_proof
    from app.data_ingestion.tonghuashun.repository import CollectionRepository
    originals = swap_fixture(engine, 'fund-otc')
    caches = []
    constructor = directory_proof.DirectoryReplays
    def cache():
        value = constructor()
        caches.append(value)
        return value
    monkeypatch.setattr(directory_proof, 'DirectoryReplays', cache)
    publish = CollectionRepository.publish
    def fail_second(self, dataset, subject, variant, **kwargs):
        result = publish(self, dataset, subject, variant, **kwargs)
        if kwargs.get('_paired_heads') is not None and subject == 'fund-otc':
            raise RuntimeError('isolated pair error after both tentative changes')
        return result
    monkeypatch.setattr(CollectionRepository, 'publish', fail_second)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    with pytest.raises(RuntimeError) as error:
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    assert error.value.__traceback__ is not None
    assert caches[0].entries == {} and caches[0].bytes == 0
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == len(originals)
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
        assert all(session.get(State, ('tickers', asset, 'default')).status == 'failed'
                   for asset in ['fund-etf', 'fund-otc'])


def test_pair_rechecks_revision_before_any_new_original(engine, monkeypatch):
    from app.data_ingestion.tonghuashun import directory_proof
    originals = swap_fixture(engine, 'fund-otc')
    pair = directory_proof.DirectoryReplays.pair
    def concurrent_change(self):
        entries = pair(self)
        if entries:
            with Session(engine) as session:
                session.get(State, ('tickers', 'fund-etf', 'default')).revision += 1
                session.commit()
        return entries
    monkeypatch.setattr(directory_proof.DirectoryReplays, 'pair', concurrent_change)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == len(originals)
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 3
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'


def test_large_validated_directory_retains_exact_native_bytes_under_same_ceiling(monkeypatch):
    from decimal import Decimal
    from app.data_ingestion.tonghuashun import directory_proof as proof
    from app.data_ingestion.tonghuashun.contracts import content_hash, exact_json
    from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_ticker_conflict
    monkeypatch.setattr(proof, 'CONTEXT_BYTES', 1024 * 1024)
    rows = [ticker(f'{i:06d}.OF', 'fund-otc', name='fixture', sample=Decimal('1.23456789'))
            for i in range(1000)]
    data = {'timestamp': NEW_SNAPSHOT, 'item': rows}
    request = receipts('fund-otc')
    diagnostic = bounded_ticker_conflict('fund-otc', {
        'source_code': B['thscode'], 'old_asset_type': 'fund-etf', 'new_asset_type': 'fund-otc',
        'provider_snapshot': NEW_SNAPSHOT, 'directory_content_hash': content_hash(data)})
    assert proof._bounded_size((data, request), proof.CONTEXT_BYTES // 2) is None
    cache = proof.DirectoryReplays()
    assert cache.retain('fund-otc', 2, data, request, NOW, len(rows), diagnostic)
    entry = cache.entries['fund-otc']
    assert entry.data is None and entry.requests is None and entry.packed is not None
    assert entry.packed[0] == exact_json(data).encode()
    restored, restored_requests = entry.payload()
    assert content_hash(restored) == content_hash(data)
    assert content_hash(restored_requests) == content_hash(request)
    assert cache.bytes < proof.CONTEXT_BYTES // 2
    cache.clear()
    assert entry.packed is None and cache.bytes == 0


def test_native_byte_limit_stops_compact_retention_before_oversized_copy(monkeypatch):
    from app.data_ingestion.tonghuashun import directory_proof as proof
    monkeypatch.setattr(proof, 'NATIVE_BYTES', 8)
    assert proof._bounded_json({'item': ['larger than the native ceiling']}, proof.NATIVE_BYTES) is None


@pytest.mark.parametrize('first', ['fund-etf', 'fund-otc'])
def test_pair_publishes_compact_originals_and_releases_both_owners(engine, monkeypatch, first):
    from decimal import Decimal
    from app.data_ingestion.tonghuashun import directory_proof as proof
    monkeypatch.setattr(proof, 'CONTEXT_BYTES', 2 * 1024 * 1024)
    extra = {asset: [ticker(f'{base + i:06d}.{suffix}', asset,
                           sample=Decimal('1.23456789')) for i in range(1000)]
             for asset, base, suffix in [('fund-etf', 500000, 'SH'), ('fund-otc', 100000, 'OF')]}
    originals = swap_fixture(engine, first, extra)
    compact, caches = [], []
    class Cache(proof.DirectoryReplays):
        def __init__(self):
            super().__init__()
            caches.append(self)
        def retain(self, subject, *args):
            result = super().retain(subject, *args)
            if result:
                compact.append(self.entries[subject].packed is not None)
            return result
    monkeypatch.setattr(proof, 'DirectoryReplays', Cache)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        ([NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC])
        + extra[parameters['asset_type']], timestamp=NEW_SNAPSHOT)
    result = collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                     client, engine, now=NOW + timedelta(days=1))
    assert result['succeeded'] == 2 and result['failed'] == 0 and result['received'] == 2004
    assert client.request.call_count == 2 and compact == [True, True]
    assert caches[0].bytes == 0 and caches[0].entries == {}
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == len(originals) + 2
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-etf'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-otc'


def test_pair_cancellation_after_tentative_publication_rolls_back_both(engine, monkeypatch):
    import time
    from app.data_ingestion.tonghuashun.control import (
        CollectionYield, CollectionControl, active_control,
    )
    from app.data_ingestion.tonghuashun.repository import CollectionRepository
    originals = swap_fixture(engine, 'fund-etf')
    cancelled = [False]
    publish_pair = CollectionRepository.publish_pair
    def cancel_after_both(self, *args, **kwargs):
        results = publish_pair(self, *args, **kwargs)
        cancelled[0] = True
        return results
    monkeypatch.setattr(CollectionRepository, 'publish_pair', cancel_after_both)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    monitor = CollectionControl(engine, None)
    original_check = monitor.check
    def checked():
        if cancelled[0]:
            raise CollectionYield('stopped')
        original_check()
    monkeypatch.setattr(monitor, 'check', checked)
    token = active_control.set(monitor)
    try:
        with pytest.raises(CollectionYield):
            collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                    client, engine, now=NOW + timedelta(days=1))
    finally:
        active_control.reset(token)
    assert client.request.call_count == 2
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == len(originals)
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
        assert all(session.get(State, ('tickers', asset, 'default')).status == 'failed'
                   for asset in ['fund-etf', 'fund-otc'])


def test_pair_waits_for_postgres_heads_and_preserves_newer_revision(engine):
    if engine.dialect.name != 'postgresql':
        pytest.skip('actual PostgreSQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from sqlalchemy import text
    from app.data_ingestion.tonghuashun.directory_proof import DirectoryReplays
    from app.data_ingestion.tonghuashun.contracts import content_hash
    from app.data_ingestion.tonghuashun.repository import CollectionRepository
    from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_ticker_conflict
    originals = swap_fixture(engine, 'fund-etf')
    cache = DirectoryReplays()
    for asset, rows, code, former in [('fund-etf', [NEW_A, OTHER_ETF], A['thscode'], 'fund-otc'),
                                      ('fund-otc', [NEW_B, OTHER_OTC], B['thscode'], 'fund-etf')]:
        data = {'timestamp': NEW_SNAPSHOT, 'item': rows}
        conflict = bounded_ticker_conflict(asset, {'source_code': code, 'old_asset_type': former,
            'new_asset_type': asset, 'provider_snapshot': NEW_SNAPSHOT,
            'directory_content_hash': content_hash(data)})
        assert cache.retain(asset, 1, data, receipts(asset), NOW + timedelta(days=1), len(rows), conflict)
    entries = cache.pair()
    assert len(entries) == 2
    started, pids = threading.Event(), []
    def candidate():
        with Session(engine) as session:
            pids.append(session.scalar(text('SELECT pg_backend_pid()')))
            started.set()
            with pytest.raises(CollectionError):
                CollectionRepository(session).publish_pair(entries, now=NOW + timedelta(days=2))
            session.rollback()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as holder:
            head = holder.scalar(select(State).where(State.dataset == 'tickers',
                State.subject == 'fund-etf', State.variant == 'default').with_for_update())
            future = executor.submit(candidate)
            assert started.wait(3)
            waiting = False
            for _ in range(60):
                with engine.connect() as observer:
                    waiting = observer.scalar(text('SELECT wait_event_type=:kind FROM pg_stat_activity WHERE pid=:pid'),
                                              {'kind': 'Lock', 'pid': pids[0]})
                if waiting:
                    break
                time.sleep(.05)
            assert waiting is True
            head.revision += 1
            holder.commit()
        future.result(timeout=5)
    cache.clear()
    with Session(engine) as session:
        assert_originals(session, originals)
        assert len(list(session.scalars(select(Observation)))) == len(originals)
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 2
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
