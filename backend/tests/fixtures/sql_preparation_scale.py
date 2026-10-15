"""Fresh-process measurement, existing default factories and private SQL facts."""
import json
import math
import os
from pathlib import Path
import re
import resource
import sys
import time
import weakref
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
payload = json.load(sys.stdin)
assert os.environ.get('POSTGRES_TEST_ENABLED') == '1'
assert payload['schema'].startswith('lfd01_test_') and re.fullmatch('[a-z0-9_]+', payload['schema'])
if hasattr(os, 'sched_getaffinity'):
    affinity = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, affinity[:4])
_soft, hard = resource.getrlimit(resource.RLIMIT_AS)
memory_limit = min(8*1024**3, hard) if hard != resource.RLIM_INFINITY else 8*1024**3
resource.setrlimit(resource.RLIMIT_AS, (memory_limit, hard))

def peak_rss():
    # VmHWM belongs to this exec's address space. getrusage may also retain
    # the seeding parent's pre-exec high water mark on Linux.
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmHWM:'):
            return int(line.split()[1])*1024
    raise AssertionError('Linux process peak RSS is unavailable')

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from app.main import create_app
from app.db.session import get_db_session
from app.core.config import Settings
from app.backtest_service.formal_resolver import SqlFormalPlanResolver, FactReadBudget
from app.backtest_service.session_fact_pages import BRIDGES
from app.backtest_service.models import Run, RunResult
from app.backtest_service.runtime_contracts import RuntimeFault
from app.instruments.models import InstrumentIdentityFactRecord
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.instruments.rules.contracts import FactQualityStatus
from app.instruments.rules.resolver import RulePackageResolver
from app.backtest_service import formal_sql_candidates as candidates
from tests.test_s3_sql_resolver import (http_api, TOKEN, CURSOR, KNOWN, common_provenance,
    CalendarFactRepository, CalendarSessionFact, REGISTRY_FACT_IDS, DEFINITION_FACT_IDS)
from datetime import date, timedelta
from uuid import UUID

engine = create_engine(payload['url'], connect_args={'options': '-csearch_path=' + payload['schema'] + ',public'})
assert engine.url.host in {'127.0.0.1', 'localhost'} and engine.url.database == 'quant_foundry_test'
root = Path(payload['scratch'])
(root/'work').mkdir(mode=0o700)
settings = Settings(api_token=TOKEN, cursor_signing_key=CURSOR, database_password='isolated-test',
    s3_result_root=root/'results', data_store_root=Path(payload['root']), environment='test',
    scheduler_enabled=False, s3_work_root=root/'work', s3_daemon_work_root=root/'work',
    s3_worker_image='sha256:'+'a'*64, _env_file=None)
app = create_app(settings)
def db():
    with Session(engine) as session:
        yield session
app.dependency_overrides[get_db_session] = db
metrics = dict(fact_selects=0, returned_fact_rows=0, max_batch_rows=0, max_batch_bytes=0,
    max_batch_seconds=0.0, select_count=0, page_count=0, max_symbols_page=0,
    source_decodes=dict(identity=0, mapping=0, rule=0), rule_resolutions=0)
observed, refs, anchors = {}, {}, {}
tracking, changed = [True], [False]
decoded_total = dict(identity=0, mapping=0, rule=0)
checking = [False]
config = payload['config']
targets = {(config['universe'][0], config['start']), (config['universe'][-1], config['end'])}
def statement(connection, cursor, sql, parameters, context, executemany):
    if checking[0] and sql.lstrip().startswith(('SELECT', 'WITH')):
        assert 'sha256' in sql or 'current_setting' in sql, 'consistency checks must not fetch domain history'
    if tracking[0] and sql.lstrip().startswith(('SELECT', 'WITH')):
        metrics['select_count'] += 1
event.listen(engine, 'before_cursor_execute', statement)
original_read, original_check, original_reset = FactReadBudget.read, FactReadBudget.check, FactReadBudget.reset
original_resolve, original_page = SqlFormalPlanResolver._resolve, SqlFormalPlanResolver._read_dependency_page
original_rule = RulePackageResolver.resolve
def reset(self):
    original_reset(self)
    self.measured_started = time.perf_counter()
