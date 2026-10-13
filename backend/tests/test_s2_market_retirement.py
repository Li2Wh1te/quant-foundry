"""D12: default synthetic formal chain and authenticated legacy retirement.

No source/provider/service success is injected; production resources and original collection/shared persistence are untouched.
"""
from dataclasses import replace
from datetime import date
import pytest
from sqlalchemy import event, text
from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.data_ingestion.repositories.etf_detail import EtfDetailQueryRepository
from app.data_store.adapters.registry import BY_ID
from app.data_store.readers import Query
from app.data_store.pipeline import run_entry
from tests.test_data_store_domain_samples import sample
from tests.test_s2_market_read import market, HEADERS, database_schema  # noqa: F401
from tests.test_s2_market_series import identity, seed
from tests.test_s2_market_adjustments import seed_factors
from tests.test_data_store_kernel import database, limits, store  # noqa: F401
from tests.test_data_store_local_pipeline import Inputs, ready  # noqa: F401


def test_default_combined_formal_chain_reads_business_counts_with_old_market_repositories_blocked(market, monkeypatch):
    client, current, service = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 6))
    seed_factors(current, [('2026-02-06', '2')])
    binding = service.bindings['etf-current']
    # Same physical dataset also holds a deliberately unselected formal variant.
    other = replace(sample('E67'), variant='unselected-d12-fixture')
    other = replace(other, content={**other.content, 'ts_code': '510300.SH'})
    assert run_entry(current, BY_ID['E67'], Inputs(other))['qualified']
    physical = current.read(binding.entry.spec, Query(partitions=tuple(sorted({binding.partition('510300.SH', '510300.SH'), replace(binding, variant='unselected-d12-fixture').partition('510300.SH', '510300.SH')})),
        page_size=1000, require_qualified=True))
    assert len(physical.rows) == 2 and sum(row['representation'] == binding.representation for row in physical.rows) == 1
    with current.catalog.engine.connect() as connection:
        assert connection.execute(text("SELECT enabled FROM data_source_configs WHERE key='tushare'")).scalar_one() is False

    def deny(*_args, **_kwargs):
        raise AssertionError('legacy market repository blocked')
    for repository in (EtfQueryRepository, EtfDetailQueryRepository):
        for name in ('list_codes', 'overview', 'list_days', 'get_day', 'get_code', 'list_daily_bars', 'list_adjustment_factors'):
            if hasattr(repository, name):
                monkeypatch.setattr(repository, name, deny)
    forbidden = []
    def block(_conn, _cursor, statement, _params, _context, _many):
        if any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars', 'etf_adjustment_factors', 'trading_calendar_days', 'data_source_configs', 'backtesting_accounts')):
            forbidden.append(statement)
            raise AssertionError('legacy collection/execution SQL')
    event.listen(current.catalog.engine, 'before_cursor_execute', block)
    try:
        paths = [
            '/overview?catalog_key=etf-current', '/instruments?catalog_key=etf-current',
            '/calendar?exchange=SSE&start=2026-01-26&end=2026-02-06',
            '/instruments/resolve?symbol=510300.SH&asset_type=etf',
            '/instruments/' + key, '/instruments/' + key + '/series',
            '/instruments/' + key + '/bars?end=2026-02-06',
            '/instruments/' + key + '/factors?start=2026-01-26&end=2026-02-06',
        ]
        responses = [client.get('/api/admin/market' + path, headers=HEADERS) for path in paths]
        assert all(response.status_code == 200 for response in responses), [(response.status_code, response.text) for response in responses]
        summary, catalog = responses[0].json(), responses[1].json()
        assert summary['total_instruments'] == catalog['total'] == len(catalog['items']) == 1
        assert summary['meta']['business_as_of'] is None and summary['meta']['coverage']['missing_dates'] is None
        assert summary['listed_count'] is None
        assert len(responses[6].json()['items']) == 10
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', block)
    assert not forbidden


@pytest.mark.parametrize('path', [
    '/trading-calendar', '/trading-calendar/overview', '/trading-calendar/SSE/2026-01-02',
    '/etfs/overview', '/etfs/510300.SH', '/etfs/510300.SH/daily-bars', '/etfs/510300.SH/adjustment-factors',
])
def test_legacy_market_gets_authenticated_410_without_a_database_read(market, path):
    client, current, _service = market
    seen = []
    def deny(_conn, _cursor, statement, _params, _context, _many):
        seen.append(statement)
        raise AssertionError('retired market path must not touch the database')
    event.listen(current.catalog.engine, 'before_cursor_execute', deny)
    try:
        url = '/api/admin/data-collections' + path
        assert client.get(url).status_code == 401
        response = client.get(url, headers=HEADERS)
        assert response.status_code == 410 and response.json()['detail']['code'] == 'LEGACY_MARKET_RETIRED'
        assert response.json()['detail']['replacement'] == '/api/admin/market'
        assert 'items' not in response.json() and not seen
    finally:
        event.remove(current.catalog.engine, 'before_cursor_execute', deny)


