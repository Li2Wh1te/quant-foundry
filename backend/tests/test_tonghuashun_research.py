"""Research contracts and restartability checked with isolated provider data."""
from datetime import UTC, datetime, date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_tonghuashun_collections import engine, seed, ticker, bar, reply, NOW
from app.data_ingestion.clients.tonghuashun import TonghuashunResponse
from app.data_ingestion.models.tonghuashun import TonghuashunDumpImport as Import, TonghuashunDumpStage as Stage
from app.data_ingestion.tonghuashun.contracts import CollectionParameters as Params, CollectionError, DATASETS, date_ms
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.research_acquisition import Unit, news, pool
from app.data_ingestion.tonghuashun.research_service import boundary
from app.data_ingestion.tonghuashun.dump_service import reserve, stage_file, publish_batch, validate_file, download, DUMPS
from app.data_sources.tonghuashun_catalog import list_interfaces
from app.scheduling.registry import task_registry


@pytest.fixture
def research_engine(engine):
    Import.__table__.create(engine)
    Stage.__table__.create(engine)
    return engine


def client(response):
    return Mock(interval_ms=0,request=Mock(side_effect=response if callable(response) else None,return_value=response))


def calendar():
    return reply([{'date':d.strftime('%Y%m%d'),'date_ms':date_ms(d)} for d in (date(2026,9,10),date(2026,9,11),date(2026,9,14))])


def test_approved_scope_and_parameters():
    interfaces=list_interfaces()
    assert len([i for i in interfaces if i['ingestion_status']=='implemented'])==59
    assert len([i for i in interfaces if i['integration_scope']=='not_public'])==12
    assert len(DATASETS)==57
    for key in ('futures.prices.daily','fund.backtest.result','meta.tickers.search'):
        assert next(i for i in interfaces if i['key']==key)['integration_scope']=='excluded'
    for key in ('fund_offerings','dragon_tiger','stock_recent_dump'):
        props=task_registry.require('data.ths.'+key).parameters_model.model_json_schema()['properties']
        assert 'subjects' not in props and 'asset_types' not in props
    assert 'quota_tabs' in task_registry.require('data.ths.fund_quota_summary').parameters_model.model_fields
    with pytest.raises(ValueError):Params(quota_tabs=['bad tab'])


def test_quote_batches_missing_symbol_and_retry(engine):
    seed(engine,[ticker('600519.SH','a-share'),ticker('000001.SZ','a-share')])
    c=client(lambda key,p:calendar() if key=='a-share.calendar.trading-days' else reply([{'thscode':'600519.SH','last_price':Decimal('1.000000000000001')}]))
    with pytest.raises(CollectionError):collect('stock_quote',Params(),c,engine,now=NOW)
    with Session(engine) as session:
        repo=CollectionRepository(session)
        assert repo.read('stock_quote','600519.SH','default').status=='succeeded'
        assert repo.read('stock_quote','000001.SZ','default').status=='failed'
    quotes=[c for c in c.request.call_args_list if c.args[0]=='a-share.prices.snapshot']
    assert len(quotes)==1 and set(quotes[0].args[1]['thscodes'].split(','))=={'600519.SH','000001.SZ'}
    c=client(lambda key,p:calendar() if key=='a-share.calendar.trading-days' else reply([{'thscode':'000001.SZ'}]))
    result=collect('stock_quote',Params(),c,engine,now=NOW)
    assert result['succeeded']==1 and result['skipped']==1


def test_auction_readiness_and_nontrading_day(engine):
    seed(engine,[ticker('600519.SH','a-share')])
    c=client(lambda key,p:calendar() if key=='a-share.calendar.trading-days' else reply([{'thscode':'600519.SH'}],data_status='not_ready',auction_phase='final'))
    with pytest.raises(CollectionError):collect('stock_auction',Params(),c,engine,now=NOW)
    c.request.reset_mock()
    assert collect('stock_auction',Params(),c,engine,now=NOW.replace(day=13))['succeeded']==0
    assert c.request.call_count==1
    assert boundary(DATASETS['stock_auction'],Unit('600519.SH',{}),datetime(2026,9,14,9,25,tzinfo=UTC),'incremental') is None


