"""Current-only inventory contracts on isolated PostgreSQL; no business files."""
from dataclasses import replace
import json
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import text

from app.data_store import router as routes
from app.data_store import current_revision
from app.data_store.tables import entry_status as entry_tables
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Catalog
from app.data_store.limits import StoreLimits
from app.data_store.readers import Query
from app.data_store.issue_scope import relevant_issue_count
from tests.test_data_store_kernel import database  # noqa: F401
from tests.test_data_store_issues_api import issue_api, TOKEN  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                              reason='isolated PostgreSQL required')
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
PREFIX = '/api/admin/data-store'


def issues(client, dataset=None, **params):
    if dataset is not None:
        params['dataset'] = dataset
    return client.get(PREFIX + '/issues?' + urlencode(params), headers=HEADERS)


def test_equal_sort_fields_have_complete_stable_order(issue_api, database):
    client, dataset, _ = issue_api
    engine, _ = database
    with engine.begin() as c:
        c.execute(text('DELETE FROM data_store_issues'))
        c.execute(text('DELETE FROM data_store_legacy_restrictions'))
        for i in reversed(range(43)):
            c.execute(text("INSERT INTO data_store_issues "
                "(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json,last_seen) "
                "VALUES (:d,:k,'same-scope',:r,'isolated','{}','{}','2026-10-11T00:00:00Z')"),
                {'d': dataset, 'k': f'key-{i:02}', 'r': f'ISOLATED_{i:02}'})
    first = issues(client, dataset, limit=20).json()
    second = issues(client, dataset, limit=20, offset=20, revision=first['revision']).json()
    third = issues(client, dataset, limit=20, offset=40, revision=first['revision']).json()
    assert [r['reason'] for page in (first, second, third) for r in page['items']] == [f'ISOLATED_{i:02}' for i in range(43)]
    assert first['revision'] == second['revision'] == third['revision']
    assert third['next_offset'] is None
    assert all('record_key' not in r for r in first['items'])


def test_same_counts_and_timestamps_cannot_reuse_old_issue_revision(issue_api, database):
    client, dataset, _ = issue_api
    engine, _ = database
    first = issues(client, dataset, limit=1).json()
    with engine.begin() as c:
        c.execute(text("UPDATE data_store_issues SET reason='CURRENT_SOURCE_MISMATCH' WHERE dataset=:d"), {'d': dataset})
    rejected = issues(client, dataset, limit=1, offset=1, revision=first['revision'])
    assert rejected.status_code == 409
    assert rejected.json()['detail']['code'] == 'DATA_CHANGED'
    fresh = issues(client, dataset, limit=1).json()
    assert fresh['total'] == first['total']
    assert fresh['affected_objects'] == first['affected_objects']
    assert fresh['revision'] != first['revision']
    assert issues(client, dataset, limit=1, offset=1, revision=fresh['revision']).status_code == 200


def test_issue_revision_is_bound_to_filter_and_global_restrictions(issue_api, database):
    client, dataset, other = issue_api
    engine, _ = database
    first = issues(client, dataset, limit=1).json()
    with engine.begin() as c:
        c.execute(text("UPDATE data_store_issues SET reason='CURRENT_SOURCE_MISMATCH' WHERE dataset=:d"), {'d': other})
    assert issues(client, dataset, offset=1, revision=first['revision']).status_code == 200
    assert issues(client, other, revision=first['revision']).status_code == 409
    with engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_restrictions SET scope_key='changed-global-scope'"))
    assert issues(client, dataset, offset=1, revision=first['revision']).status_code == 409


def test_page_totals_and_revision_share_one_current_sql_view(issue_api, database, monkeypatch):
    client, dataset, _ = issue_api
    engine, _ = database
    original = routes.inventory_revision
    def concurrent_change(*args, **kwargs):
        revision = original(*args, **kwargs)
        with engine.begin() as c:
            c.execute(text('DELETE FROM data_store_issues WHERE dataset=:d'), {'d': dataset})
        return revision
    with monkeypatch.context() as patch:
        patch.setattr(routes, 'inventory_revision', concurrent_change)
        first = issues(client, dataset, limit=20).json()
    assert first['total'] == len(first['items']) == 2
    assert first['affected_objects'] == 3
    assert issues(client, dataset, revision=first['revision']).status_code == 409


