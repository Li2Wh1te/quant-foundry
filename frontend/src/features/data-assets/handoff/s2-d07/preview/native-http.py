"""Probe actual project HTTP routes in the cloud-dev wrapper's disposable database.

No dependency overrides, mock transport, synthetic business rows, ready writes,
filesystem exceptions, supplier calls, or production access. A non-ready receipt
is an explicitly labelled isolated control fixture, not a production observation.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(REPO / 'backend'))
import requests
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from app.core.config import Settings
from app.db.session import get_engine
from app.main import create_app
from app.data_store.router import BUSINESS, _frequency


class LocalHttp(requests.Session):
    def __init__(self, base_url, timeout=15):
        super().__init__()
        self.base_url, self.timeout, self.trust_env = base_url, timeout, False

    def request(self, method, url, **kwargs):
        from urllib.parse import urljoin
        return super().request(method, urljoin(self.base_url, url), timeout=self.timeout, **kwargs)


def main():
    dsn = make_url(os.environ['QF_TEST_POSTGRES_DSN'])
    assert dsn.host == '127.0.0.1' and dsn.database == 'qf_cloud_test'
    settings = Settings(_env_file=None, scheduler_enabled=False)
    assert settings.environment == 'test'
    assert str(settings.data_store_root).startswith('/workspace/.setup/cloud-dev/runs/run-')
    # The native constructor expects a pre-created trusted root. This directory
    # belongs only to this disposable run and contains no business files.
    settings.data_store_root.mkdir(mode=0o700, parents=True)
    output = Path(os.environ.get('QF_D07_EVIDENCE_DIR', str(REPO / 'frontend/src/features/data-assets/handoff/s2-d07/evidence')))
    output.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    checks = []
    blocked = []
    def record(name, response, expected, code=None):
        assert response.status_code == expected, (name, response.status_code)
        body = response.json() if response.content else None
        if code:
            assert body['detail']['code'] == code
        checks.append({'name': name, 'status': response.status_code, **({'code': code} if code else {})})
        return body
    try:
        for _ in range(100):
            if server.started: break
            if not thread.is_alive(): raise RuntimeError('Owned API failed to start')
            time.sleep(.1)
        assert server.started
        headers = {'Authorization': f'Bearer {settings.api_token.get_secret_value()}'}
        prefix = '/api/admin/data-store'
        entry = next(e for e in BUSINESS.values() if e.native == 'stock_daily')
        # Only a bounded refusal probe; these keys do not represent real data.
        query = dict(dataset=entry.spec.name, frequency=_frequency(entry), representation='isolated-refusal-probe',
                     subject='isolated-refusal-probe', from_key='2026-01-02', to_key='2026-01-02',
                     columns=['object_key'], page_size=20, cursor=None, allow_partial=False)
        with LocalHttp(f'http://127.0.0.1:{port}') as client:
            record('actual auth: missing token', client.get(prefix + '/datasets'), 401)
            record('actual auth: incorrect token', client.get(prefix + '/datasets', headers={'Authorization': 'Bearer isolated-invalid'}), 401)
            record('actual token verification', client.get('/api/auth/verify', headers=headers), 204)
            record('actual status registry', client.get(prefix + '/status?limit=1&offset=0', headers=headers), 200)
            record('actual filtered empty issue collection', client.get(prefix + '/issues', params={'dataset': entry.spec.name, 'limit': 20, 'offset': 0}, headers=headers), 200)
            response = client.get(prefix + '/datasets?limit=1&offset=0', headers=headers)
            if response.status_code == 422 and response.json()['detail']['code'] == 'UNSUPPORTED_FILESYSTEM':
                record('natural empty install retains native filesystem refusal', response, 422, 'UNSUPPORTED_FILESYSTEM')
                blocked.append('CurrentStore is blocked by UNSUPPORTED_FILESYSTEM on this executor; no override applied.')
                record('actual query retains native filesystem refusal', client.post(prefix + '/query', json=query, headers=headers), 422, 'UNSUPPORTED_FILESYSTEM')
            else:
                record('natural empty-install native catalog', response, 200)
                blocked.append('No real business files exist in this disposable database; successful business paging remains unverified.')
            with get_engine().begin() as connection:
                assert connection.execute(text('SELECT count(*) FROM data_store_legacy_maintenance')).scalar_one() == 0
                # A closed test gate only. Never manufacture a ready receipt.
                connection.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES (1,'rebuilding')"))
            first = record('actual catalog under closed isolated maintenance gate', client.get(prefix + '/datasets?limit=1&offset=0', headers=headers), 200)
            assert first['items'][0]['status'] == 'rebuilding' and first['next_offset'] == 1
            second = record('actual registry pagination under closed gate', client.get(prefix + '/datasets?limit=1&offset=1', headers=headers), 200)
            assert second['items'][0]['dataset'] != first['items'][0]['dataset']
            from urllib.parse import quote
            detail = record('actual registry descriptor/schema under closed gate', client.get(prefix + '/datasets/' + quote(entry.spec.name, safe=''), headers=headers), 200)
            assert detail['fields'] and detail['schema_id'] == entry.spec.schema_id and detail['preview_key'] is None
            record('actual query honors closed maintenance gate', client.post(prefix + '/query', json=query, headers=headers), 503, 'DATA_STORE_REBUILDING')
            record('actual unknown dataset refusal', client.get(prefix + '/datasets/isolated-unknown-dataset', headers=headers), 404, 'DATASET_UNKNOWN')
            record('actual page-size budget validation', client.post(prefix + '/query', json={**query, 'page_size': 101}, headers=headers), 422)
            record('actual column budget validation', client.post(prefix + '/query', json={**query, 'columns': ['object_key'] * 33}, headers=headers), 422)
        env = os.environ.copy()
        env['QF_DEV_BACKEND_URL'] = f'http://127.0.0.1:{port}'
        env['QF_D07_NATIVE_DATASET'] = entry.spec.name
        env['QF_D07_NATIVE_SCREENSHOTS'] = str(output / 'native-screenshots')
        # The value comes only from the wrapper's public local test environment.
        with (output / 'native-vite.log').open('w') as log:
            frontend = subprocess.Popen(['pnpm', '--dir', str(REPO / 'frontend'), 'dev', '--host', '127.0.0.1', '--port', '5198', '--strictPort'], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with LocalHttp('http://127.0.0.1:5198', timeout=1) as client:
                    for _ in range(100):
                        try:
                            if client.get('http://127.0.0.1:5198').status_code == 200: break
                        except requests.RequestException: pass
                        if frontend.poll() is not None: raise RuntimeError('Owned Vite failed to start')
                        time.sleep(.1)
                    else: raise RuntimeError('Owned Vite readiness timeout')
                subprocess.run(['node', str(Path(__file__).with_name('native-browser.cjs'))], env=env, check=True)
            finally:
                frontend.terminate()
                try: frontend.wait(timeout=10)
                except subprocess.TimeoutExpired: frontend.kill(); frontend.wait()
        blocked.extend([
            'No authorized real development/production read route or session was supplied to this task.',
            'require_ready remains global; healthy-A plus restricted-B domain separation is not delivered in this checkout.',
            'Real business pagination, cursor DATA_CHANGED and role-based 403 are unverified; the current API has one authenticated credential.'
        ])
        (output / 'native-http.json').write_text(json.dumps({'source': 'Actual FastAPI HTTP + migrated disposable PostgreSQL',
            'controlFixture': 'isolated non-ready maintenance receipt; no ready writes or business rows',
            'passed': len(checks), 'checks': checks, 'blocked': blocked,
            'realDataAcceptance': False, 'productionOperations': False}, ensure_ascii=False, indent=2) + '\n')
        print(f'Native HTTP: {len(checks)} actual route checks passed; real business data acceptance remains blocked.')
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive(): raise RuntimeError('Owned API did not stop')
        sock.close()


if __name__ == '__main__':
    main()