def test_selected_only_requires_known_scope(engine):
    with pytest.raises(CollectionError,match='指定股票'):
        collect('rank_trend',Params(),client(reply([])),engine,now=NOW)
    seed(engine,[ticker('600519.SH','a-share')])
    c=client(lambda key,p:reply([{'thscode':'600519.SH','date':'2026-09-11','date_ms':date_ms(date(2026,9,11)),'rank':123}] if p['start_date'] <= '2026-09-11' <= p['end_date'] else []))
    assert collect('rank_trend',Params(subjects=['600519.SH']),c,engine,now=NOW)['succeeded']==1
    assert c.request.call_args.args[1]['end_date']=='2026-09-14'


def test_dragon_native_containers_and_explicit_trade_dates(engine):
    def response(key,p):
        if key=='a-share.calendar.trading-days':return calendar()
        return TonghuashunResponse({'trade_date':p['date'],'board_type':p['board_type'],'timestamp':date_ms(date.fromisoformat(p['date'])),
            'stock_items':[{'thscode':'600519.SH','range_days':1},{'thscode':'600519.SH','range_days':3}], 'hot_money_items':[]},'trace')
    c=client(response)
    r=collect('dragon_tiger',Params(batch_size=2),c,engine,now=NOW)
    assert r['succeeded']==2 and r['pending']==7
    with Session(engine) as session:
        data=CollectionRepository(session).read('dragon_tiger','2026-09-14.all','default').data
        assert len(data['item'][0]['stock_items'])==2
    assert all(call.args[1].get('date')!='2026-09-13' for call in c.request.call_args_list)


def test_pool_rejects_duplicate_and_truncated_pages():
    u=Unit('2026-09-11',{'date_ms':date_ms(date(2026,9,11))},date(2026,9,11));spec=DATASETS['limit_up']
    c=client(lambda key,p:reply([{'thscode':'600519.SH'}],pagination={'page':p['page'],'size':200,'total':2}))
    with pytest.raises(CollectionError,match='重复'):pool(Acquisition(c),spec,u)
    c=client(reply([],pagination={'page':1,'size':200,'total':0}))
    assert pool(Acquisition(c),spec,u)['item']==[]
    c=client(reply([],pagination={'page':1,'size':200,'total':1}))
    with pytest.raises(CollectionError):pool(Acquisition(c),spec,u)


def test_date_fallback_never_publishes(engine):
    c=client(reply([],date='2020-01-01',date_ms=date_ms(date(2020,1,1))))
    with pytest.raises(CollectionError):
        collect('hot_history',Params(start_date=date(2026,9,11),end_date=date(2026,9,11)),c,engine,now=NOW)


def test_manager_ranges_and_qdii_preserve_native_values(engine):
    with Session(engine) as session:
        CollectionRepository(session).publish('fund_profile','510300.SH','default',expected=0,
            data={'item':[{'manager_info':[{'manager_id':'m1'}]}]},requests=[],now=NOW)
        session.commit()
    c=client(reply([{'return_year':Decimal('1.23')}]))
    assert collect('fund_manager_performance',Params(),c,engine,now=NOW)['succeeded']==5
    assert {call.args[1]['range'] for call in c.request.call_args_list}=={'month','tmonth','year','nowyear','now'}
    c=client(lambda key,p:reply([{'name':json.loads(p['tab'])[0],'total_limit':'10690.00'}],provider_envelope='array'))
    assert collect('fund_quota_summary',Params(),c,engine,now=NOW)['succeeded']==2
    with Session(engine) as session:
        data=CollectionRepository(session).read('fund_quota_summary','nazhi100','default').data
        assert data['item'][0]['total_limit']=='10690.00'
        assert data['category_scope']=='explicit_categories_only'


