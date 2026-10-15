"""E40 bounded directory proof/reuse fixtures; never a supplier acceptance."""
from datetime import timedelta
import hashlib
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.scheduling.registry import task_registry
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit as WorkUnit
from app.data_ingestion.scheduler_tasks import tonghuashun as scheduler
from app.data_ingestion.tonghuashun.directory_proof import DirectoryAcquisition as Acquisition
from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, CollectionParameters, DATASETS, content_hash, exact_json,
)
from app.data_ingestion.tonghuashun.control import CollectionControl, active_control
from app.data_ingestion.tonghuashun.directory_proof import DirectoryReplays, complete_directory
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect
from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_ticker_conflict
from tests.test_tonghuashun_collections import NOW, Observation, State, Ticker, bar, engine, reply, ticker

CODE = '158025.SZ'
OLD_SNAPSHOT = 1789989829624
NEW_SNAPSHOT = 1791619222079
OLD = ticker(CODE, 'fund-otc', name='创业板成长ETF鹏华', exchange='SZ')
NEW = {**OLD, 'asset_type': 'fund-etf'}
OTHER_ETF = ticker('510300.SH')
OTHER_OTC = ticker('000001.OF', 'fund-otc')


def receipts(asset, count=1):
    return [{'interface': 'meta.tickers.list', 'parameters': {
        'asset_type': asset, 'limit': 10000, 'offset': index * 10000},
        'request_id': 'fixture'} for index in range(count)]


def publish_directory(session, asset, rows, *, snapshot=OLD_SNAPSHOT, when=NOW,
                      requests=None, metadata=None):
    repository = CollectionRepository(session)
    previous = repository.read('tickers', asset, 'default')
    data = {'timestamp': snapshot, 'item': rows, **(metadata or {})}
    result = repository.publish('tickers', asset, 'default', expected=previous.revision,
        data=data, requests=receipts(asset) if requests is None else requests,
        now=when, ticker_rows=rows)
    session.commit()
    return result


def baseline(engine, other_rows=None):
    WorkUnit.__table__.create(engine)
    with Session(engine) as session:
        otc = publish_directory(session, 'fund-otc', [OLD, *(other_rows or [OTHER_OTC])])
        etf = publish_directory(session, 'fund-etf', [OTHER_ETF])
        repository = CollectionRepository(session)
        protected = [otc['version_id'], etf['version_id']]
        for dataset, data in (
            ('fund_profile', {'timestamp': 1791187222170, 'item': [{'thscode': CODE, 'ticker': '158025',
                'fund_name': OLD['name'], 'estab_date': 1789488000000}]}),
            ('fund_nav', {'timestamp': 1791276022087, 'item': [{'nav_date': 1791187200000, 'unit_nav': 1}]}),
        ):
            result = repository.publish(dataset, CODE, 'default', expected=0, data=data,
                requests=[], now=NOW + timedelta(seconds=1))
            protected.append(result['version_id'])
        session.commit()
        before = {str(row.id): (row.data_json, row.request_json, row.content_hash)
                  for row in session.scalars(select(Observation))}
    return before


def assert_originals(session, before):
    after = {str(row.id): (row.data_json, row.request_json, row.content_hash)
             for row in session.scalars(select(Observation))}
    assert all(after[identity] == original for identity, original in before.items())


def handler(engine, monkeypatch, client, asset, *, requests=1):
    monkeypatch.setattr(scheduler, 'get_engine', lambda: engine)
    monkeypatch.setattr(scheduler.TonghuashunClient, 'from_settings', lambda _: client)
    monkeypatch.setattr(scheduler, 'get_settings', lambda: None)
    return scheduler.execute('tickers', Mock(run_id=None), CollectionParameters(
        asset_types=[asset] if isinstance(asset, str) else asset, max_requests=requests, max_seconds=180))


