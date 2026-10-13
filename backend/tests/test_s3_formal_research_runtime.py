"""FormalProvider composition regression on real PG/CurrentStore/IPC/Rust.

These explicitly synthetic accepted-fact DTOs test the provider's composition
port. They do NOT prove the still-missing default production fact resolver.
No test constructs Binding or independently supplies worker research routes.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pyarrow as pa
import pytest

from tests.test_s3_formal_runtime import database, store, formal, nodes
from tests.test_s3_formal_runtime import limits as formal_limits
from tests.test_s3_data_gateway import quality
from app.backtest_service.data_gateway import GatewayError, serve
from app.backtest_service.formal_runtime import AcceptedRunPlan
from app.backtest_service.formal_research import FormalDailyResearchGateway
from app.backtest_service.runtime_contracts import Claim
from app.backtest_service.service import FormalProvider

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PG required')
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from s3_research_fixtures import research_nodes


@pytest.fixture
def limits(formal_limits):
    # The minute fixture has 1,920 rows; keep it inside the existing changed-file
    # cap instead of turning an unrelated ten-row Parquet fixture into 192 files.
    return replace(formal_limits, file_rows=1000)


def composed(formal, *, missing=None):
    market, packet, work = nodes(formal)
    research = []
    for binding in research_nodes(formal, packet['calendar']):
        kind = binding.kind
        if missing == 'public_time' and kind == 'fundamentals':
            binding = replace(binding, accepted_facts=tuple(f for f in binding.accepted_facts if f != 'verified_public_time'))
        if missing == 'anchor' and kind == 'adjustment':
            binding = replace(binding, factor_anchor=None)
        if missing == 'complete_set' and kind == 'index_stocks':
            binding = replace(binding, accepted_facts=tuple(f for f in binding.accepted_facts if f != 'verified_complete_sets'))
        research.append(binding)
    def check_facts(accepted, *, connection):
        assert accepted is work
    plan = AcceptedRunPlan(market, packet, check_facts=check_facts,
                           research=tuple(research), reference_indices=('REF',))
    process = SimpleNamespace(image='isolated-prebuilt-worker', daemon=SimpleNamespace(preflight=lambda _: None))
    provider = FormalProvider(SimpleNamespace(environment='test'), store=formal,
                              plan_resolver=lambda _: plan, process=process)
    return provider, work, plan


def execute(actual, tmp_path, source):
    from quantfoundry._transport import Channel
    from quantfoundry._worker import _run_module
    from quantfoundry.data import _MarketRoute
    listener = socket.socket(socket.AF_UNIX)
    endpoint = tmp_path / 'data.sock'
    listener.bind(str(endpoint)); listener.listen(1)
    actual.spec['source']['socket_path'] = str(endpoint)
    errors = []
    def peer():
        try:
            connection, _ = listener.accept()
            serve(Channel(connection), actual.gateway, actual.context, close_context=False)
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=peer, daemon=True); thread.start()
    module = SimpleNamespace()
    exec(source, module.__dict__)
    result = None
    try:
        routes = {f: _MarketRoute(row['binding'], tuple(row['fields'])) for f, row in actual.market_routes.items()}
        result = _run_module(module, actual.spec, market_routes=routes, research_routes=actual.research_routes)
        return json.loads(result.summary_json())
    finally:
        if result is not None:
            result.close()
        listener.close(); thread.join(5)
        assert not thread.is_alive() and not errors, errors


def test_ordinary_formal_binding_delivers_research_and_factors_on_one_context(formal, tmp_path):
    provider, work, _ = composed(formal)
    actual = provider.binding(work, Claim('formal-research-run', 'token', 1), Event())
    assert isinstance(actual.gateway, FormalDailyResearchGateway)
    assert set(actual.research_routes) == {'fundamentals', 'valuation', 'industry', 'instruments', 'index_stocks', 'adjustment', 'price'}
    assert set(actual.context.dependencies) == {actual.spec['source']['binding']}
    try:
        result = execute(actual, tmp_path, '''from decimal import Decimal
from quantfoundry.api import (get_fundamentals, get_valuation, get_industry,
                             get_instruments, get_index_stocks, get_price, record)
def handle_data(c, data):
    assert get_fundamentals(['A.SH'], fields=['net_profit']).iloc[0]['net_profit'] == Decimal('120')
    assert get_valuation(['A.SH'], fields=['pe_ratio']).iloc[0]['pe_ratio'] == Decimal('12')
    assert get_industry(['A.SH']).iloc[0]['industry_code'] == 'I1'
    assert get_instruments(['A.SH']).iloc[0]['name'] == 'Fixture A'
    assert get_index_stocks('REF') == ('A.SH',)
    reference = get_price('REF', count=1)
    assert reference.iloc[0]['close'] == Decimal('3000.125')
    assert reference.attrs['qf']['units']['close'] == 'index_points'
    prices = get_price('A.SH', count=2, adjustment='post')
    assert list(prices['close']) and all(p == Decimal('20') for p in prices['close'])
    record(research_calls=6)
''')
        assert result['outcome']['status'] == 'succeeded', json.dumps(result)
        assert set(actual.context.dependencies) == {actual.spec['source']['binding'], *actual.research_routes.values()}
        used = actual.gateway.bindings[actual.research_routes['fundamentals']].spec
        quality(formal, used)
        with pytest.raises(GatewayError) as changed:
            actual.gateway.finalize(actual.context, lambda _: pytest.fail('changed research cannot commit success'))
        assert changed.value.code == 'DATA_CHANGED'
    finally:
        actual.gateway.close(actual.context)


@pytest.mark.parametrize('missing,call', [
    ('public_time', "get_fundamentals(['A.SH'], fields=['net_profit'])"),
    ('anchor', "get_price('A.SH', count=1, adjustment='post')"),
    ('complete_set', "get_index_stocks('REF')"),
])
def test_composed_formal_routes_keep_missing_fact_refusals(formal, tmp_path, missing, call):
    provider, work, _ = composed(formal, missing=missing)
    actual = provider.binding(work, Claim('formal-missing-' + missing, 'token', 1), Event())
    try:
        result = execute(actual, tmp_path, 'from quantfoundry.api import get_fundamentals, get_price, get_index_stocks\n'
                         + 'def handle_data(c, data):\n    ' + call + '\n')
        assert result['outcome']['status'] == 'failed', result
        assert result['outcome']['error']['code'] == 'CAPABILITY_UNAVAILABLE', result
    finally:
        actual.gateway.close(actual.context)


@pytest.mark.parametrize('call,code', [
    ("get_fundamentals(['A.SH'], fields=['net_profit'], as_of=c.now_ns+1)", 'LOOKAHEAD_FORBIDDEN'),
    ("get_index_stocks('UNAUTHORIZED')", 'DATA_RESTRICTED'),
    ("get_price('UNAUTHORIZED', count=1)", 'DATA_RESTRICTED'),
])
def test_composed_research_keeps_time_and_reference_scope(formal, tmp_path, call, code):
    provider, work, _ = composed(formal)
    actual = provider.binding(work, Claim('formal-restricted', 'token', 1), Event())
    try:
        result = execute(actual, tmp_path, 'from quantfoundry.api import get_fundamentals, get_index_stocks, get_price\n'
                         + 'def handle_data(c, data):\n    ' + call + '\n')
        assert result['outcome']['status'] == 'failed', result
        assert result['outcome']['error']['code'] == code, result
    finally:
        actual.gateway.close(actual.context)


def test_resampled_market_and_research_use_the_same_context(formal, tmp_path):
    from tests.test_s3_resampled_runtime import accepted_minutes
    from app.backtest_service.formal_research import ResampledResearchGateway
    provider, work, _, _ = accepted_minutes(formal)
    original = provider._plan(work)
    plan = replace(original, research=research_nodes(formal, original.spec['calendar']), reference_indices=('REF',))
    provider.plan_resolver = lambda _: plan
    actual = provider.binding(work, Claim('resampled-research', 'token', 1), Event())
    try:
        assert isinstance(actual.gateway, ResampledResearchGateway)
        result = execute(actual, tmp_path, '''from quantfoundry.api import get_fundamentals, get_price
def handle_data(c, data):
    assert str(get_fundamentals(['A.SH'], fields=['net_profit']).iloc[0]['net_profit']) == '120'
    assert len(get_price(['A.SH'], count=2)) > 0
    assert str(get_price('REF', frequency='1d', count=1).iloc[0]['close']) == '3000.125'
''')
        assert result['outcome']['status'] == 'succeeded', json.dumps(result)
        assert actual.research_routes['fundamentals'] in actual.context.dependencies
        assert actual.research_routes['price'] in actual.context.dependencies
    finally:
        actual.gateway.close(actual.context)