def test_inventory_budget_and_bad_revision_fail_explicitly(issue_api, monkeypatch):
    client, dataset, _ = issue_api
    assert issues(client, dataset, revision='unsafe').status_code == 422
    assert issues(client, dataset, revision='0' * 64).status_code == 409
    monkeypatch.setattr(current_revision, 'ROW_LIMIT', 1)
    response = issues(client, dataset)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'CONTROL_BUDGET_EXCEEDED'
    assert client.get(PREFIX + '/issues?revision=' + '0' * 64).status_code == 401


def test_catalog_reaches_beyond_1000_and_rejects_same_count_changes(issue_api, database, monkeypatch):
    client, _, _ = issue_api
    engine, _ = database
    with engine.begin() as c:
        entry_tables.create(c)
        c.execute(text('CREATE TABLE data_store_legacy_maintenance (singleton integer PRIMARY KEY,phase text NOT NULL)'))
        c.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'rebuilding')"))
    template = BY_ID['E07']
    entries = []
    for i in range(1050):
        entries.append(SimpleNamespace(id=f'isolated-{i:04}', spec=replace(template.spec, name=f'isolated.catalog_{i:04}'),
            domain=template.domain, source=template.source, native=template.native, disposition='business', projection=template.projection,
            describe_capability=template.describe_capability))
    monkeypatch.setattr(routes, 'BUSINESS', {e.spec.name: e for e in reversed(entries)})
    pages = []
    revision = None
    for offset in range(0, 1050, 100):
        params = {'limit': 100, 'offset': offset}
        if revision:
            params['revision'] = revision
        response = client.get(PREFIX + '/datasets?' + urlencode(params), headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body['total'] == 1050 and body['phase'] == 'rebuilding'
        assert all(item['status'] == 'rebuilding' for item in body['items'])
        revision = body['revision']
        pages.extend(item['dataset'] for item in body['items'])
    assert pages == [e.spec.name for e in entries]
    assert body['next_offset'] is None
    with engine.begin() as c:
        c.execute(text("UPDATE data_store_legacy_restrictions SET scope_key='same-count-change'"))
    stale = client.get(PREFIX + '/datasets?' + urlencode({'offset':1000, 'revision':revision}), headers=HEADERS)
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'DATA_CHANGED'


def test_catalog_unrelated_database_writes_do_not_change_its_revision(issue_api, database, monkeypatch):
    client, _, _ = issue_api
    engine, _ = database
    with engine.begin() as c:
        entry_tables.create(c)
        c.execute(text('CREATE TABLE data_store_legacy_maintenance (singleton integer PRIMARY KEY,phase text NOT NULL)'))
        c.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'rebuilding')"))
        c.execute(text('CREATE TABLE isolated_unrelated (id integer PRIMARY KEY,value text)'))
    first = client.get(PREFIX + '/datasets?limit=1', headers=HEADERS).json()
    with engine.begin() as c:
        c.execute(text("INSERT INTO isolated_unrelated VALUES (1,'isolated')"))
    next_page = client.get(PREFIX + '/datasets?' + urlencode({'limit':1,'offset':1,'revision':first['revision']}), headers=HEADERS)
    assert next_page.status_code == 200 and next_page.json()['revision'] == first['revision']


@pytest.mark.parametrize('restriction,count', [
    ({'blocking_objects':1},1), ({'blocking_objects':1,'partition':'p0'},1),
    ({'blocking_objects':1,'partition':'p1'},0), ('unknown-shape',1),
])
def test_unlocated_overflow_is_not_default_allowed_while_known_other_scope_stays_isolated(issue_api, database, restriction, count):
    engine, _ = database
    entry = BY_ID['E07']
    catalog = Catalog(engine, StoreLimits())
    catalog.register(entry.spec)
    with engine.begin() as c:
        entry_tables.create(c)
        c.execute(text('INSERT INTO data_store_entry_status(entry_id,summary_json) VALUES (:i,:s)'),
            {'i':entry.id,'s':json.dumps({'overflow_restriction':restriction})})
    store = SimpleNamespace(catalog=catalog,limits=StoreLimits())
    query = Query(partitions=('p0',),lower=('r','s','k'),upper=('r','s','k\x00'))
    assert relevant_issue_count(store,entry.spec,query) == count