def test_failed_local_update_keeps_proven_formal_history_readable_without_claiming_coverage(market):
    from app.data_store.adapters.canonical import NativeInputError
    client, current, _service = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 6))
    path = '/api/admin/market/instruments/' + key + '/bars?end=2026-02-06'
    before = client.get(path, headers=HEADERS).json()
    class Interrupted(Inputs):
        def iter_entry(self, entry):
            raise NativeInputError('SOURCE_BUDGET_EXCEEDED', 'artificial interrupted local scan')
            yield  # This test source never supplies a successful query result.
    with pytest.raises(NativeInputError):
        run_entry(current, BY_ID['E70'], Interrupted())
    after = client.get(path, headers=HEADERS)
    assert after.status_code == 200, after.text
    assert after.json()['items'] == before['items']
    assert after.json()['meta']['coverage']['verified'] is False


@pytest.mark.skipif(__import__('os').getenv('QF_D12_BROWSER') != '1', reason='explicit owned Chromium/frontend build required')
def test_real_default_formal_browser_chain_with_legacy_reads_blocked(market):
    import http.client
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import os
    from datetime import datetime, timezone
    from decimal import Decimal
    from uuid import UUID
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from app.data_ingestion.models.etf import EtfCode, EtfEntity
    from app.data_ingestion.models.etf_daily import EtfDailyBar
    from app.data_ingestion.models.etf_watchlist import EtfWatchlistEntry
    from app.data_sources.models import DataSourceConfig
    from app.data_sources.providers import PROVIDERS
    from app.scheduling.models import ScheduledTask, TaskRun
    from sqlalchemy.orm import Session

    client, current, _service = market
    key = identity(current)
    seed(current, date(2026, 1, 26), date(2026, 2, 6))
    other = replace(sample('E67'), variant='unselected-d12-fixture')
    other = replace(other, content={**other.content, 'ts_code': '510300.SH'})
    assert run_entry(current, BY_ID['E67'], Inputs(other))['qualified']
    with current.catalog.engine.begin() as connection:
        for model in (EtfEntity, EtfCode, EtfDailyBar, EtfWatchlistEntry, ScheduledTask, TaskRun):
            model.__table__.create(connection)
    with Session(current.catalog.engine) as session, session.begin():
        iid = UUID(key.split(':', 1)[1])
        session.add(EtfEntity(id=iid)); session.flush()
        session.add(EtfCode(source='tushare', ts_code='510300.SH', etf_id=iid,
            list_status='L', exchange='SSE', first_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        session.add(EtfDailyBar(source='tushare', ts_code='510300.SH', trade_date=date(2026, 2, 6), close=Decimal('99')))
        for key in PROVIDERS:
            if session.get(DataSourceConfig, key) is None:
                session.add(DataSourceConfig(key=key, initialized=True, enabled=False, values={}))
    def block(_conn, _cursor, statement, _params, _context, _many):
        native = any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars', 'etf_adjustment_factors', 'trading_calendar_days'))
        if native and 'etf_watchlist_entries' not in statement.lower():
            raise AssertionError('legacy market SQL is blocked; shared watchlist remains separate')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0)); api_port = reservation.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, host='127.0.0.1', port=api_port, lifespan='off', log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    repo = Path(__file__).resolve().parents[2]
    frontend = repo / 'frontend' / 'dist'
    assert (frontend / 'index.html').is_file()
    class StaticAndProxy(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            if self.path.startswith('/api/'):
                proxy = http.client.HTTPConnection('127.0.0.1', api_port, timeout=30)
                try:
                    proxy.request('GET', self.path, headers={key: value for key, value in self.headers.items() if key.lower() != 'host'})
                    response = proxy.getresponse(); payload = response.read()
                    self.send_response(response.status); self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # The page cancelled a superseded read.
                finally:
                    proxy.close()
                return
            target = (frontend / self.path.split('?')[0].lstrip('/')).resolve()
            if not target.is_relative_to(frontend.resolve()):
                self.send_error(403); return
            if not target.is_file(): target = frontend / 'index.html'
            import mimetypes
            payload = target.read_bytes(); self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(target)[0] or 'application/octet-stream')
            self.end_headers(); self.wfile.write(payload)
    web = ThreadingHTTPServer(('127.0.0.1', 0), StaticAndProxy)
    web_thread = threading.Thread(target=web.serve_forever, daemon=True); web_thread.start()
    listening = False
    try:
        for _ in range(50):
            if server.started: break
            time.sleep(.1)
        assert server.started
        shared = http.client.HTTPConnection('127.0.0.1', api_port, timeout=10)
        shared.request('PUT', '/api/admin/etf-watchlist/510300.SH', headers=HEADERS)
        response = shared.getresponse(); payload = response.read(); shared.close()
        assert response.status == 200, payload
        event.listen(current.catalog.engine, 'before_cursor_execute', block); listening = True
        subprocess.run(['node', str(repo / 'ops/s2-d12/formal-browser-check.mjs')], cwd=repo,
            env={**os.environ, 'QF_D12_FORMAL_URL': f'http://127.0.0.1:{web.server_port}',
                'QF_D12_TEST_TOKEN': HEADERS['Authorization'].removeprefix('Bearer ')}, check=True, timeout=90)
    finally:
        web.shutdown(); web.server_close(); web_thread.join(timeout=5)
        server.should_exit = True; thread.join(timeout=5)
        if listening:
            event.remove(current.catalog.engine, 'before_cursor_execute', block)
