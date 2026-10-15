#!/usr/bin/env python3
"""Test immutable API/frontend images on fresh owned PG/ext4 Docker volumes.

Artificial inputs only; no production environment, provider or response injection.
Removes exactly the uniquely named resources created by this invocation.
"""
import argparse
from decimal import Decimal
import json
import os
import runpy
import re
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--closure-only', action='store_true', help='Run the recovered D13 closure gaps without repeating the baseline checks')
    parser.add_argument('--keep-preview', action='store_true', help='Keep only this successful synthetic pair for a local interactive preview')
    args = parser.parse_args()
    assert not args.output.exists(), 'Require a new evidence path'
    repo = Path(__file__).resolve().parents[1]
    receipt = json.loads(args.manifest.read_text())
    api, web = (receipt['images'][key]['id'] for key in ('backend', 'frontend'))
    assert api.startswith('sha256:') and web.startswith('sha256:')
    project = 'qf-s2-d13-' + uuid4().hex[:12]
    containers, volumes = [], []
    network_created = False
    edge_created = False
    edge = project + '-edge'
    result = dict(task='S2-D13', synthetic=True, live_verified=False, deployed=False,
        images=receipt['images'], architecture=receipt['architecture'], checks=[], requests=[])
    env = dict(QF_ENVIRONMENT='test', QF_API_TOKEN='a' * 64, QF_CURSOR_SIGNING_KEY='b' * 64,
        QF_DATABASE_HOST='postgres', QF_DATABASE_PORT='5432', QF_DATABASE_USER='postgres',
        QF_DATABASE_PASSWORD='isolated-d13-only', QF_DATABASE_NAME='qf_s2_market_test',
        QF_SERVER_HOST='0.0.0.0', QF_DATA_STORE_ROOT='/app/data/current-store',
        QF_SCHEDULER_ENABLED='false', QF_S3_ACCEPT_NEW_RUNS='true' if args.closure_only else 'false',
        QF_MARKET_TEST_ADJUSTMENT_CONTRACT='cumulative-exact-date-fixture@1',
        QF_MARKET_TEST_POST_ANCHOR_DATE='2024-01-01', QF_MARKET_TEST_POST_ANCHOR_FACTOR='1')
    environment = [item for key, value in env.items() for item in ('--env', key + '=' + value)]
    uid = '999:999' if args.closure_only else '65532:65532'

    def command(parts, timeout=120):
        completed = subprocess.run(parts, capture_output=True, text=True, timeout=timeout)
        if completed.returncode:
            raise RuntimeError('Isolated command failed: ' + completed.stderr[-3000:] + completed.stdout[-3000:])
        return completed.stdout.strip()

    def owned_mount(volume, target):
        # An empty named volume can be populated from the image again on a
        # later mount, including its image UID. Preserve the owner prepared
        # for this fresh, uniquely owned test volume instead.
        return ['--mount', 'type=volume,source=' + volume + ',target=' + target + ',volume-nocopy']

    def ephemeral(extra, command_args, *, timeout=120):
        compatibility = ['--user', uid, *owned_mount(volumes[2], '/app/data/logs')] if args.closure_only else []
        return command(['docker', 'run', '--rm', '--network', project, *environment, *compatibility, *extra,
            api, *command_args], timeout=timeout)

    def read(path, params=None, *, authenticated=True, expected=200):
        suffix = '?' + urlencode(params) if params else ''
        started = time.monotonic()
        request = Request(base + path + suffix, headers={'Authorization': 'Bearer ' + env['QF_API_TOKEN']} if authenticated else {})
        try:
            response = urlopen(request, timeout=35)
        except HTTPError as error:
            response = error
        payload = response.read()
        assert len(payload) <= 4 * 1024 * 1024, 'market response exceeds its declared output budget'
        elapsed = round(time.monotonic() - started, 4)
        logged_params = {key: ('<signed cursor omitted>' if key == 'cursor' else value) for key,value in (params or {}).items()}
        result['requests'].append(dict(path=path, params=logged_params, status=response.code,
            seconds=elapsed, bytes=len(payload)))
        assert response.code == expected, (path, response.code, payload[:800])
        return json.loads(payload) if payload else None

    try:
        result['stage'] = 'isolated-setup'
        command(['docker', 'network', 'create', '--internal', '--label', 'qf-s2-d13-isolated=true', project]); network_created = True
        # Only Nginx joins this separate bridge for the host loopback port.
        # API/PostgreSQL stay exclusively on the internal network.
        command(['docker', 'network', 'create', '--label', 'qf-s2-d13-isolated=true', edge]); edge_created = True
        for suffix in (('pg', 'current', 'logs') if args.closure_only else ('pg', 'current')):
            name = project + '-' + suffix
            command(['docker', 'volume', 'create', '--label', 'qf-s2-d13-isolated=true', name]); volumes.append(name)
        containers.append(project + '-pg')
        command(['docker', 'run', '-d', '--name', containers[-1], '--network', project, '--network-alias', 'postgres',
            '--env', 'POSTGRES_PASSWORD=isolated-d13-only', '--env', 'POSTGRES_DB=qf_s2_market_test',
            '--volume', volumes[0] + ':/var/lib/postgresql/data', 'postgres:17-alpine'])
        for _ in range(100):
            ready = subprocess.run(['docker', 'exec', containers[0], 'pg_isready', '-U', 'postgres'], capture_output=True).returncode == 0
            if ready: break
            time.sleep(.2)
        assert ready
        for volume, target in [(volumes[1], '/app/data/current-store')] + ([(volumes[2], '/app/data/logs')] if args.closure_only else []):
            command(['docker', 'run', '--rm', '--network', 'none', '--user', '0:0',
                *owned_mount(volume, target), api, 'chown', uid, target])
        native = ephemeral(['--read-only'], ['python', '-c',
            "import hashlib,json; from pathlib import Path; from quantfoundry import _native; "
            "print(json.dumps(dict(sha256=hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest(),"
            "adjustment_callable=callable(_native.adjust_prices_json))))"])
        result['native_installation'] = json.loads(native)
        assert result['native_installation']['sha256'] == receipt['source']['build']['wheel']['native_sha256']
        assert result['native_installation']['adjustment_callable']
        ephemeral([], ['alembic', 'upgrade', 'head'], timeout=180)
        result['migration'] = ephemeral([], ['alembic', 'current'])
        result['stage'] = 'formal-fixture'
        fixture = ephemeral([*owned_mount(volumes[1], '/app/data/current-store'), '--mount',
            'type=bind,src=' + str(repo / 'ops/s2-batch2/isolated-fixture.py') + ',dst=/tmp/fixture.py,readonly'],
            ['python', '-c', "import runpy,sys; sys.argv=['fixture']+" + ("['closure']" if args.closure_only else "[]") + "; runpy.run_path('/tmp/fixture.py',run_name='__main__')"], timeout=600 if args.closure_only else 180)
        result['fixture'] = json.loads(fixture.splitlines()[-1])
        result['stage'] = 'default-image-start'
        containers.append(project + '-api')
        command(['docker', 'run', '-d', '--name', containers[-1], '--network', project, '--network-alias', 'backend',
            *environment, '--user', uid, *owned_mount(volumes[1], '/app/data/current-store'),
            *(owned_mount(volumes[2], '/app/data/logs') if args.closure_only else []), api])
        containers.append(project + '-web')
        command(['docker', 'run', '-d', '--name', containers[-1], '--network', project, '--network', edge, '--publish', '127.0.0.1::80',
            '--env', 'QF_BACKEND_PORT=8000', web])
        address = command(['docker', 'port', containers[-1], '80/tcp'])
        assert address.startswith('127.0.0.1:')
        base = 'http://' + address
        for _ in range(100):
            try:
                with urlopen(base + '/readyz', timeout=1) as response:
                    ready = response.status == 200
                if ready: break
            except (URLError, TimeoutError):
                pass
            time.sleep(.2)
        assert ready, 'Default python -m app / Nginx pair did not start'
        result['checks'].append('immutable pair starts with real lifespan, without an S3 worker')
        if args.closure_only:
            result['stage'] = 'compatibility'
            checks = runpy.run_path(str(repo / 'ops/s2-batch2/closure_checks.py'))
            result['compose_preservation'] = checks['check_compose'](repo, receipt['images'], args.output.with_name('COMPOSE_RESULT.json'))
            def snapshot():
                return json.loads(ephemeral([*owned_mount(volumes[1], '/app/data/current-store'), '--mount',
                    'type=bind,src=' + str(repo / 'ops/s2-batch2/isolated-fixture.py') + ',dst=/tmp/fixture.py,readonly'],
                    ['python', '-c', "import runpy,sys; sys.argv=['fixture','snapshot']; runpy.run_path('/tmp/fixture.py',run_name='__main__')"]))
            before = snapshot()
            assert before['settings']['s3_accept_new_runs'] is True
            assert before['settings']['scheduler_enabled'] is False
            read('/api/auth/verify', expected=204)
            read('/api/system/version')
            assert all(not item['enabled'] for item in read('/api/data-sources')['items'])
            selections = read('/api/admin/etf-watchlist')['items']
            assert len(selections) == 1 and selections[0]['ts_code'] == '510300.SH' and Decimal(selections[0]['close']) == 99
            read('/api/admin/backtests/capabilities')
            issues = read('/api/admin/data-store/issues')
            result['checks'].append('UID 999 image start; no worker; read routes preserve explicit S3 admission and disabled source settings')
        if not args.closure_only:
            read('/api/admin/market/capabilities', authenticated=False, expected=401)
            read('/api/admin/market/capabilities')
            overview = read('/api/admin/market/overview', dict(catalog_key='etf-current'))
            assert overview['total_instruments'] == 125
            first = read('/api/admin/market/instruments', dict(catalog_key='etf-current', limit=50))
            assert first['total'] == 125 and len(first['items']) == 50 and first['next_cursor']
            second = read('/api/admin/market/instruments', dict(catalog_key='etf-current', limit=50, cursor=first['next_cursor']))
            assert not set(x['symbol'] for x in first['items']) & set(x['symbol'] for x in second['items'])
            third = read('/api/admin/market/instruments', dict(catalog_key='etf-current', limit=50, cursor=second['next_cursor']))
            assert len(third['items']) == 25 and third['next_cursor'] is None
            assert len({row['symbol'] for page in (first,second,third) for row in page['items']}) == 125
            search = read('/api/admin/market/instruments', dict(catalog_key='etf-current', q='510300'))
            assert search['total'] == 1
            resolved = read('/api/admin/market/instruments/resolve', dict(symbol='510300.SH', asset_type='etf'))
            key = result['fixture']['instrument_key']; assert resolved['instrument_key'] == key
            read('/api/admin/market/instruments/' + key)
            options = read('/api/admin/market/instruments/' + key + '/series', dict(as_of='2026-10-09'))
            prod = next(row for row in options if row['series_key'] == 'etf-daily-local')
            assert prod['adjustments'] == ['none'] and prod['periods'] == ['day','week','month','year']
            raw_path = '/api/admin/market/instruments/' + key + '/bars'
            fixed = dict(series_key='etf-daily-local', end='2026-10-09', as_of='2026-10-09', limit=512)
            raw = read(raw_path, fixed)
            assert len(raw['items']) == 512 and raw['next_cursor']
            assert raw['items'][-1]['volume'] == '9007199254740993.1234'
            older = read(raw_path, {**fixed, 'cursor':raw['next_cursor']})
            repeat = read(raw_path, {**fixed, 'cursor':raw['next_cursor']})
            assert older['items'] == repeat['items'] and older['meta']['read_token'] == raw['meta']['read_token']
            assert older['items'][-1]['last_trade_date'] < raw['items'][0]['first_trade_date']
            read(raw_path, {**fixed,'limit':511,'cursor':raw['next_cursor']}, expected=422)
            read(raw_path, {**fixed,'limit':2001}, expected=422)
            for period in ('week', 'month', 'year'):
                params = {**fixed, 'start':'2024-01-01', 'period':period, 'limit':1}
                latest = read(raw_path, params)
                previous = read(raw_path, {**params, 'cursor':latest['next_cursor']})
                expanded = read(raw_path, {**params,'limit':2})
                assert previous['items'] + latest['items'] == expanded['items']
            mid = read(raw_path, {**fixed, 'start':'2026-09-01','end':'2026-09-15','as_of':'2026-09-15','period':'month'})
            assert mid['items'][0]['completion'] == 'partial'
            assert mid['items'][0]['last_trade_date'] == '2026-09-15'
            adjusted = {**fixed, 'series_key':'isolated-etf-daily', 'start':'2026-01-01', 'limit':5}
            pre = read(raw_path, {**adjusted, 'adjustment':'pre'})
            post = read(raw_path, {**adjusted, 'adjustment':'post'})
            baseline = read(raw_path, {**adjusted, 'adjustment':'none'})
            assert pre['basis']['anchor'] == {'date':'2026-10-09','factor':'2'}
            assert post['basis']['anchor'] == {'date':'2024-01-01','factor':'1'}
            assert Decimal(pre['items'][0]['open']) == Decimal(baseline['items'][0]['open'])
            assert Decimal(post['items'][0]['open']) == Decimal(baseline['items'][0]['open']) * 2
            multi = {**fixed,'series_key':'isolated-etf-daily','start':'2024-01-01','period':'year','adjustment':'pre','limit':1}
            adjusted_latest = read(raw_path, multi)
            adjusted_older = read(raw_path, {**multi,'cursor':adjusted_latest['next_cursor']})
            adjusted_whole = read(raw_path, {**multi,'limit':2})
            assert adjusted_older['items'] + adjusted_latest['items'] == adjusted_whole['items']
            assert adjusted_older['basis'] == adjusted_latest['basis']
            corresponding_raw = read(raw_path, {**multi,'adjustment':'none','limit':2})
            assert Decimal(adjusted_older['items'][0]['open']) == Decimal(corresponding_raw['items'][0]['open']) / 2
            denied = read(raw_path, {**fixed,'adjustment':'pre'}, expected=422)
            assert denied['detail']['code'] == 'FACTOR_ANCHOR_UNVERIFIED'
            missing_path = '/api/admin/market/instruments/' + result['fixture']['no_factor_key'] + '/bars'
            read(missing_path, fixed)
            missing_days = read(missing_path, {**fixed,'start':'2026-10-05','period':'week'})
            assert missing_days['items'][0]['completion'] == 'partial'
            assert missing_days['items'][0]['volume'] is None
            assert missing_days['meta']['coverage']['missing_dates'] == ['2026-10-05','2026-10-06','2026-10-07','2026-10-08']
            denied = read(missing_path, {**adjusted,'adjustment':'pre'}, expected=422)
            assert denied['detail']['code'] == 'FACTOR_UNAVAILABLE'
            read(raw_path, {**fixed,'end':'2026-10-12'}, expected=422)
            read('/api/admin/market/instruments/' + key + '/factors', dict(series_key='etf-daily-local',start='2026-10-01',end='2026-10-09',limit=20))
            unknown = read('/api/admin/market/calendar/SSE/2030-01-01')
            assert unknown['items'][0]['state'] == 'unknown'
            issues = read('/api/admin/data-store/issues')
            assert issues['total'] >= 1
            for suffix in ('trading-calendar','trading-calendar/overview','trading-calendar/SSE/2026-10-09',
                    'etfs/overview','etfs/510300.SH','etfs/510300.SH/daily-bars','etfs/510300.SH/adjustment-factors'):
                read('/api/admin/data-collections/' + suffix, expected=410)
            # Change one owned artificial mapping fact through the real DB while
            # the default API remains running. Old cursors must not read new data.
            ephemeral(['--mount','type=bind,src=' + str(repo / 'ops/s2-batch2/isolated-fixture.py') + ',dst=/tmp/fixture.py,readonly'],
                ['python','-c', "import runpy,sys; sys.argv=['fixture','append-identity']; runpy.run_path('/tmp/fixture.py',run_name='__main__')"])
            changed = read(raw_path, {**fixed,'cursor':raw['next_cursor']}, expected=409)
            assert changed['detail']['code'] == 'DATA_CHANGED'
            assert read(raw_path,fixed)['meta']['read_token'] != raw['meta']['read_token']
            result['checks'].extend(['125-object metadata paging/search; real formal summary/identity/detail/calendar/issues',
                'multi-year raw and complete output bucket paging; repeat and mismatched cursor',
                'installed native pre/post test contract; future factor excluded; production E69 remains restricted',
                'missing factors independent from raw; unauthenticated 401; seven authenticated legacy 410s'])
        if args.browser:
            result['stage'] = 'browser'
            browser_result = args.output.with_name('BROWSER_RESULT.json')
            completed = subprocess.run(['node', str(repo / ('ops/s2-batch2/closure-browser.mjs' if args.closure_only else 'ops/s2-batch2/browser-check.mjs'))], cwd=repo,
                env={**os.environ, 'QF_ENVIRONMENT':'test', 'QF_S2_TEST_URL':base,
                    'QF_S2_TEST_TOKEN':env['QF_API_TOKEN'], 'QF_S2_BROWSER_OUTPUT':str(browser_result),
                    'QF_S2_TEST_DATASET':issues['items'][0]['dataset'], 'QF_S2_FIXTURE':json.dumps(result['fixture'])},
                capture_output=True, text=True, timeout=420 if args.closure_only else 180)
            browser_result.with_name('BROWSER_PROCESS.log').write_text(completed.stdout + completed.stderr)
            if completed.returncode and browser_result.exists():
                result['stage'] = 'browser-' + json.loads(browser_result.read_text()).get('stage','unknown')
                if completed.returncode < 0:
                    result['stage'] += '-process-killed'
                elif not json.loads(browser_result.read_text()).get('error'):
                    # Only a safe exception kind leaves the private process log.
                    failure = re.search(r'\b(AssertionError|TimeoutError|TypeError|ReferenceError|SyntaxError|Error)\b', completed.stderr)
                    result['stage'] += '-process-' + (failure.group(1) if failure else 'exit')
            assert completed.returncode == 0, completed.stderr[-4000:] + completed.stdout[-2000:]
            result['browser'] = json.loads(browser_result.read_text())
        if args.closure_only:
            result['stage'] = 'read-only-preservation'
            assert args.browser, 'Closure needs the real browser path'
            after = snapshot()
            assert before['database'] == after['database'], 'Market reads modified application/source/governance facts'
            assert before['files'] == after['files'], 'Market reads created or changed persistent formal/scratch files'
            assert before['settings'] == after['settings'], 'Market reads changed service configuration'
            assert before['pending_owners'] == after['pending_owners'], 'Market reads changed source continuation ownership'
            # Record actual log growth separately from formal data; the test
            # performs a fixed finite number of reads and creates no poller.
            result['preservation'] = dict(database_tables=len(after['database']), formal_files=len(after['files']),
                unchanged=True, quota_slots_before=before['quota_slots'], quota_slots_after=after['quota_slots'],
                logs_before=before['logs'], logs_after=after['logs'], settings=after['settings'])
        process = command(['docker','exec',containers[1],'python','-c',
            "from pathlib import Path; print(Path('/proc/1/status').read_text())"])
        result['api_memory_kib'] = {line.split(':')[0]:int(line.split()[1]) for line in process.splitlines()
            if line.startswith(('VmRSS:','VmHWM:'))}
        assert result['api_memory_kib']['VmHWM'] * 1024 < 2 * 1024**3, 'Default CurrentStore process memory budget exceeded'
        result['passed'] = True
        if args.keep_preview:
            result['preview'] = dict(url=base, containers=containers, volumes=volumes, network=project, networks=[project,edge],
                synthetic_only=True)
    except BaseException as error:
        marker = re.search(r'__D13_FIXTURE_FAILURE__=([A-Za-z0-9_-]{1,160})', str(error))
        if marker:
            result['stage'] += '-' + marker.group(1)
        result['passed'] = False
        result['error'] = str(error)[-5000:]
        for name in containers[1:]:
            logs = subprocess.run(['docker','logs','--tail','15',name], capture_output=True,text=True)
            result.setdefault('isolated_failure_logs',{})[name] = (logs.stdout+logs.stderr)[-2500:]
        raise
    finally:
        if not (args.keep_preview and result.get('passed')):
            for name in reversed(containers):
                subprocess.run(['docker','rm','--force',name],capture_output=True)
            for name in volumes:
                subprocess.run(['docker','volume','rm',name],capture_output=True)
            if edge_created:
                subprocess.run(['docker','network','rm',edge],capture_output=True)
            if network_created:
                subprocess.run(['docker','network','rm',project],capture_output=True)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    elapsed = [item['seconds'] for item in result['requests']]
    print(json.dumps(dict(passed=result['passed'], requests=len(result['requests']),
        fixture=result['fixture'], images=result['images'], native_installation=result['native_installation'],
        maximum_response_bytes=max(item['bytes'] for item in result['requests']),
        maximum_request_seconds=max(elapsed), total_request_seconds=round(sum(elapsed),4),
        api_memory_kib=result['api_memory_kib'], browser_findings=result.get('browser',{}).get('findings',[]),
        browser_request_count=len(result.get('browser',{}).get('requests',[])),
        screenshots=result.get('browser',{}).get('screenshots',[]), preview=result.get('preview'),
        evidence=str(args.output)),ensure_ascii=False))


if __name__ == '__main__':
    main()