def test_news_cursor_resume_and_initial_bound():
    spec=DATASETS['fund_news'];u=Unit('510300.SH',{'thscode':'510300.SH'})
    def response(key,p):
        n=int(p.get('offset','0'))
        return reply([{'id':str(n),'publish_time_ms':date_ms(date(2026,9,14)),'top':False}],has_more=True,offset=str(n+1))
    data=news(Acquisition(client(response)),spec,u,{'item':[],'last_sweep_date':'2026-09-13'},NOW)
    assert data['resume_cursor']=='25' and data['failed_requests']
    data2=news(Acquisition(client(lambda key,p:reply([{'id':'final','publish_time_ms':date_ms(date(2026,9,14))}],has_more=False,offset=None))),spec,u,data,NOW)
    assert data2['resume_cursor'] is None and len(data2['item'])==26
    initial=news(Acquisition(client(response)),spec,u,None,NOW)
    assert initial['scope_truncated'] and not initial['failed_requests']
    with pytest.raises(CollectionError):
        news(Acquisition(client(reply([{'id':'same','publish_time_ms':date_ms(date(2026,9,14))}],has_more=True,offset='same'))),spec,u,None,NOW)


def parquet_rows():
    return [{**bar(date(2026,9,11)), 'thscode':s,'currency':'CNY','interval':'1d','adjusted':'none'} for s in ('000001.SZ','600519.SH')]


def write_parquet(path,rows=None):
    pq.write_table(pa.Table.from_pylist(rows or parquet_rows()),path)


def test_parquet_whole_file_validation(tmp_path):
    path=tmp_path/'data.parquet';write_parquet(path)
    disk,meta=validate_file(path,'stock_recent_dump',tmp_path,NOW)
    assert meta['rows']==2
    assert '1.234567890123456789' in disk.execute('SELECT payload FROM records LIMIT 1').fetchone()[0]
    disk.close();(tmp_path/'rows.sqlite').unlink()
    write_parquet(path,parquet_rows()+parquet_rows())
    with pytest.raises(CollectionError,match='重复主键'):validate_file(path,'stock_recent_dump',tmp_path,NOW)


def test_dump_restart_and_newer_rest_protection(research_engine):
    p=Params(batch_size=1)
    generation,needed=reserve('stock_daily_dump',p,research_engine,NOW)
    assert needed and reserve('stock_daily_dump',p,research_engine,NOW)[0] is None
    def downloader(url,path):
        assert url=='https://objects.example/file?secret=hidden'
        write_parquet(path)
        return hashlib.sha256(Path(path).read_bytes()).hexdigest(),Path(path).stat().st_size
    c=client(TonghuashunResponse({'presigned_url':'https://objects.example/file?secret=hidden','dump_id':DUMPS['stock_daily_dump'][0]},'trace'))
    stage_file('stock_daily_dump',generation,c,research_engine,NOW,downloader)
    same,needed=reserve('stock_daily_dump',p,research_engine,NOW+timedelta(seconds=1))
    assert same==generation and not needed
    with Session(research_engine) as session:
        CollectionRepository(session).publish('stock_daily','000001.SZ','default',expected=0,
            data={'item':[bar(date(2026,9,11),'9.99')]},requests=[],now=NOW+timedelta(seconds=2))
        session.commit()
    r=publish_batch('stock_daily_dump',generation,p,research_engine,NOW+timedelta(seconds=3))
    assert r['pending']==1 and r['superseded']==1
    assert publish_batch('stock_daily_dump',generation,p,research_engine,NOW+timedelta(seconds=4))['pending']==0
    with Session(research_engine) as session:
        repo=CollectionRepository(session)
        assert repo.read('stock_daily','000001.SZ','default').data['item'][0]['close_price']==Decimal('9.99')
        assert repo.read('stock_daily','600519.SH','default').data['item'][0]['close_price']==Decimal('1.234567890123456789')
        assert not repo.read('stock_daily_dump','market','default').data['failed_requests']
        assert 'hidden' not in session.get(Import,'stock_daily_dump').metadata_json
        assert not list(session.scalars(select(Stage)))
    assert reserve('stock_daily_dump',p,research_engine,NOW+timedelta(days=1))[0] is None