def check(self):
    result = original_check(self)
    if tracking[0]:
        metrics['max_batch_seconds'] = max(metrics['max_batch_seconds'], time.perf_counter()-self.measured_started)
    return result
def read(self, call):
    before = self.rows
    result = original_read(self, call)
    if tracking[0]:
        metrics['fact_selects'] += 1
        metrics['returned_fact_rows'] += self.rows-before
        metrics['max_batch_rows'] = max(metrics['max_batch_rows'], self.rows)
        metrics['max_batch_bytes'] = max(metrics['max_batch_bytes'], self.bytes)
    return result
def resolved(self, session, budget, work, **kwargs):
    observed['resolve_calls'] = observed.get('resolve_calls', 0) + 1
    facts = original_resolve(self, session, budget, work, **kwargs)
    observed.update(resolver=self, facts=facts, work=work)
    return facts
def page(self, session, work, symbols, day, **kwargs):
    assert all(ref() is None for ref in refs.values())
    value = original_page(self, session, work, symbols, day, **kwargs)
    if tracking[0]:
        metrics['page_count'] += 1
        metrics['max_symbols_page'] = max(metrics['max_symbols_page'], len(symbols))
    refs.setdefault('first', weakref.ref(value))
    refs['previous'] = weakref.ref(value)
    for symbol, value_day, fact, _ in value.identities:
        if (symbol, value_day.isoformat()) in targets:
            rule = next(row[2] for row in value.rule_packages if row[:2] == (symbol, value_day))
            anchors[(symbol, value_day.isoformat())] = (fact, rule)
    if payload['change_during'] and not changed[0]:
        with engine.begin() as writer:
            row = writer.execute(select(InstrumentRuleFactRecord.quality_status).where(
                InstrumentRuleFactRecord.instrument_id == UUID(payload['last_instrument_id']))).scalar_one()
            other = next(status.value for status in FactQualityStatus if status.value != row)
            writer.execute(InstrumentRuleFactRecord.__table__.update().where(
                InstrumentRuleFactRecord.instrument_id == UUID(payload['last_instrument_id'])).values(quality_status=other))
        changed[0] = True
    return value
def rule(self, *args, **kwargs):
    if tracking[0]: metrics['rule_resolutions'] += 1
    return original_rule(self, *args, **kwargs)
def conversion(name, original):
    def call(*args, **kwargs):
        decoded_total[name] += 1
        if tracking[0]: metrics['source_decodes'][name] += 1
        return original(*args, **kwargs)
    return call
