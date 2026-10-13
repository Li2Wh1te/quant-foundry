"""Local D09 preview/check runner over a newly owned disposable test database.

Run inside the environment's foreground --db wrapper (or an equivalent empty
loopback test DB with migrations). Creates synthetic inputs through the existing
formal pipeline, never a successful provider or HTTP fixture. Not a deployment.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
from unittest.mock import patch

import uvicorn
from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from app.core.config import Settings  # noqa: E402
from app.data_store import locking  # noqa: E402
from app.data_store.adapters.registry import BY_ID  # noqa: E402
from app.data_store.pipeline import run_entry  # noqa: E402
from app.data_store.storage import CurrentStore  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_data_store_domain_samples import ts  # noqa: E402
from tests.test_data_store_local_pipeline import Inputs  # noqa: E402
from tests.test_s2_market_read import mapping  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Run the browser suite and stop owned services')
    parser.add_argument('--port', type=int, default=5173)
    parser.add_argument('--backend-port', type=int, default=8769)
    args = parser.parse_args()
    os.environ['QF_SCHEDULER_ENABLED'] = 'false'
    settings = Settings()
    if (settings.environment != 'test' or settings.database_host != '127.0.0.1'
            or os.getenv('POSTGRES_TEST_ENABLED') != '1'
            or not os.getenv('QF_TEST_POSTGRES_DSN')):
        raise RuntimeError('Requires an explicitly owned disposable loopback test DB')
    engine = get_engine()
    with engine.begin() as connection:
        # Refuse reuse: this runner only seeds a fresh wrapper-owned database.
        if connection.execute(text('SELECT count(*) FROM data_store_datasets')).scalar_one():
            raise RuntimeError('Refusing a populated formal store')
        connection.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES(1,'ready')"))
    root = settings.data_store_root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Same explicitly test-only overlay probe as repository kernel tests. This
    # proves projection behavior, never production filesystem suitability.
    with patch.object(locking, '_filesystem_name', return_value='ext4'):
        with CurrentStore(engine, root, cursor_key=settings.cursor_signing_key.get_secret_value().encode(), initialize=True) as current:
            rows = []
            for index in range(121):
                exchange = 'SSE' if index % 2 == 0 else 'SZSE'
                code = f'{510000 + index}.SH' if exchange == 'SSE' else f'{510000 + index}.SZ'
                rows.append(ts('E67', 'directory', {'source': 'tushare', 'ts_code': code,
                    'csname': f'隔离夹具 ETF {index:03}', 'exchange': exchange,
                    'list_date': f'{2015 + index % 10}0202'}))
            assert run_entry(current, BY_ID['E67'], Inputs(*rows))['qualified']
            dates = [('SSE', '2026-10-01', False), ('SSE', '2026-10-09', True),
                ('SSE', '2026-10-10', True), ('SSE', '2026-12-31', True),
                ('SSE', '2027-01-01', False), ('SSE', '2027-01-04', True),
                ('SZSE', '2026-10-01', True)]
            assert run_entry(current, BY_ID['E68'], Inputs(*(ts('E68', 'calendar',
                {'exchange': exchange, 'calendar_date': day, 'is_open': opened})
                for exchange, day, opened in dates)))['qualified']
            mapping(current, source_code='510000.SH', trading_code='510000')

        def no_collection(_connection, _cursor, statement, _params, _context, _many):
            if any(name in statement.lower() for name in ('etf_codes', 'etf_daily_bars',
                    'etf_adjustment_factors', 'trading_calendar_days', 'backtesting_accounts')):
                raise AssertionError('D09 preview forbids native market/execution reads')

        event.listen(engine, 'before_cursor_execute', no_collection)
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.backend_port, log_level='warning'))
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        vite = None
        try:
            for _ in range(100):
                if server.started:
                    break
                if not thread.is_alive():
                    raise RuntimeError('Preview backend failed to start')
                time.sleep(.1)
            else:
                raise RuntimeError('Preview backend startup timed out')
            env = {**os.environ, 'QF_DEV_BACKEND_URL': f'http://127.0.0.1:{args.backend_port}',
                'QF_D09_URL': f'http://127.0.0.1:{args.port}'}
            with tempfile.TemporaryFile(mode='w+') as log:
                vite = subprocess.Popen(['pnpm', '--dir', str(ROOT / 'frontend'), 'dev',
                    '--host', '127.0.0.1', '--port', str(args.port), '--strictPort'], env=env, stdout=log, stderr=log)
                print(f'D09 SYNTHETIC FORMAL PREVIEW: {env["QF_D09_URL"]}/admin/data/etf-basics', flush=True)
                if args.check:
                    return subprocess.call(['node', str(ROOT / 'frontend/tests/s2-market-browser.mjs')], env=env)
                print('Interactive preview ready; authenticate with the wrapper test credential. Ctrl-C stops owned services.', flush=True)
                while vite.poll() is None and thread.is_alive():
                    time.sleep(.25)
                raise RuntimeError('Preview service exited unexpectedly')
        finally:
            if vite is not None and vite.poll() is None:
                vite.terminate()
                try:
                    vite.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vite.kill()
                    vite.wait()
            server.should_exit = True
            thread.join(timeout=10)
            event.remove(engine, 'before_cursor_execute', no_collection)


if __name__ == '__main__':
    sys.exit(main())
