"""Complete manager collections can fix only their matching current restriction."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os

import pytest

from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_local_pipeline import Inputs, NOW, ready, rows

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')


def manager(n=2, *, subject='synthetic-manager', history=None, extra=None, failure=None):
    if history is None:
        history = {'FUND-A.OF': {'code': 'FUND-A.OF', 'start': '2020-01-01', 'name': 'A'},
                   'FUND-B.OF': {'code': 'FUND-B.OF', 'start': '2021-01-01', 'name': 'B'}}
    body = {'item': [{'investment_history': history}], **(extra or {})}
    return LocalInput('tonghuashun', 'fund_manager_experience', subject, 'default',
                      NOW + timedelta(seconds=n), body, digest([body, n, subject]), failure=failure)


def problems(st, entry):
    return st.catalog.issues(entry.spec.name, limit=100)


def scope_problem(st, entry, subject):
    found = [p for p in problems(st, entry)
             if json.loads(p['target_json'])['prefix'][1:] == [subject]]
    assert len(found) == 1
    return found[0]


def failed(st, entry, source):
    result = run_entry(st, entry, Inputs(replace(source, failure='SOURCE_REFRESH_FAILED')))
    assert result['complete'] and not result['qualified']
    return scope_problem(st, entry, source.subject)


@pytest.mark.parametrize('extra', [None, {'complete': True, 'has_more': False, 'failed_requests': [], 'next_cursor': ''}])
def test_complete_manager_collection_resolves_exact_old_refresh_and_keeps_values(ready, extra):
    entry = BY_ID['E54']
    first = manager(0)
    run_entry(ready, entry, Inputs(first))
    unit = next(normalize(entry, first))
    before = rows(ready, entry, unit, qualified=False)
    old = failed(ready, entry, manager(1))
    assert json.loads(old['target_json'])['prefix'] == [first.representation_key, first.subject]

    source = manager(2, extra=extra)
    result = run_entry(ready, entry, Inputs(source))

    assert result['complete'] and result['resolved'] == 1
    assert not problems(ready, entry)
    after = rows(ready, entry, unit, qualified=False)
    business = lambda rs: [{k: v for k, v in r.items() if not k.startswith('basis_')} for r in rs]
    assert business(before) == business(after)
    assert {r['object_key'] for r in after} == {u.object_key for u in normalize(entry, first)}
    assert all(int(r['basis_ns']) == manager(2).order_ns for r in after)
    assert all(json.loads(r['quality_json'])['performance'] == 'MISSING' for r in after)
    generation = ready.catalog.dataset(entry.spec.name)['generation']
    files = ready.catalog.files(entry.spec.name, entry.spec.partitioner((*unit.key, 'root')))
    run_entry(ready, entry, Inputs(source))
    assert ready.catalog.dataset(entry.spec.name)['generation'] == generation
    assert ready.catalog.files(entry.spec.name, entry.spec.partitioner((*unit.key, 'root'))) == files


@pytest.mark.parametrize('extra', [
    {'complete': False}, {'has_more': True}, {'next_cursor': 'next'},
    {'failed_requests': [{'interface': 'synthetic', 'error_kind': 'timeout'}]},
    {'failed_requests': None}, {'failed_requests': False},
    {'complete': None}, {'complete': 1}, {'has_more': None}, {'has_more': 0},
])
def test_partial_or_malformed_collection_cannot_clear_subject_failure(ready, extra):
    entry = BY_ID['E54']
    failed(ready, entry, manager(1))
    result = run_entry(ready, entry, Inputs(manager(2, extra=extra)))
    remaining = scope_problem(ready, entry, 'synthetic-manager')
    assert not result['qualified']
    assert json.loads(remaining['target_json'])['blocking']


@pytest.mark.parametrize('kind', ['missing_history', 'empty_history', 'bad_child_code', 'bad_child_text', 'extra_container', 'non_object_container'])
def test_incomplete_or_bad_atomic_members_never_gain_scope_proof(ready, kind):
    entry = BY_ID['E54']
    failed(ready, entry, manager(1))
    source = manager(2)
    body = deepcopy(source.content)
    if kind == 'missing_history':
        body['item'][0].pop('investment_history')
    elif kind == 'empty_history':
        body['item'][0]['investment_history'] = {}
    elif kind == 'bad_child_code':
        body['item'][0]['investment_history']['FUND-B.OF']['code'] = 'OTHER.OF'
    elif kind == 'bad_child_text':
        body['item'][0]['investment_history']['FUND-B.OF']['start'] = 123
    elif kind == 'extra_container':
        body['item'].append(deepcopy(body['item'][0]))
    if kind == 'non_object_container':
        source = replace(source, content=body['item'], representation='local_table_baseline', token=digest(body['item']))
    else:
        source = replace(source, content=body, token=digest(body))
    result = run_entry(ready, entry, Inputs(source))
    assert not result['qualified'] and scope_problem(ready, entry, source.subject)


@pytest.mark.parametrize('kind', ['older', 'other_subject', 'other_group'])
def test_scope_requires_matching_subject_group_and_sufficient_order(ready, kind):
    entry = BY_ID['E54']
    old = failed(ready, entry, manager(3))
    source = manager(2 if kind == 'older' else 4)
    if kind == 'other_subject':
        source = manager(4, subject='different-manager')
    elif kind == 'other_group':
        source = replace(source, order_kind='source_revision')
    result = run_entry(ready, entry, Inputs(source))
    assert not result['qualified']
    assert scope_problem(ready, entry, 'synthetic-manager') == old


def test_later_failed_refresh_stays_blocked_after_good_collection(ready):
    entry = BY_ID['E54']
    failed(ready, entry, manager(1))
    latest_failure = replace(manager(3), failure='SOURCE_REFRESH_FAILED')
    result = run_entry(ready, entry, Inputs(manager(2), latest_failure))
    problem = scope_problem(ready, entry, 'synthetic-manager')
    assert not result['qualified'] and problem['evidence_token'] == latest_failure.token
    assert int(json.loads(problem['target_json'])['order']) == latest_failure.order_ns


def test_partial_response_cannot_reuse_earlier_good_scope_proof(ready):
    entry = BY_ID['E54']
    failed(ready, entry, manager(1))
    partial = manager(3, extra={'failed_requests': [{'interface': 'synthetic', 'error_kind': 'timeout'}]})
    result = run_entry(ready, entry, Inputs(manager(2), partial))
    problem = scope_problem(ready, entry, 'synthetic-manager')
    assert not result['qualified']
    assert problem['reason'] == 'REPORT_INCOMPLETE'
    assert problem['evidence_token'] == partial.token


def test_fix_preserves_unrelated_same_partition_issue_metadata(ready):
    entry = BY_ID['E54']
    source = manager(2)
    unit = next(normalize(entry, source))
    partition = entry.spec.partitioner((*unit.key, 'root'))
    candidates = (manager(1, subject='sibling-'+str(i)) for i in range(1000))
    sibling = next(raw for raw in candidates
                   if entry.spec.partitioner((*next(normalize(entry, raw)).key, 'root')) == partition)
    failed(ready, entry, manager(1))
    failed(ready, entry, sibling)
    before = scope_problem(ready, entry, sibling.subject)
    run_entry(ready, entry, Inputs(source))
    assert scope_problem(ready, entry, sibling.subject) == before
    assert len(problems(ready, entry)) == 1


@pytest.mark.parametrize('entry_id', ['E17', 'E21'])
def test_successful_window_cannot_clear_unproven_whole_subject(ready, entry_id):
    entry = BY_ID[entry_id]
    body = ({'item': [{'stock_ratio_pct': '50', 'report_date_ms': 1609430400000}]}
            if entry_id == 'E17' else
            {'item': [{'ex_dividend_date_ms': 1609430400000, 'per_ten_cash_before_tax': '1'},
                      {'ex_dividend_date_ms': 1625068800000, 'per_ten_cash_before_tax': '2'}]})
    source = LocalInput('tonghuashun', entry.native, 'SYNTHETIC.OF', 'default',
                        NOW + timedelta(seconds=2), body, digest(body))
    old = failed(ready, entry, replace(source, observed_at=NOW + timedelta(seconds=1)))
    assert all(not u.failure for u in normalize(entry, source))
    result = run_entry(ready, entry, Inputs(source))
    assert not result['qualified'] and scope_problem(ready, entry, source.subject) == old