with patch.object(FactReadBudget, 'reset', reset), patch.object(FactReadBudget, 'check', check), \
        patch.object(FactReadBudget, 'read', read), patch.object(SqlFormalPlanResolver, '_resolve', resolved), \
        patch.object(SqlFormalPlanResolver, '_read_dependency_page', page), patch.object(RulePackageResolver, 'resolve', rule), \
        patch.object(candidates, '_identity_to_domain', conversion('identity', candidates._identity_to_domain)), \
        patch.object(candidates, 'mapping_to_domain', conversion('mapping', candidates.mapping_to_domain)), \
        patch.object(candidates, 'rule_to_domain', conversion('rule', candidates.rule_to_domain)), \
        patch('app.backtest_service.router.get_engine', return_value=engine):
    started = time.perf_counter()
    with http_api(app, timeout=180) as request:
        response = request('POST', '/api/admin/backtests/preflight', config)
    metrics['elapsed_seconds'] = time.perf_counter()-started
    metrics['preparation_peak_rss_bytes'] = peak_rss()
    assert response.status_code == 200, response.text
    diagnostic = response.json()['errors'][0]
    metrics['code'] = diagnostic['code']
    facts, resolver, work = observed['facts'], observed['resolver'], observed['work']
    cells = len(config['universe'])*((date.fromisoformat(config['end'])-date.fromisoformat(config['start'])).days+1)
    metrics['prepared_cells'] = cells
    assert not any((facts.identities, facts.calendars, facts.rule_packages,
        facts.trading_status, facts.corporate_actions, facts.commissions))
    checks = {row['binding']: row for row in facts.checks}
    for name in BRIDGES:
        assert checks[name]['available'] and checks[name]['scope']['checked_cells'] == str(cells), checks[name]
    assert checks['execution_calendar_assembly']['available'] and checks['execution_corporate_actions']['available']
    assert metrics['page_count'] == math.ceil(len(config['universe'])/256)*(cells//len(config['universe']))
    assert all(ref() is None for ref in refs.values())
    tracking[0] = False
    if payload['change_during']:
        assert diagnostic['code'] == 'DATA_CHANGED', diagnostic
    else:
        assert diagnostic['code'] == 'CAPABILITY_UNAVAILABLE', diagnostic
        assert checks['execution_plan_assembly']['missing'] == ['resolved_market_projection', 'resolved_execution_inputs']
        first = facts.execution_calendar['calendar'][0]
        last = facts.execution_calendar['calendar'][-1]
        first_page = facts.execution_inputs.read(first['session']['key'], 0, 2)
        tail = facts.execution_inputs.read(last['session']['key'], len(config['universe'])-1, 1)
        assert str(tail.identities[0][2].instrument_id) == payload['last_instrument_id']
        from copy import deepcopy
        small = deepcopy(work)
        small.config.update(universe=config['universe'][:2], end=(date.fromisoformat(config['start'])+timedelta(days=1)).isoformat())
        legacy = resolver.resolve_facts(small, paged=False)
        assert first_page.identities == tuple((s, d, f, ()) for s, d, f, _ in legacy.identities[:2])
        for row in legacy.rule_packages:
            key = (row[0], row[1].isoformat())
            if key in anchors:
                assert row[2] == anchors[key][1]
        for row in legacy.execution_calendar['calendar']:
            assert row == next(item for item in facts.execution_calendar['calendar'] if item['date'] == row['date'])
        # Cold calendar history is outside the run guard. Its addition does not
        # decode any domain history at ordinary or final consistency checks.
        with Session(engine) as session:
            for offset in range(365):
                day = date(2010, 1, 1)+timedelta(days=offset)
                provenance = common_provenance(content_hash=None)
                provenance.update(known_at=KNOWN, observed_at=KNOWN, knowledge_from=KNOWN, knowledge_as_of=KNOWN)
                CalendarFactRepository(session).append_session_fact(CalendarSessionFact(calendar_id='SSE',
                    session_date=day, is_open=False, definition_version='sse-bootstrap-v1',
                    registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']), registry_version=1,
                    definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']), **provenance))
            session.commit()
        before = dict(decoded_total)
        before_calls = observed['resolve_calls']
        checking[0] = True
        resolver.check_facts(work, facts)
        checking[0] = False
        assert observed['resolve_calls'] == before_calls
        metrics['cold_recheck_decodes'] = sum(decoded_total.values())-sum(before.values())
        with engine.begin() as writer:
            writer.execute(InstrumentIdentityFactRecord.__table__.update().where(
                InstrumentIdentityFactRecord.instrument_id == UUID(payload['last_instrument_id'])).values(currency='USD'))
        for final in (False, True):
            checking[0] = True
            try:
                if final:
                    with engine.begin() as connection:
                        resolver.check_facts(work, facts, connection=connection)
                        raise AssertionError('changed facts must prevent final publication')
                else:
                    resolver.check_facts(work, facts)
            except RuntimeFault as error:
                assert error.code == 'DATA_CHANGED'
            else:
                raise AssertionError('changed facts were accepted')
            finally:
                checking[0] = False
        assert observed['resolve_calls'] == before_calls
        metrics['final_recheck_decodes'] = sum(decoded_total.values())-sum(before.values())
    with Session(engine) as session:
        assert session.scalar(select(Run.id)) is None and session.scalar(select(RunResult.run_id)) is None
    metrics['no_runs_or_results'] = True
metrics['process_peak_rss_bytes'] = peak_rss()
metrics['process_address_space_limit_bytes'] = memory_limit
engine.dispose()
print(json.dumps(metrics, sort_keys=True))
