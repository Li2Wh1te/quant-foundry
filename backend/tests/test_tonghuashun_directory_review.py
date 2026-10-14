"""Merge-review guards independent of the original 27 E40 regression cases."""
from datetime import timedelta
import gc
from unittest.mock import Mock
import weakref

import pytest

from app.data_ingestion.tonghuashun.directory_proof import DirectoryAcquisition as Acquisition
from app.data_ingestion.tonghuashun.contracts import CollectionError, CollectionParameters, DATASETS
from app.data_ingestion.tonghuashun.control import CollectionControl, active_control
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect
from tests.test_tonghuashun_directory_reuse import (
    CODE, NEW, NEW_SNAPSHOT, OTHER_ETF, OTHER_OTC, baseline, engine, reply, ticker, NOW,
    Observation, Session, State, Ticker, assert_originals, content_hash, receipts,
)


def test_incomplete_full_page_cannot_be_stitched_to_a_later_terminal_page():
    page = [ticker(f'{index:06d}.SH') for index in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply(page, timestamp=NEW_SNAPSHOT, complete=False),
                                  reply([], timestamp=NEW_SNAPSHOT)]
    with pytest.raises(CollectionError, match='不完整'):
        Acquisition(client).directory(DATASETS['tickers'], 'fund-etf')
    assert client.request.call_count == 1


def test_unexpected_error_releases_the_invocation_replay_map_even_if_traceback_is_retained(engine, monkeypatch):
    from app.data_ingestion.tonghuashun import directory_proof
    baseline(engine)
    created = []
    original = directory_proof.DirectoryReplays
    def inspectable_replays():
        cache = original()
        created.append(cache)
        return cache
    monkeypatch.setattr(directory_proof, 'DirectoryReplays', inspectable_replays)
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([NEW, OTHER_ETF], timestamp=NEW_SNAPSHOT),
                                  RuntimeError('isolated unexpected fixture error')]
    with pytest.raises(RuntimeError) as captured:
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                client, engine, now=NOW + timedelta(days=1))
    assert captured.value.__traceback__ is not None
    assert len(created) == 1
    assert created[0].entries == {} and created[0].bytes == 0


@pytest.mark.parametrize('release', ['remove', 'clear'])
def test_released_ready_alias_drops_payload_without_mutating_originals(release):
    from app.data_ingestion.tonghuashun.directory_proof import DirectoryReplays
    from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_ticker_conflict
    data = {'timestamp': NEW_SNAPSHOT, 'item': [dict(NEW)]}
    requests = receipts('fund-etf')
    digest, request_digest = content_hash(data), content_hash(requests)
    conflict = bounded_ticker_conflict('fund-etf', {
        'source_code': CODE, 'old_asset_type': 'fund-otc', 'new_asset_type': 'fund-etf',
        'provider_snapshot': NEW_SNAPSHOT, 'directory_content_hash': digest})
    cache = DirectoryReplays()
    assert cache.retain('fund-etf', 2, data, requests, NOW, 1, conflict)
    ready = cache.ready('fund-otc', {'timestamp': NEW_SNAPSHOT, 'item': [OTHER_OTC]},
                        receipts('fund-otc'), NOW)
    assert len(ready) == 1
    if release == 'remove':
        cache.remove('fund-etf')
    else:
        cache.clear()
    assert cache.entries == {} and cache.bytes == 0
    assert ready[0].data is None and ready[0].requests is None
    assert content_hash(data) == digest and content_hash(requests) == request_digest


