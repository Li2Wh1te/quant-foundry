"""Review counterexamples on actual native contracts; synthetic inputs only."""
from datetime import timedelta
import gc
import json
import sys
from unittest.mock import Mock
import weakref

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_ingestion.tonghuashun.contracts import CollectionError, CollectionParameters, content_hash
from app.data_ingestion.tonghuashun.directory_proof import DirectoryReplays
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect
from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_ticker_conflict
from tests.test_tonghuashun_collections import NOW, Observation, State, Ticker, engine, reply, ticker
from tests.test_tonghuashun_directory_reuse import assert_originals, publish_directory, receipts
from tests.test_tonghuashun_directory_mutual_blocking import (
    A, B, NEW_A, NEW_B, OTHER_ETF, OTHER_OTC, swap_fixture, NEW_SNAPSHOT,
)


def retained_pair(rows=None, *, snapshot=20, revision=1):
    rows = rows or {'fund-etf': [NEW_A, OTHER_ETF], 'fund-otc': [NEW_B, OTHER_OTC]}
    cache = DirectoryReplays()
    for asset, code, old in [('fund-etf', rows['fund-etf'][0]['thscode'], 'fund-otc'),
                             ('fund-otc', rows['fund-otc'][0]['thscode'], 'fund-etf')]:
        data = {'timestamp': snapshot, 'item': rows[asset]}
        diagnostic = bounded_ticker_conflict(asset, {'source_code': code,
            'old_asset_type': old, 'new_asset_type': asset, 'provider_snapshot': snapshot,
            'directory_content_hash': content_hash(data)})
        assert cache.retain(asset, revision, data, receipts(asset, len(rows[asset]) // 10000 + 1),
                            NOW + timedelta(days=1), len(rows[asset]), diagnostic)
    entries = cache.pair()
    assert len(entries) == 2
    return cache, entries


def advanced_heads(engine):
    with Session(engine) as session:
        for asset, rows in [('fund-etf', [B, OTHER_ETF]), ('fund-otc', [A, OTHER_OTC])]:
            publish_directory(session, asset, rows, snapshot=10)
        # Nonempty valid 30 heads omit both former claims. Registry old rows
        # retain their exact snapshot-10 claim times and raw hashes.
        for asset, rows in [('fund-etf', [OTHER_ETF]), ('fund-otc', [OTHER_OTC])]:
            publish_directory(session, asset, rows, snapshot=30, when=NOW + timedelta(hours=1))
        session.commit()
        before = {str(v.id): (v.data_json, v.request_json, v.content_hash)
                  for v in session.scalars(select(Observation))}
    return before


def test_review_counterexample_snapshot_10_then_heads_30_cannot_publish_pair_20(engine, record_testsuite_property):
    before = advanced_heads(engine)
    cache, entries = retained_pair(revision=2)
    with Session(engine) as session:
        with pytest.raises(CollectionError):
            CollectionRepository(session).publish_pair(entries, now=NOW + timedelta(days=2))
        session.rollback()
    cache.clear()
    with Session(engine) as session:
        assert_originals(session, before)
        assert len(list(session.scalars(select(Observation)))) == len(before)
        for asset in ['fund-etf', 'fund-otc']:
            head = session.get(State, ('tickers', asset, 'default'))
            assert head.revision == 2
            assert json.loads(session.get(Observation, head.observation_id).data_json)['timestamp'] == 30
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
    record_testsuite_property('P1_snapshot_10_30_20_rejected_before_any_new_original', True)


def test_review_counterexample_large_pair_releases_prevalidation_sets_under_native_context(engine, monkeypatch, record_testsuite_property):
    from app.data_ingestion.tonghuashun import directory_proof as proof
    rows = {asset: [{'thscode': prefix + f'{i:047d}' + '.ABCDEFGH', 'asset_type': asset}
                   for i in range(100000)]
            for asset, prefix in [('fund-etf', 'E'), ('fund-otc', 'O')]}
    assert all(len(row['thscode']) == 57 for asset in rows for row in rows[asset])
    with Session(engine) as session:
        publish_directory(session, 'fund-etf', [{**rows['fund-otc'][0], 'asset_type': 'fund-etf'}], snapshot=10)
        publish_directory(session, 'fund-otc', [{**rows['fund-etf'][0], 'asset_type': 'fund-otc'}], snapshot=10)
    cache, entries = retained_pair(rows)
    assert all(entry.packed is not None for entry in entries)
    del rows
    memberships, measures = [], []
    complete = proof.complete_directory
    def tracked(*args, **kwargs):
        catalogue = complete(*args, **kwargs)
        if catalogue:
            memberships.append((weakref.ref(catalogue.codes), proof._bounded_size(catalogue.codes, 64 * 1024**2)))
        return catalogue
    monkeypatch.setattr(proof, 'complete_directory', tracked)
    original = proof._original
    def measured(*args, **kwargs):
        value = original(*args, **kwargs)
        gc.collect()
        live = [(reference, size) for reference, size in memberships if reference() is not None]
        size = cache.bytes + sum(cost for _, cost in live)
        measures.append({'bytes': size, 'membership_sets': len(live), 'python': sys.version})
        assert size <= proof.CONTEXT_BYTES
        return value
    monkeypatch.setattr(proof, '_original', measured)
    proven = proof.category_change_proven
    def probe(*args, **kwargs):
        assert proven(*args, **kwargs) is True
        raise RuntimeError('synthetic lifecycle probe after full exact claim proof')
    monkeypatch.setattr(proof, 'category_change_proven', probe)
    with Session(engine) as session:
        with pytest.raises(RuntimeError) as saved:
            CollectionRepository(session).publish_pair(entries, now=NOW + timedelta(days=2))
        session.rollback()
    assert saved.value.__traceback__ is not None
    assert measures and max(row['bytes'] for row in measures) <= proof.CONTEXT_BYTES
    gc.collect()
    assert all(reference() is None for reference, _ in memberships)
    record_testsuite_property('P2_context_limit_bytes', proof.CONTEXT_BYTES)
    record_testsuite_property('P2_native_retained_context_peak_bytes', max(row['bytes'] for row in measures))
    record_testsuite_property('P2_native_membership_set_peak_count', max(row['membership_sets'] for row in measures))
    record_testsuite_property('P2_retained_original_buffer_bytes', cache.bytes)
    record_testsuite_property('P2_membership_refs_released_with_saved_traceback', True)
    record_testsuite_property('P2_measurement_kind', 'conservative object context accounting; not process RSS')
    cache.clear()


def test_review_counterexample_pair_second_stage_rejection_is_bounded_visible(engine, monkeypatch):
    from app.data_ingestion.tonghuashun import service
    swap_fixture(engine, 'fund-etf')
    events = []
    logger = Mock()
    logger.warning.side_effect = lambda *args, **kwargs: events.append({'event': args[0] if args else '', **kwargs})
    monkeypatch.setattr(service, 'logger', logger)
    def rejected(*args, **kwargs):
        raise CollectionError('vendor_secret_payload_api_key_do_not_log')
    monkeypatch.setattr(CollectionRepository, 'publish_pair', rejected)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    selected = [event for event in events if event['event'] == 'tonghuashun_directory_pair_rejected']
    assert len(selected) == 1
    diagnostic = selected[0]['pair_rejection']
    assert diagnostic['stage'] == 'pair_publication'
    assert diagnostic['reason'] == 'publication_rejected'
    assert len(diagnostic['candidates']) == 2
    assert all(row['expected_revision'] == 2 and len(row['directory_content_hash']) == 64
               for row in diagnostic['candidates'])
    assert len(json.dumps(selected[0]).encode()) <= 2048
    assert 'vendor_secret' not in json.dumps(selected[0])
    assert client.request.call_count == 2


@pytest.mark.parametrize('rejection', ['revision_changed', 'original_hash_mismatch', 'unproven_category_change'])
def test_actual_pair_rejection_records_stage_revisions_hash_and_later_conflict(engine, monkeypatch, rejection):
    from app.data_ingestion.tonghuashun import service
    before = swap_fixture(engine, 'fund-etf')
    if rejection == 'unproven_category_change':
        with Session(engine) as session:
            session.get(Ticker, OTHER_ETF['thscode']).asset_type = 'a-share'
            session.commit()
    pair = DirectoryReplays.pair
    def mutate_after_ready(self):
        entries = pair(self)
        if entries:
            if rejection == 'revision_changed':
                with Session(engine) as session:
                    session.get(State, ('tickers', 'fund-etf', 'default')).revision += 1
                    session.commit()
            elif rejection == 'original_hash_mismatch':
                entries[0].data['timestamp'] += 1
        return entries
    monkeypatch.setattr(DirectoryReplays, 'pair', mutate_after_ready)
    events = []
    logger = Mock()
    logger.warning.side_effect = lambda *args, **kwargs: events.append({'event': args[0], **kwargs})
    monkeypatch.setattr(service, 'logger', logger)
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW_A, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else [NEW_B, OTHER_OTC],
        timestamp=NEW_SNAPSHOT)
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    selected = [event for event in events if event['event'] == 'tonghuashun_directory_pair_rejected']
    assert len(selected) == 1
    diagnostic = selected[0]['pair_rejection']
    assert diagnostic['reason'] == rejection
    assert diagnostic['stage'] == ('pair_publication' if rejection == 'unproven_category_change' else 'pair_head_admission')
    assert diagnostic['publication_committed'] is False
    assert len(diagnostic['candidates']) == 2
    if rejection == 'revision_changed':
        assert diagnostic['current_revision'] == 3
    elif rejection == 'original_hash_mismatch':
        assert len(diagnostic['actual_content_hash']) == 64
        assert diagnostic['actual_content_hash'] != diagnostic['candidates'][0]['directory_content_hash']
    else:
        assert diagnostic['ticker_conflict']['source_code'] == OTHER_ETF['thscode']
        assert diagnostic['ticker_conflict']['old_asset_type'] == 'a-share'
    assert client.request.call_count == 2
    with Session(engine) as session:
        assert_originals(session, before)
        assert len(list(session.scalars(select(Observation)))) == len(before)
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'