def test_bad_staging_never_publishes(research_engine):
    generation,_=reserve('stock_daily_dump',Params(),research_engine,NOW)
    def downloader(url,path):
        pq.write_table(pa.table({'bad':[1]}),path)
        return 'digest',1
    c=client(TonghuashunResponse({'presigned_url':'https://objects.example/file'},None))
    with pytest.raises(CollectionError):stage_file('stock_daily_dump',generation,c,research_engine,NOW,downloader)
    with Session(research_engine) as session:
        assert not list(session.scalars(select(Stage)))
        assert CollectionRepository(session).read('stock_daily','600519.SH','default').data is None


@pytest.mark.parametrize('url',['http://objects.example/a','https://user:password@example.com/a','https://127.0.0.1/a','https://[::1]/a','https://example.com:444/a'])
def test_download_rejects_unsafe_destinations(url,tmp_path):
    with pytest.raises(CollectionError):download(url,tmp_path/'never')


def test_short_history_fund_can_skip_empty_early_windows(engine):
    seed(engine,[ticker()])
    def response(key,p):
        day=date(2026,9,14)
        rows=[{'date_ms':date_ms(day),'rsi_pct':Decimal('53.8000')}] if p['start']<=date_ms(day)<=p['end'] else []
        return reply(rows)
    result=collect('fund_performance_history',Params(),client(response),engine,now=NOW.replace(hour=15))
    assert result['succeeded']==1
    with Session(engine) as session:
        data=CollectionRepository(session).read('fund_performance_history','510300.SH','default').data
        assert data['item'][0]['rsi_pct']==Decimal('53.8000')
        assert data['requested_end']=='2026-09-14'


def test_qdii_transport_accepts_only_documented_array_envelopes(monkeypatch):
    from tests.test_tonghuashun import response
    from app.data_ingestion.clients import tonghuashun as transport
    monkeypatch.setattr(transport,'_gate',transport._RequestGate())
    monkeypatch.setattr(transport.requests,'get',lambda *a,**k:response({'code':0,'data':[{'name':'nazhi100','total_limit':'1.00'}]}))
    c=transport.TonghuashunClient('https://example.test','fixture-key',interval_ms=0,max_attempts=1)
    assert c.request('fund.quota.summary',{'tab':'["nazhi100"]'}).data['item'][0]['total_limit']=='1.00'
    with pytest.raises(transport.TonghuashunError):c.request('fund.performance.returns',{})