def test_budget_between_directory_pages_refetches_without_a_checkpoint(engine, monkeypatch):
    before = baseline(engine)
    page = [ticker(f'{index:06d}.OF', 'fund-otc') for index in range(10000)]
    client = Mock(interval_ms=0)
    client.request.return_value = reply(page, timestamp=NEW_SNAPSHOT)
    result = handler(engine, monkeypatch, client, 'fund-otc')
    assert result['yield_reason'] == 'budget'
    assert result['collection_progress']['requests'] == 1
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.scalar(select(func.count()).select_from(WorkUnit)) == 0
        assert session.get(State, ('tickers', 'fund-otc', 'default')).revision == 1
    # A later normal batch makes a fresh request at offset zero, not a replay.
    client.request.side_effect = [reply(page, timestamp=NEW_SNAPSHOT), reply([OLD], timestamp=NEW_SNAPSHOT)]
    second = handler(engine, monkeypatch, client, 'fund-otc', requests=2)
    assert second['succeeded'] == 1
    assert [call.args[1]['offset'] for call in client.request.call_args_list] == [0, 0, 10000]






def test_snapshot_restart_keeps_only_successful_contiguous_receipts():
    page = [ticker(f'{index:06d}.SH') for index in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply(page, timestamp=1), reply([], timestamp=2),
                                  reply([NEW], timestamp=3)]
    acquisition = Acquisition(client)
    data = acquisition.directory(DATASETS['tickers'], 'fund-etf')
    assert acquisition.fetched_count == 10001 and client.request.call_count == 3
    assert [record['parameters']['offset'] for record in acquisition.requests] == [0]
    assert complete_directory('fund-etf', data, acquisition.requests, NOW).codes == frozenset([CODE])