def test_pair_diagnostic_rebuild_discards_unknown_text_and_credentials():
    from types import SimpleNamespace
    from app.data_ingestion.tonghuashun.ticker_diagnostics import PairPublicationRejected, bounded_pair_rejection
    entry = SimpleNamespace(subject='fund-etf', revision=2, request_hash='a'*64,
        conflict={'provider_snapshot': 20, 'directory_content_hash': 'b'*64, 'secret': 'provider_secret'})
    error = PairPublicationRejected('pair_publication', 'unproven_category_change', 'fund-etf',
        ticker_conflict={'source_code': A['thscode'], 'old_asset_type': 'fund-otc', 'new_asset_type': 'fund-etf',
                         'provider_snapshot': 20, 'directory_content_hash': 'b'*64, 'payload': 'provider_secret'},
        current_snapshot='provider_secret', current_head_hash='provider_secret', secret='provider_secret')
    safe = bounded_pair_rejection((entry,), error)
    assert safe['ticker_conflict']['source_code'] == A['thscode']
    assert 'provider_secret' not in json.dumps(safe)
    assert len(json.dumps(safe).encode()) <= 1792


def test_pair_membership_combined_budget_rejects_and_releases_saved_traceback_sets(engine, monkeypatch):
    from types import SimpleNamespace
    from app.data_ingestion.tonghuashun import directory_proof as proof
    from app.data_ingestion.tonghuashun.ticker_diagnostics import PairPublicationRejected
    monkeypatch.setattr(proof, 'CONTEXT_BYTES', 2 * 1024**2)
    references = []
    complete = proof.complete_directory
    def tracked(*args, **kwargs):
        catalogue = complete(*args, **kwargs)
        assert catalogue is not None
        references.append(weakref.ref(catalogue.codes))
        return catalogue
    monkeypatch.setattr(proof, 'complete_directory', tracked)
    entries = []
    for asset, prefix in [('fund-etf', 'E'), ('fund-otc', 'O')]:
        body = {'timestamp': 20, 'item': [{'thscode': prefix+f'{i:047d}'+'.ABCDEFGH', 'asset_type': asset}
                                        for i in range(4000)]}
        entries.append(SimpleNamespace(subject=asset, observed_at=NOW,
            payload=lambda data=body, subject=asset: (data, receipts(subject))))
    with Session(engine) as session:
        with pytest.raises(PairPublicationRejected) as saved:
            CollectionRepository(session)._pair_memberships(entries)
    assert saved.value.reason == 'context_budget_exceeded'
    assert saved.value.__traceback__ is not None
    gc.collect()
    assert len(references) == 2 and all(reference() is None for reference in references)