@pytest.mark.parametrize('failure', ['publication', 'after_removal', 'cleanup_progress_write'])
def test_replay_exception_releases_borrowed_objects_with_saved_tracebacks(engine, monkeypatch, failure):
    from app.data_ingestion.tonghuashun import directory_proof
    from app.scheduling.models import ScheduledTask, TaskRun
    before = baseline(engine)
    run_id = None
    if engine.dialect.name == 'postgresql':
        ScheduledTask.__table__.create(engine)
        TaskRun.__table__.create(engine)
        with Session(engine) as session:
            task = ScheduledTask(name='isolated replay progress fixture', task_type='data.ths.tickers',
                                 parameters={}, schedule={'type': 'once'}, state='paused')
            session.add(task)
            session.flush()
            run = TaskRun(task_id=task.id, task_version=1, task_type=task.task_type,
                          trigger_type='manual', status='running', parameters={}, parameter_version=1,
                          created_at=NOW, started_at=NOW)
            session.add(run)
            session.flush()
            run_id = run.id
            session.commit()
    monitor = CollectionControl(engine, run_id)
    created, entries, payloads, progress_writes = [], [], [], []
    original_cache = directory_proof.DirectoryReplays
    def inspectable_cache():
        cache = original_cache()
        retain = cache.retain
        def tracked_retain(subject, revision, data, requests, *args):
            result = retain(subject, revision, data, requests, *args)
            if result:
                entries.append(cache.entries[subject])
                payloads.extend(weakref.ref(obj) for obj in (data, data['item'], requests))
            return result
        cache.retain = tracked_retain
        created.append(cache)
        return cache
    monkeypatch.setattr(directory_proof, 'DirectoryReplays', inspectable_cache)

    # These container identities belong only to the invocation, not the mock's
    # reply list. Weak references prove they are reclaimed while the error and
    # its cause/tracebacks remain alive; checking map counts alone cannot.
    class Payload(dict):
        pass
    class Sequence(list):
        pass
    fetch = Acquisition.fetch
    def owned_payload(self, spec, subject, *args):
        body = fetch(self, spec, subject, *args)
        if subject == 'fund-etf':
            body = Payload(body)
            body['item'] = Sequence(body['item'])
            self.requests = Sequence(self.requests)
        return body
    monkeypatch.setattr(Acquisition, 'fetch', owned_payload)

    publish = CollectionRepository.publish
    def borrow_and_fail(data, requests, rows):
        borrowed = (data, requests, rows)
        assert borrowed[0]['item'] is borrowed[2]
        raise ValueError('isolated replay cause')
    def publication(self, dataset, subject, variant, **kwargs):
        result = publish(self, dataset, subject, variant, **kwargs)
        if subject == 'fund-etf' and kwargs['expected'] == 2 and failure != 'after_removal':
            try:
                borrow_and_fail(kwargs['data'], kwargs['requests'], kwargs['ticker_rows'])
            except ValueError as error:
                raise RuntimeError('isolated replay publication failure') from error
        return result
    monkeypatch.setattr(CollectionRepository, 'publish', publication)
    completed = monitor.completed
    def completed_or_fail(summary, **kwargs):
        if failure == 'after_removal' and summary.get('directory_reused'):
            assert entries[0].data is None and entries[0].requests is None
            raise RuntimeError('isolated replay progress failure')
        return completed(summary, **kwargs)
    monkeypatch.setattr(monitor, 'completed', completed_or_fail)
    emit = monitor.emit
    def emit_or_fail(**values):
        if 'directory_buffer_bytes' in values:
            progress_writes.append(values['directory_buffer_bytes'])
            if failure == 'cleanup_progress_write' and values['directory_buffer_bytes'] == 0:
                raise OSError('isolated cleanup progress write failure')
        return emit(**values)
    monkeypatch.setattr(monitor, 'emit', emit_or_fail)
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([dict(NEW), dict(OTHER_ETF)], timestamp=NEW_SNAPSHOT),
                                  reply([dict(OTHER_OTC)], timestamp=NEW_SNAPSHOT)]
    token = active_control.set(monitor)
    try:
        with pytest.raises(RuntimeError, match='isolated replay (publication|progress) failure') as captured:
            collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']),
                    client, engine, now=NOW + timedelta(days=1))
    finally:
        active_control.reset(token)
    assert captured.value.__traceback__ is not None
    if failure != 'after_removal':
        assert isinstance(captured.value.__cause__, ValueError)
        assert captured.value.__cause__.__traceback__ is not None
    assert len(created) == len(entries) == 1
    assert created[0].entries == {} and created[0].bytes == 0
    assert entries[0].data is None and entries[0].requests is None
    assert progress_writes[0] > 0 and progress_writes[-1] == 0
    assert monitor.detail['directory_buffer_bytes'] == 0
    gc.collect()
    assert len(payloads) == 3 and all(ref() is None for ref in payloads)
    assert client.request.call_count == 2
    with Session(engine) as session:
        if run_id is not None:
            saved_progress = session.get(TaskRun, run_id).collection_progress
            if failure == 'cleanup_progress_write':
                # A deliberately failed DB write cannot be called durable.
                # The monitor/supervisor have zero and retain the primary error.
                assert saved_progress['directory_buffer_bytes'] > 0
            else:
                assert saved_progress['directory_buffer_bytes'] == 0
        assert_originals(session, before)
        positive = session.get(State, ('tickers', 'fund-etf', 'default'))
        negative = session.get(State, ('tickers', 'fund-otc', 'default'))
        assert negative.status == 'succeeded' and negative.revision == 2
        current = session.get(Ticker, CODE)
        if failure == 'after_removal':
            assert positive.status == 'succeeded' and positive.revision == 3
            assert current.asset_type == 'fund-etf'
            original = session.get(Observation, positive.observation_id)
            assert original.row_count == 2
        else:
            assert positive.status == 'failed' and positive.revision == 2
            assert current.asset_type == 'fund-otc'
