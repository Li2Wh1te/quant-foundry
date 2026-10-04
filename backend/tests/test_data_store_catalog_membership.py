"""Native directory transitions, with no vendor or production connections."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.data_store.adapters.contracts import LocalInput, digest, native_json
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog_membership import CatalogMemberships
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import PipelineOptions, input_identity, run_entry
from app.data_store.readers import Query
from app.data_store.verify_coverage import verify_existing
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_domain_samples import ready
from tests.test_data_store_local_pipeline import Inputs

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTRY = BY_ID['E40']
TARGET = '561610.SH'


def directory(category, codes, seconds, *, version=100, size=10_000):
    body = {'timestamp': version, 'item': [dict(thscode=code, asset_type=category,
            name=category + ':' + code, exchange='SH', currency='CNY') for code in codes]}
    requests = [{'interface': 'meta.tickers.list',
                 'parameters': {'asset_type': category, 'limit': size, 'offset': offset}}
                for offset in range(0, (len(codes) // size + 1) * size, size)]
    source = LocalInput('tonghuashun', 'tickers', category, 'default',
                        NOW + timedelta(seconds=seconds), body, digest([body, seconds, requests]))
    return source, requests


def transition(former='fund-otc', survivor='fund-etf'):
    old, _ = directory(former, [TARGET], 1, version=90)
    positive = directory(survivor, [TARGET], 3)
    negative = directory(former, ['OTHER.SH'], 4)
    context = CatalogMemberships([positive, negative])
    return old, positive, negative, context


def proof(context, a, b):
    return context.relation((a.representation_key, TARGET, 'current'),
                            (a.group, a.order_ns, a.token), (b.group, b.order_ns, b.token))


def native_append(ready, source, requests):
    with ready.catalog.engine.begin() as connection:
        connection.execute(text('''INSERT INTO tonghuashun_observations
            (id,dataset,subject,variant,observed_at,content_hash,data_json,request_json)
            VALUES (:id,:dataset,:subject,:variant,:observed_at,:content_hash,:data_json,:request_json)'''),
            dict(id=uuid4(), dataset=source.dataset, subject=source.subject,
                 variant=source.variant, observed_at=source.observed_at,
                 content_hash=digest(source.content), data_json=native_json(source.content),
                 request_json=native_json(requests)))


def native_reset(ready):
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM tonghuashun_observations WHERE dataset='tickers'"))
        connection.execute(text("DELETE FROM tonghuashun_collection_states WHERE dataset='tickers'"))


def target_rows(ready, source, code=TARGET):
    key = (source.representation_key, code, 'current')
    part = ENTRY.spec.partitioner((*key, 'root'))
    return ready.read(ENTRY.spec, Query(partitions=(part,), lower=key,
        upper=(*key[:2], 'current\x00'), require_qualified=False)).rows


@pytest.mark.parametrize('former,survivor', [('fund-otc', 'fund-etf'), ('fund-etf', 'fund-otc')])
def test_same_complete_snapshot_proves_category_change_without_time_priority(former, survivor):
    old, positive, _, context = transition(former, survivor)
    resolved = proof(context, old, positive[0])
    assert resolved['winner'] == {'group': positive[0].group, 'order': positive[0].order_ns,
                                  'token': positive[0].token}
    assert proof(context, positive[0], old) == resolved
    # The surviving directory can be observed BEFORE the negative directory.
    # Only the former category's own observation order fences its old member.
    assert positive[0].order_ns < resolved['former']['before']


@pytest.mark.parametrize('problem', ['full_page', 'missing_page', 'wrong_scope', 'mixed_page_size',
                                  'duplicate_member', 'bad_member', 'different_version',
                                  'both_present', 'both_absent', 'tied_head', 'failed_head'])
def test_incomplete_or_ambiguous_catalogues_do_not_prove_withdrawal(problem):
    old, positive, negative, _ = transition()
    head, requests = negative
    if problem in ('full_page', 'missing_page'):
        head, requests = directory('fund-otc', ['OTHER.SH', 'OTHER2.SH'], 4, size=2)
        requests = requests[:-1]
    elif problem == 'wrong_scope':
        requests = deepcopy(requests)
        requests[0]['parameters']['asset_type'] = 'fund-etf'
    elif problem == 'mixed_page_size':
        head, requests = directory('fund-otc', ['OTHER.SH', 'OTHER2.SH'], 4, size=2)
        requests = deepcopy(requests)
        requests[1]['parameters']['limit'] = 3
    elif problem == 'duplicate_member':
        head = replace(head, content={'timestamp': 100, 'item': head.content['item'] * 2})
    elif problem == 'bad_member':
        body = deepcopy(head.content)
        body['item'].append({'thscode': None, 'asset_type': 'fund-otc'})
        head = replace(head, content=body)
    elif problem == 'different_version':
        head = replace(head, content={**head.content, 'timestamp': 101})
    elif problem == 'both_present':
        head, requests = directory('fund-otc', [TARGET], 4)
    elif problem == 'both_absent':
        positive = directory('fund-etf', ['ELSE.SH'], 3)
    elif problem == 'failed_head':
        head = replace(head, failure='SOURCE_REFRESH_FAILED')
    heads = [positive, (head, requests)]
    if problem == 'tied_head':
        heads.append((replace(head, token='0' * 64), requests))
    context = CatalogMemberships(heads)
    assert proof(context, old, positive[0]) is None


def test_complete_multipage_terminal_and_late_old_data_are_fenced():
    old, positive, _, _ = transition()
    negative = directory('fund-otc', ['OTHER.SH', 'OTHER2.SH'], 4, size=2)
    context = CatalogMemberships([positive, negative])
    assert len(negative[1]) == 2 and proof(context, old, positive[0])
    # A same-time or later assertion in the former category is outside the
    # proven absence, even if a processing caller presents it out of order.
    for seconds in (4, 5):
        late = replace(old, observed_at=NOW + timedelta(seconds=seconds))
        assert proof(context, late, positive[0]) is None
    later_positive = replace(positive[0], observed_at=NOW + timedelta(seconds=6))
    assert proof(context, old, later_positive) is None


@pytest.mark.parametrize('former,survivor', [('fund-otc', 'fund-etf'), ('fund-etf', 'fund-otc')])
def test_real_native_reprocessing_repairs_exact_old_conflict_and_preserves_basis(ready, former, survivor):
    native_reset(ready)
    old, positive, negative, _ = transition(former, survivor)
    # Reproduce the old per-code reduction without a complete membership
    # context, then repair through actual native PostgreSQL snapshots.
    prior = run_entry(ready, ENTRY, Inputs(old, positive[0]))
    assert prior['complete'] and not prior['qualified']
    assert [x['reason'] for x in ready.catalog.issues(ENTRY.spec.name)] == ['SOURCE_ORDER_UNCOMPARABLE']
    for raw, requests in ((old, []), positive, negative):
        native_append(ready, raw, requests)
    result = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert result['complete'] and result['qualified'] and result['resolved'] == 1
    rows = target_rows(ready, old)
    actual = list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    winner = next(raw for raw in actual if raw.subject == survivor)
    assert len(rows) == 1 and rows[0]['f0_asset_type'] == survivor
    assert (rows[0]['basis_group'], int(rows[0]['basis_ns']), rows[0]['basis_token']) == (
        winner.group, winner.order_ns, winner.token)
    files = [(x['path'], x['content_hash']) for x in ready.catalog.files(
        ENTRY.spec.name, ENTRY.spec.partitioner((*next(normalize(ENTRY, old)).key, 'root')))]
    verification = verify_existing(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert verification['complete'] and verification['missing_objects'] == 0
    assert verification['mismatched_objects'] == 0 and verification['unexpected_objects'] == 0
    repeated = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert repeated['qualified'] and not ready.catalog.issues(ENTRY.spec.name)
    assert files == [(x['path'], x['content_hash']) for x in ready.catalog.files(
        ENTRY.spec.name, ENTRY.spec.partitioner((*next(normalize(ENTRY, old)).key, 'root')))]


def test_refresh_failures_remain_real_after_member_transition(ready):
    native_reset(ready)
    old, positive, negative, _ = transition()
    assert not run_entry(ready, ENTRY, Inputs(old, positive[0]))['qualified']
    for raw, requests in ((old, []), positive, negative):
        native_append(ready, raw, requests)
    with ready.catalog.engine.begin() as connection:
        connection.execute(text('''INSERT INTO tonghuashun_collection_states
            (dataset,status,subject,variant,attempted_at,error_kind)
            VALUES ('tickers','failed',:subject,'default',:at,'invalid_data')'''),
            [dict(subject=category, at=NOW + timedelta(seconds=5))
             for category in ('fund-etf', 'fund-otc')])
    result = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert result['complete'] and not result['qualified'] and result['resolved'] == 1
    assert [x['reason'] for x in ready.catalog.issues(ENTRY.spec.name)] == [
        'SOURCE_REFRESH_FAILED', 'SOURCE_REFRESH_FAILED']
    verified = verify_existing(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert verified['complete'] and verified['disposed_failures'] == 2
    assert not run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))['qualified']


@pytest.mark.parametrize('problem', ['different_versions', 'both_latest_absent'])
def test_native_missing_or_different_version_heads_keep_identity_problem(ready, problem):
    native_reset(ready)
    old, positive, negative, _ = transition()
    prior_positive = directory('fund-etf', [TARGET], 2, version=95)
    if problem == 'different_versions':
        negative = directory('fund-otc', ['OTHER.SH'], 4, version=101)
    else:
        positive = directory('fund-etf', ['ELSE.SH'], 3)
    for raw, requests in ((old, []), prior_positive, positive, negative):
        native_append(ready, raw, requests)
    result = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert result['complete'] and not result['qualified']
    assert any(issue['reason'] == 'SOURCE_ORDER_UNCOMPARABLE'
               for issue in ready.catalog.issues(ENTRY.spec.name))
    # Complete enumeration may withdraw category membership, but neither this
    # rule nor the directory collector invents delisting or successor identity.
    assert len(target_rows(ready, old)) == 1


def test_saved_member_proof_survives_checkpoint_without_reading_new_input(ready):
    native_reset(ready)
    # Place the repaired object after an unrelated partition, so the first
    # bounded invocation seals its proof but cannot yet load the old object.
    code = next('TEST'+str(index)+'.SH' for index in range(100)
                if ENTRY.spec.partitioner((directory('fund-etf', [], 1)[0].representation_key,
                                           'TEST'+str(index)+'.SH', 'current', 'root')).endswith('b15'))
    old, _ = directory('fund-otc', [code], 1, version=90)
    positive = directory('fund-etf', [code], 3)
    negative = directory('fund-otc', ['OTHER.SH'], 4)
    run_entry(ready, ENTRY, Inputs(old, positive[0]))
    for raw, requests in ((old, []), positive, negative):
        native_append(ready, raw, requests)
    first = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine),
                      options=PipelineOptions(maximum_partition_passes=1))
    assert not first['complete'] and first['committed_partitions'] == 1
    assert ready.budget.pending_keys()
    # Later local data belongs to a future complete pass. Resuming this seal
    # neither acquires it nor borrows its contents to alter the saved proof.
    newer, requests = directory('fund-otc', [code], 6, version=101)
    native_append(ready, newer, requests)
    resumed = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert resumed['resumed'] and resumed['source_rows'] == 0 and resumed['qualified']
    assert target_rows(ready, old, code)[0]['f0_asset_type'] == 'fund-etf'
    assert not ready.budget.pending_keys()
    next_scan = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert next_scan['complete'] and not next_scan['qualified']
    assert any(x['reason'] == 'SOURCE_ORDER_UNCOMPARABLE' for x in ready.catalog.issues(ENTRY.spec.name))


def test_pre_rule_seal_cannot_borrow_new_catalogue_proof():
    source = Inputs()
    descriptor = [ENTRY.spec.descriptor(), type(source).__module__, type(source).__qualname__,
                  'update', (), False]
    assert input_identity(ENTRY, source, PipelineOptions()) != digest(descriptor)