def test_pair_postgres_concurrent_head_10_to_30_preserves_newer_head_against_20(engine):
    if engine.dialect.name != 'postgresql':
        pytest.skip('actual PostgreSQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from sqlalchemy import text
    from app.data_ingestion.tonghuashun.ticker_diagnostics import PairPublicationRejected
    with Session(engine) as session:
        publish_directory(session, 'fund-etf', [B, OTHER_ETF], snapshot=10)
        publish_directory(session, 'fund-otc', [A, OTHER_OTC], snapshot=10)
    cache, entries = retained_pair()
    started, pids, errors = threading.Event(), [], []
    def candidate():
        with Session(engine) as session:
            pids.append(session.scalar(text('SELECT pg_backend_pid()')))
            started.set()
            with pytest.raises(PairPublicationRejected) as error:
                CollectionRepository(session).publish_pair(entries, now=NOW + timedelta(days=2))
            errors.append(error.value)
            session.rollback()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as holder:
            holder.scalar(select(State).where(State.dataset == 'tickers', State.subject == 'fund-etf',
                State.variant == 'default').with_for_update())
            future = executor.submit(candidate)
            assert started.wait(3)
            for _ in range(60):
                with engine.connect() as observer:
                    waiting = observer.scalar(text('SELECT wait_event_type=:kind FROM pg_stat_activity WHERE pid=:pid'),
                                              {'kind': 'Lock', 'pid': pids[0]})
                if waiting:
                    break
                time.sleep(.05)
            assert waiting is True
            publish_directory(holder, 'fund-etf', [OTHER_ETF], snapshot=30, when=NOW + timedelta(hours=1))
        future.result(timeout=5)
    assert errors[0].reason == 'revision_changed' and errors[0].context['current_revision'] == 2
    cache.clear()
    with Session(engine) as session:
        assert len(list(session.scalars(select(Observation)))) == 3
        head = session.get(State, ('tickers', 'fund-etf', 'default'))
        assert head.revision == 2
        assert json.loads(session.get(Observation, head.observation_id).data_json)['timestamp'] == 30
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'
        assert session.get(Ticker, B['thscode']).asset_type == 'fund-etf'
