"""Issue filtering executes on real PostgreSQL without requiring file storage."""
import os
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data_store.catalog import Catalog
from app.data_store.limits import StoreLimits
from app.data_store.router import BUSINESS
from app.db.session import get_db_session
from app.main import create_app
from tests.test_data_store_api import ASGIClient
from tests.test_data_store_kernel import database  # noqa: F401 - isolated PostgreSQL schema

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='isolated PostgreSQL required')
TOKEN = 'a' * 64


@pytest.fixture
def issue_api(database):
    engine, _ = database
    first, second = list(BUSINESS.values())[:2]
    catalog = Catalog(engine, StoreLimits())
    catalog.register(first.spec)
    catalog.register(second.spec)
    with engine.begin() as connection:
        connection.execute(text('''CREATE TABLE data_store_legacy_restrictions (
            origin_key text PRIMARY KEY, dataset text, scope_key text,
            captured_at timestamptz DEFAULT clock_timestamp())'''))
        for index, entry in enumerate([first, second]):
            target = {'scope_version': 'object-key-set-v1', 'members': [['r', 's', 'k1'], ['r', 's', 'k2']]}
            connection.execute(text('''INSERT INTO data_store_issues
                (dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json)
                VALUES (:dataset,:key,:scope,'FULL_RANGE_UNPROVEN','fixture',:target,'{}')'''),
                {'dataset': entry.spec.name, 'key': f'fixture-{index}', 'scope': f'scope-{index}', 'target': json.dumps(target)})
        connection.execute(text("INSERT INTO data_store_legacy_restrictions(origin_key,dataset,scope_key) VALUES ('global',NULL,'global-scope')"))
    app = create_app(Settings(api_token=TOKEN, cursor_signing_key='b' * 64,
                              database_password='isolated-test', environment='test', scheduler_enabled=False))
    def session_dependency():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db_session] = session_dependency
    return ASGIClient(app), first.spec.name, second.spec.name


def test_issue_dataset_filter_keeps_global_restrictions_and_member_counts(issue_api):
    client, dataset, other = issue_api
    from urllib.parse import quote
    response = client.get(f'/api/admin/data-store/issues?dataset={quote(dataset, safe="")}&limit=1', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 2 and body['affected_objects'] == 3 and body['next_offset'] == 1
    second = client.get(f'/api/admin/data-store/issues?dataset={quote(dataset, safe="")}&limit=1&offset=1', headers={'Authorization': f'Bearer {TOKEN}'})
    assert second.status_code == 200
    rows = body['items'] + second.json()['items']
    assert {row['dataset'] for row in rows} == {dataset, None}
    assert all(row['dataset'] != other for row in rows)
    assert second.json()['next_offset'] is None


def test_issue_unfiltered_null_parameter_and_authentication(issue_api):
    client, _, _ = issue_api
    assert client.get('/api/admin/data-store/issues').status_code == 401
    response = client.get('/api/admin/data-store/issues?limit=20', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200
    assert response.json()['total'] == 3 and response.json()['affected_objects'] == 5
    assert response.json()['next_offset'] is None