def test_download_pins_address_without_api_credentials_and_checks_length(monkeypatch,tmp_path):
    from app.data_ingestion.tonghuashun import dump_service as mod
    monkeypatch.setattr(mod.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
    connected=[]
    response=Mock(status=200,headers={'Content-Length':'3'},read1=Mock(side_effect=[b'abc',b'']))
    conn=Mock(getresponse=Mock(return_value=response))
    monkeypatch.setattr(mod.http.client,'HTTPSConnection',lambda *a,**k:connected.append((a,k)) or conn)
    digest,size=download('https://objects.example/a?signature=secret',tmp_path/'file')
    assert size==3 and digest==hashlib.sha256(b'abc').hexdigest()
    assert connected[0][0][0]=='objects.example'
    assert conn.request.call_args.kwargs['headers']=={'Accept':'application/octet-stream'}
    conn.close.assert_called_once()
    response.read1.side_effect=[b'x',b'']
    with pytest.raises(CollectionError,match='不完整'):download('https://objects.example/a',tmp_path/'bad')
    response.status=302
    with pytest.raises(CollectionError):download('https://objects.example/a',tmp_path/'redirect')


def test_new_migration_round_trip_preserves_existing_tables():
    import importlib
    from sqlalchemy import create_engine,inspect,text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    m=importlib.import_module('app.db.migrations.versions.20260920_01_tonghuashun_dump_staging')
    db=create_engine('sqlite://')
    with db.begin() as connection:
        connection.execute(text('CREATE TABLE existing (id INTEGER)'))
        connection.execute(text('INSERT INTO existing VALUES (1)'))
        with Operations.context(MigrationContext.configure(connection)):
            m.upgrade()
            assert 'tonghuashun_dump_stages' in inspect(connection).get_table_names()
            m.downgrade()
        assert connection.execute(text('SELECT id FROM existing')).scalar_one()==1
    db.dispose()


@pytest.mark.parametrize('dataset', ['stock_quote', 'fund_profile'])
def test_concurrent_publication_is_deferred_without_invalidating_winner(engine, dataset):
    code = '600519.SH' if dataset == 'stock_quote' else '510300.SH'
    seed(engine, [ticker(code, 'a-share' if dataset == 'stock_quote' else 'fund-etf')])
    winner = {'timestamp': 1720000000000, 'item': [{'thscode': code, 'name': 'newer'}]}

    def respond(key, parameters):
        if key == 'a-share.calendar.trading-days':
            return calendar()
        # Another session wins while the first collector is awaiting I/O.
        with Session(engine) as session:
            CollectionRepository(session).publish(dataset, code, 'default', expected=0,
                data=winner, requests=[], now=NOW)
            session.commit()
        return reply([{'thscode': code, 'name': 'older'}])

    result = collect(dataset, Params(), client(respond), engine, now=NOW)
    assert result['failed'] == result['succeeded'] == 0
    assert result['skipped'] == result['pending'] == 1
    with Session(engine) as session:
        state = CollectionRepository(session).read(dataset, code, 'default')
        assert state.status == 'succeeded'
        assert state.data == winner
        assert state.revision == 1


def test_download_falls_back_only_within_validated_public_addresses(monkeypatch, tmp_path):
    from app.data_ingestion.tonghuashun import dump_service as mod
    dns = Mock(return_value=[(10, 1, 6, '', ('2606:4700:4700::1111', 443)),
                             (2, 1, 6, '', ('8.8.8.8', 443)),
                             (2, 1, 6, '', ('1.1.1.1', 443))])
    monkeypatch.setattr(mod.socket, 'getaddrinfo', dns)
    connect = Mock(side_effect=[OSError('unreachable'), Mock()])
    monkeypatch.setattr(mod.socket, 'create_connection', connect)
    response = Mock(status=200, headers={'Content-Length': '3'},
                    read1=Mock(side_effect=[b'abc', b'']))
    conn = Mock(getresponse=Mock(return_value=response))
    conn.request.side_effect = lambda *a, **kw: conn._create_connection(('objects.example', 443), 20)
    monkeypatch.setattr(mod.http.client, 'HTTPSConnection', lambda *a, **kw: conn)
    assert download('https://objects.example/a', tmp_path / 'file')[1] == 3
    assert [c.args[0] for c in connect.call_args_list] == [('1.1.1.1', 443), ('8.8.8.8', 443)]
    dns.assert_called_once()


def test_etf_quotes_use_singular_code_and_preserve_each_subject(engine):
    seed(engine, [ticker('510300.SH'), ticker('510500.SH')])
    def respond(interface, parameters):
        if interface == 'a-share.calendar.trading-days':
            return calendar()
        assert interface == 'fund.market.snapshot'
        assert set(parameters) == {'thscode'}
        assert ',' not in parameters['thscode']
        return reply([{'thscode': parameters['thscode'], 'last_price': Decimal('1.23456789')}])
    c = client(respond)
    result = collect('etf_quote', Params(), c, engine, now=NOW)
    assert result['succeeded'] == 2 and result['failed'] == 0
    calls = [call for call in c.request.call_args_list if call.args[0] == 'fund.market.snapshot']
    assert [call.args[1] for call in calls] == [{'thscode': '510300.SH'}, {'thscode': '510500.SH'}]
    with Session(engine) as session:
        for code in ('510300.SH', '510500.SH'):
            data = CollectionRepository(session).read('etf_quote', code, 'default').data
            assert data['item'][0]['thscode'] == code