@pytest.mark.parametrize('metadata', [
    {'complete': False}, {'has_more': True}, {'next_cursor': 'next'}, {'failed_requests': [{'error': 'fixture'}]},
])
def test_incomplete_terminal_page_never_publishes_or_retains_candidate(engine, metadata):
    before = baseline(engine)
    client = Mock(interval_ms=0)
    client.request.return_value = reply([NEW], timestamp=NEW_SNAPSHOT, **metadata)
    with pytest.raises(CollectionError, match='部分失败'):
        collect('tickers', CollectionParameters(asset_types=['fund-etf']), client, engine, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        assert session.scalar(select(func.count()).select_from(Observation)) == len(before)
        assert session.scalar(select(func.count()).select_from(WorkUnit)) == 0


def test_duplicate_page_never_advances_current_or_creates_checkpoint(engine):
    before = baseline(engine)
    page = [ticker(f'{index:06d}.SH') for index in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply(page, timestamp=NEW_SNAPSHOT), reply([page[0]], timestamp=NEW_SNAPSHOT)]
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf']), client, engine, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        assert session.scalar(select(func.count()).select_from(Observation)) == len(before)


@pytest.mark.parametrize('defect', [
    'different_snapshot', 'duplicate_membership', 'missing_receipt', 'incomplete_negative',
    'wrong_old_row', 'unordered_absence', 'ambiguous_old_original', 'regressing_snapshot', 'wrong_negative_scope',
])
def test_unproven_category_change_keeps_old_identity_and_originals(engine, defect):
    before = baseline(engine)
    with Session(engine) as session:
        snapshot = NEW_SNAPSHOT + 1 if defect == 'different_snapshot' else NEW_SNAPSHOT
        when = NOW if defect == 'unordered_absence' else NOW + timedelta(seconds=2)
        rows = [OLD, OTHER_OTC] if defect == 'duplicate_membership' else [OTHER_OTC]
        publish_directory(session, 'fund-otc', rows, snapshot=snapshot, when=when,
            requests=[] if defect == 'missing_receipt' else None,
            metadata={'complete': False} if defect == 'incomplete_negative' else None)
        if defect == 'wrong_old_row':
            session.get(Ticker, CODE).raw_json = exact_json({**OLD, 'name': 'unproven local replacement'})
        if defect == 'wrong_negative_scope':
            foreign = publish_directory(session, 'fund-etf', [OTHER_ETF], snapshot=NEW_SNAPSHOT,
                                        when=NOW + timedelta(seconds=2))
            from uuid import UUID
            session.get(State, ('tickers', 'fund-otc', 'default')).observation_id = UUID(foreign['version_id'])
        if defect == 'ambiguous_old_original':
            original = session.scalar(select(Observation).where(Observation.subject == 'fund-otc',
                                                               Observation.observed_at == NOW))
            session.add(Observation(dataset=original.dataset, subject=original.subject, variant=original.variant,
                observed_at=original.observed_at, request_json=original.request_json, data_json=original.data_json,
                content_hash=original.content_hash, row_count=original.row_count, chain_depth=0))
        session.commit()
        expected_etf_revision = session.get(State, ('tickers', 'fund-etf', 'default')).revision
    client = Mock(interval_ms=0)
    client.request.return_value = reply([NEW, OTHER_ETF], timestamp=OLD_SNAPSHOT if defect == 'regressing_snapshot' else NEW_SNAPSHOT)
    with pytest.raises(CollectionError, match='部分失败'):
        collect('tickers', CollectionParameters(asset_types=['fund-etf']), client, engine, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        state = session.get(State, ('tickers', 'fund-etf', 'default'))
        assert state.status == 'failed' and state.revision == expected_etf_revision + 1
    assert client.request.call_count == 1


def test_same_snapshot_migration_reuses_validated_directory_and_protects_2551_objects(engine, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    other_rows = [ticker(f'{index:06d}.OF', 'fund-otc', name=f'protected-{index}') for index in range(2551)]
    before = baseline(engine, other_rows)
    # A separate current-partition witness is never an ingestion write target.
    witness = tmp_path / 'undated.b13.parquet'
    pq.write_table(pa.Table.from_pylist([OLD, *other_rows]), witness)
    witness_hash = hashlib.sha256(witness.read_bytes()).hexdigest()
    with Session(engine) as session:
        old_identity = session.get(Ticker, CODE).first_seen_at
        protected_states = {key: session.get(State, (key, CODE, 'default')).observation_id
                            for key in ('fund_profile', 'fund_nav')}
    client = Mock(interval_ms=0)
    client.request.side_effect = lambda interface, parameters: reply(
        [NEW, OTHER_ETF] if parameters['asset_type'] == 'fund-etf' else other_rows, timestamp=NEW_SNAPSHOT)
    monitor = CollectionControl(engine, None, max_requests=2)
    token = active_control.set(monitor)
    try:
        result = collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']), client, engine, now=NOW + timedelta(days=1))
    finally:
        active_control.reset(token)
    assert result['succeeded'] == 2 and result['failed'] == 0 and result['directory_reused'] == 1
    assert monitor.detail['requests'] == 2 and monitor.detail['directory_reused'] == 1
    assert monitor.detail['directory_buffer_bytes'] == 0
    assert client.request.call_count == 2
    with Session(engine) as session:
        changed = session.get(Ticker, CODE)
        assert changed.thscode == CODE and changed.first_seen_at == old_identity
        assert changed.asset_type == 'fund-etf' and changed.raw_json == exact_json(NEW)
        assert_originals(session, before)
        for row in other_rows:
            kept = session.get(Ticker, row['thscode'])
            assert kept.asset_type == 'fund-otc' and kept.raw_json == exact_json(row)
        assert all(session.get(State, (key, CODE, 'default')).observation_id == value
                   for key, value in protected_states.items())
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 3
        assert session.scalar(select(func.count()).select_from(WorkUnit)) == 0
    assert hashlib.sha256(witness.read_bytes()).hexdigest() == witness_hash


def test_newer_source_revision_blocks_pending_replay_without_extra_supplier_request(engine):
    before = baseline(engine)
    client = Mock(interval_ms=0)
    def response(interface, parameters):
        if parameters['asset_type'] == 'fund-etf':
            return reply([NEW, OTHER_ETF], timestamp=NEW_SNAPSHOT)
        with Session(engine) as session:
            session.get(State, ('tickers', 'fund-etf', 'default')).revision += 1
            session.commit()
        return reply([OTHER_OTC], timestamp=NEW_SNAPSHOT)
    client.request.side_effect = response
    with pytest.raises(CollectionError):
        collect('tickers', CollectionParameters(asset_types=['fund-etf', 'fund-otc']), client, engine, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 3
    assert client.request.call_count == 2


def test_replay_admission_is_bounded_and_requires_complete_new_absence(monkeypatch):
    from app.data_ingestion.tonghuashun import directory_proof as proof
    data = {'timestamp': NEW_SNAPSHOT, 'item': [NEW]}
    diagnostic = bounded_ticker_conflict('fund-etf', {
        'source_code': CODE, 'old_asset_type': 'fund-otc', 'new_asset_type': 'fund-etf',
        'provider_snapshot': NEW_SNAPSHOT, 'directory_content_hash': content_hash(data)})
    replays = DirectoryReplays()
    monkeypatch.setattr(proof, 'CONTEXT_BYTES', 2)
    assert not replays.retain('fund-etf', 2, data, receipts('fund-etf'), NOW, 1, diagnostic)
    assert replays.bytes == 0 and replays.entries == {}
    monkeypatch.undo()
    assert replays.retain('fund-etf', 2, data, receipts('fund-etf'), NOW, 1, diagnostic)
    assert not replays.retain('fund-etf', 2, data, receipts('fund-etf'), NOW, 1, diagnostic)
    for metadata in ({'timestamp': NEW_SNAPSHOT + 1}, {'complete': False}):
        assert not replays.ready('fund-otc', {'timestamp': NEW_SNAPSHOT, 'item': [OTHER_OTC], **metadata},
                                 receipts('fund-otc'), NOW)
    assert not replays.ready('fund-otc', {'timestamp': NEW_SNAPSHOT, 'item': [OLD]}, receipts('fund-otc'), NOW)
    assert replays.ready('fund-otc', {'timestamp': NEW_SNAPSHOT, 'item': [OTHER_OTC]}, receipts('fund-otc'), NOW)
    data['item'][0]['name'] = 'changed after validation'
    assert not replays.ready('fund-otc', {'timestamp': NEW_SNAPSHOT, 'item': [OTHER_OTC]}, receipts('fund-otc'), NOW)


@pytest.mark.parametrize('asset, code', [('fund-etf', CODE), ('fund-reits', '180307.SZ')])
def test_committed_same_snapshot_absence_allows_single_normal_publication(engine, asset, code):
    before = baseline(engine)
    if code != CODE:
        old = {**OLD, 'thscode': code}
        with Session(engine) as session:
            publish_directory(session, 'fund-otc', [OLD, old, OTHER_OTC], when=NOW + timedelta(seconds=1))
    with Session(engine) as session:
        publish_directory(session, 'fund-otc', [OTHER_OTC], snapshot=NEW_SNAPSHOT,
                          when=NOW + timedelta(seconds=2))
    client = Mock(interval_ms=0)
    new = {**NEW, 'thscode': code, 'asset_type': asset}
    client.request.return_value = reply([new], timestamp=NEW_SNAPSHOT)
    result = collect('tickers', CollectionParameters(asset_types=[asset]), client, engine,
                     now=NOW + timedelta(days=1))
    assert result['succeeded'] == 1 and client.request.call_count == 1
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, code).asset_type == asset


def test_counterpart_head_lock_rechecks_a_concurrent_change(engine):
    if engine.dialect.name != 'postgresql':
        pytest.skip('actual PostgreSQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from sqlalchemy import text
    baseline(engine)
    with Session(engine) as session:
        former = session.get(State, ('tickers', 'fund-otc', 'default')).observation_id
        publish_directory(session, 'fund-otc', [OTHER_OTC], snapshot=NEW_SNAPSHOT,
                          when=NOW + timedelta(seconds=2))
    started = threading.Event()
    worker_pid = []
    def candidate():
        with Session(engine) as session:
            worker_pid.append(session.scalar(text('SELECT pg_backend_pid()')))
            started.set()
            with pytest.raises(CollectionError):
                CollectionRepository(session).publish('tickers', 'fund-etf', 'default', expected=1,
                    data={'timestamp': NEW_SNAPSHOT, 'item': [NEW]}, requests=receipts('fund-etf'),
                    now=NOW + timedelta(seconds=3), ticker_rows=[NEW], verify_category_change=True)
            session.rollback()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as holder:
            held = holder.scalar(select(State).where(State.dataset == 'tickers',
                State.subject == 'fund-otc', State.variant == 'default').with_for_update())
            future = executor.submit(candidate)
            assert started.wait(3)
            waiting = False
            for _ in range(60):
                with engine.connect() as observer:
                    waiting = observer.scalar(text('SELECT wait_event_type = :kind FROM pg_stat_activity WHERE pid=:pid'),
                                              {'kind': 'Lock', 'pid': worker_pid[0]})
                if waiting:
                    break
                time.sleep(.05)
            assert waiting is True, 'candidate must wait for its exact counterpart head'
            held.observation_id = former  # The visible newer proof changes while the candidate waits.
            holder.commit()
        future.result(timeout=5)
    with Session(engine) as session:
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 1


def test_opposite_fund_publications_use_one_lock_order(engine):
    if engine.dialect.name != 'postgresql':
        pytest.skip('actual PostgreSQL row locks required')
    from concurrent.futures import ThreadPoolExecutor
    import threading
    baseline(engine)
    barrier = threading.Barrier(2)
    def publish(asset, rows):
        barrier.wait(timeout=3)
        with Session(engine) as session:
            result = CollectionRepository(session).publish('tickers', asset, 'default', expected=1,
                data={'timestamp': NEW_SNAPSHOT, 'item': rows}, requests=receipts(asset),
                now=NOW + timedelta(seconds=2), ticker_rows=rows, verify_category_change=True)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(publish, 'fund-etf', [OTHER_ETF])
        b = executor.submit(publish, 'fund-otc', [OLD, OTHER_OTC])
        assert a.result(timeout=5)['received'] == 1
        assert b.result(timeout=5)['received'] == 2
    with Session(engine) as session:
        assert session.get(State, ('tickers', 'fund-etf', 'default')).revision == 2
        assert session.get(State, ('tickers', 'fund-otc', 'default')).revision == 2


def test_reused_directory_preserves_observation_time_and_actual_publication_time(engine):
    baseline(engine)
    captured = NOW + timedelta(seconds=3)
    committed = NOW + timedelta(seconds=4)
    with Session(engine) as session:
        publish_directory(session, 'fund-otc', [OTHER_OTC], snapshot=NEW_SNAPSHOT,
                          when=NOW + timedelta(seconds=2))
        result = CollectionRepository(session).publish('tickers', 'fund-etf', 'default', expected=1,
            data={'timestamp': NEW_SNAPSHOT, 'item': [NEW]}, requests=receipts('fund-etf'),
            now=committed, observed_at=captured, ticker_rows=[NEW], verify_category_change=True)
        session.commit()
        state = session.get(State, ('tickers', 'fund-etf', 'default'))
        observation = session.get(Observation, state.observation_id)
        from app.data_ingestion.tonghuashun.directory_proof import aware
        assert aware(state.attempted_at) == committed and aware(state.succeeded_at) == committed
        assert aware(observation.observed_at) == captured
        assert aware(session.get(Ticker, CODE).last_seen_at) == captured


def test_budget_before_counterpart_completion_releases_ephemeral_candidate(engine, monkeypatch):
    before = baseline(engine)
    page = [ticker(f'{index:06d}.OF', 'fund-otc') for index in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([NEW, OTHER_ETF], timestamp=NEW_SNAPSHOT),
                                  reply(page, timestamp=NEW_SNAPSHOT)]
    result = handler(engine, monkeypatch, client, ['fund-etf', 'fund-otc'], requests=2)
    assert result['yield_reason'] == 'budget' and result['collection_progress']['failed'] == 1
    assert result['collection_progress']['directory_buffer_bytes'] == 0
    assert client.request.call_count == 2
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.get(Ticker, CODE).asset_type == 'fund-otc'
        assert session.get(State, ('tickers', 'fund-otc', 'default')).revision == 1
        assert session.scalar(select(func.count()).select_from(Observation)) == len(before)
        assert session.scalar(select(func.count()).select_from(WorkUnit)) == 0
