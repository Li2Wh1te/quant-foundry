"""Default HTTP preparation of both SR04 scopes, only private synthetic SQL."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tests.test_s3_sql_resolver import (database, limits, store, formal, formal_facts,
    seed, DAY, KNOWN, common_provenance, CalendarFactRepository,
    CalendarSessionFact, REGISTRY_FACT_IDS, DEFINITION_FACT_IDS)
from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
from app.data_ingestion.models.trading_calendar import TradingStatusCoverageFact
from app.backtest_service.accounts.service import AccountProfileService
from app.backtest_service.accounts.types import AccountProfileStatus
from app.strategies.service import StrategyStorageService
from app.backtest_service.service import normalize

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')


@pytest.mark.parametrize('symbols,days,change_during', [(100, 365, False), (5000, 5, False), (257, 2, True)])
def test_default_http_prepares_bounded_sql_scopes_and_rejects_changes(formal_facts, tmp_path, symbols, days, change_during):
    universe = [f'{510001 + i:06}.SH' for i in range(symbols)]
    ids = [seed(formal_facts, symbol=symbol, calendar=i == 0) for i, symbol in enumerate(universe)]
    end = DAY + timedelta(days=days - 1)
    with Session(formal_facts.catalog.engine) as session:
        for model in (CorporateActionCoverageFact, TradingStatusCoverageFact):
            for row in session.scalars(select(model)):
                row.end_date = end
        for iid in ids:
            for family in ('share_distribution', 'split', 'rights_issue'):
                session.add(CorporateActionCoverageFact(id=uuid4(), instrument_id=iid,
                    start_date=DAY, end_date=end, status='complete', event_count=0, action_type=family,
                    source='isolated', source_revision='synthetic-sr04-zero', known_at=KNOWN,
                    observed_at=KNOWN, evidence=dict(reference='isolated-sr04-zero'), validation_rule='isolated@1'))
        lower = min(DAY.replace(day=1), DAY - timedelta(days=DAY.weekday()))
        day = lower
        while day <= end + timedelta(days=101):
            if day != DAY:
                provenance = common_provenance(content_hash=None)
                provenance.update(known_at=KNOWN, observed_at=KNOWN, knowledge_from=KNOWN, knowledge_as_of=KNOWN)
                CalendarFactRepository(session).append_session_fact(CalendarSessionFact(
                    calendar_id='SSE', session_date=day, is_open=day.weekday() < 5,
                    definition_version='sse-bootstrap-v1', registry_fact_id=UUID(REGISTRY_FACT_IDS['SSE']),
                    registry_version=1, definition_fact_id=UUID(DEFINITION_FACT_IDS['SSE']),
                    **provenance))
            day += timedelta(days=1)
        account = AccountProfileService(session).create(name='isolated-scale-' + uuid4().hex,
            status=AccountProfileStatus.ACTIVE, metadata={}, fee_schedule=dict(key='isolated-scale-fee',
                metadata=dict(currency='CNY', s3_commission=dict(included_components=[])),
                fee_rules=[dict(key='commission', category='commission', side='both', rate='0.0003',
                    minimum='5', rounding_level='fee_item', rounding_scope='on_fill', rounding_mode='half_up',
                    rounding_precision='0.01')]))
        service = StrategyStorageService(session)
        strategy = service.create_strategy(name='isolated-scale-' + uuid4().hex,
            source_code='def initialize(c): pass', parameter_schema={'type': 'object'}, default_parameters={})
        revision = service.publish_revision(strategy.id, expected_draft_version=1)
        session.commit()
        config = normalize(dict(api_schema='qf.backtest.v2', account_id=str(account.id),
            strategy_revision_id=str(revision.id), initial_cash='10000', universe=universe,
            frequency='1d', start=DAY.isoformat(), end=end.isoformat(), parameters={},
            execution_model='bar_next_interval_v1', participation_rate='1', slippage_bps='0', seed=23))
    with formal_facts.catalog.engine.connect() as connection:
        schema = connection.scalar(text('SELECT current_schema()'))
    payload = dict(schema=schema, url=formal_facts.catalog.engine.url.render_as_string(hide_password=False),
        root=str(formal_facts.files.root), scratch=str(tmp_path), config=config,
        last_instrument_id=str(ids[-1]), change_during=change_during)
    result = subprocess.run([sys.executable, str(Path(__file__).parent/'fixtures'/'sql_preparation_scale.py')],
        input=json.dumps(payload), text=True, capture_output=True, timeout=240,
        env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1'))
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-6000:])
    measured = json.loads(result.stdout.splitlines()[-1])
    assert measured['prepared_cells'] == symbols * days
    assert measured['max_symbols_page'] <= 256
    assert measured['max_batch_rows'] <= 10000 and measured['max_batch_bytes'] <= 4*1024*1024
    assert measured['max_batch_seconds'] <= 5
    assert measured['process_peak_rss_bytes'] < measured['process_address_space_limit_bytes']
    assert measured['no_runs_or_results']
    if change_during:
        assert measured['code'] == 'DATA_CHANGED'
    else:
        assert measured['code'] == 'CAPABILITY_UNAVAILABLE'
        assert measured['source_decodes'] == dict(identity=symbols, mapping=symbols, rule=symbols)
        assert measured['rule_resolutions'] == symbols
        assert measured['cold_recheck_decodes'] == measured['final_recheck_decodes'] == 0
    # Captured for local measurements; normal successful CI does not publish it.
    print(json.dumps(dict(scope=f'{symbols}x{days}', **measured), sort_keys=True))
