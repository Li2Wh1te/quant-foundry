"""Schema-free E40 registry, proof freshness and cooperative rollback checks."""
from datetime import timedelta
import json,time
import pytest
from sqlalchemy import event,func,select
from sqlalchemy.orm import Session
from app.data_ingestion.tonghuashun.contracts import CollectionError,exact_json
from app.data_ingestion.tonghuashun.control import CollectionYield,CollectionControl,active_control
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.ticker_diagnostics import bounded_pair_rejection
from tests.test_tonghuashun_collections import NOW,State,Observation,Ticker,engine,ticker
from tests.test_tonghuashun_directory_reuse import publish_directory,assert_originals
from tests.test_tonghuashun_directory_mutual_blocking import A,NEW_A,OTHER_ETF,OTHER_OTC,NEW_SNAPSHOT,swap_fixture

def test_full_ticker_updates_use_bounded_lock_batches_and_preserve_first_seen(engine):
    rows = [ticker(f'{i:06d}.OF', 'fund-otc') for i in range(1000)]
    with Session(engine) as session:
        publish_directory(session, 'fund-otc', rows, snapshot=10)
    updated = [{**row, 'name': '本次真实目录更正'} for row in rows]
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('select') and 'tonghuashun_tickers' in statement:
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with Session(engine) as session:
            publish_directory(session, 'fund-otc', updated, snapshot=20, when=NOW + timedelta(days=1))
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) == 4
    with Session(engine) as session:
        tickers = list(session.scalars(select(Ticker)))
        assert len(tickers) == 1000 and all(row.name == '本次真实目录更正' for row in tickers)
        assert all(row.first_seen_at.replace(tzinfo=NOW.tzinfo) == NOW for row in tickers)

def test_cancellation_between_batches_rolls_back_already_flushed_ticker_changes(engine):
    rows = [ticker(f'{i:06d}.OF', 'fund-otc') for i in range(600)]
    with Session(engine) as session:
        publish_directory(session, 'fund-otc', rows, snapshot=10)
        before = {str(v.id): (v.data_json, v.request_json, v.content_hash)
                  for v in session.scalars(select(Observation))}
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) >= 2
    monitor = CollectionControl(engine, None)
    original_check = monitor.check
    def checked():
        if cancelled():
            raise CollectionYield('stopped')
        original_check()
    monitor.check = checked
    token = active_control.set(monitor)
    try:
        with Session(engine) as session:
            with pytest.raises(CollectionYield):
                publish_directory(session, 'fund-otc', [{**r, 'name': '未提交更正'} for r in rows],
                                  snapshot=20, when=NOW + timedelta(days=1))
            session.rollback()
    finally:
        active_control.reset(token)
    with Session(engine) as session:
        assert_originals(session, before)
        assert session.scalar(select(func.count()).select_from(Observation)) == len(before)
        assert all(t.name != '未提交更正' for t in session.scalars(select(Ticker)))

def test_deferred_batch_claim_is_refreshed_under_lock_before_classification(engine):
    swap_fixture(engine, 'fund-etf')
    with Session(engine) as session:
        publish_directory(session, 'fund-otc', [OTHER_OTC], snapshot=NEW_SNAPSHOT,
                          when=NOW + timedelta(days=1))
    with Session(engine) as session:
        cached = session.get(Ticker, A['thscode'])
        assert json.loads(cached.raw_json)['thscode'] == A['thscode']
        with Session(engine) as writer:
            writer.get(Ticker, A['thscode']).raw_json = exact_json({**A, 'name': '不匹配的真实旧声明'})
            writer.commit()
        previous = CollectionRepository(session).read('tickers', 'fund-etf', 'default')
        with pytest.raises(CollectionError):
            CollectionRepository(session).publish('tickers', 'fund-etf', 'default',
                expected=previous.revision, data={'timestamp': NEW_SNAPSHOT, 'item': [NEW_A, OTHER_ETF]},
                ticker_rows=[NEW_A, OTHER_ETF], requests=[{'interface': 'meta.tickers.list',
                    'parameters': {'asset_type': 'fund-etf', 'limit': 10000, 'offset': 0},
                    'request_id': 'fixture'}], now=NOW + timedelta(days=2), verify_category_change=True)
        session.rollback()
    with Session(engine) as session:
        assert session.get(Ticker, A['thscode']).asset_type == 'fund-otc'

def test_elapsed_diagnostic_stays_bounded_and_refuses_invalid_duration():
    result=bounded_pair_rejection((),None,elapsed_ms=12000)
    assert result['elapsed_ms']==12000 and result['publication_committed'] is False
    assert len(exact_json(result).encode())<=1792
    for invalid in (-1,True,2**63,'12000'):
        assert 'elapsed_ms' not in bounded_pair_rejection((),None,elapsed_ms=invalid)
