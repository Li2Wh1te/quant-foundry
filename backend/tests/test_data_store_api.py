"""Current API admission and pagination against an isolated PostgreSQL schema."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import json
import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data_store.adapters.registry import BY_ID
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import run_entry
from app.data_store import router as store_router
from app.db.session import get_db_session
from app.main import create_app
from tests.test_data_store_domain_samples import ready  # noqa: F401 - fixture
from tests.test_data_store_kernel import database, limits, store  # noqa: F401 - fixtures


pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1",
                                reason="isolated PostgreSQL required")
TOKEN = "a" * 64


@dataclass
class ASGIResponse:
    status_code: int
    text: str

    def json(self):
        return json.loads(self.text)


class ASGIClient:
    """Small transport for real route/dependency execution without httpx2."""

    def __init__(self, app):
        self.app = app

    def get(self, path, *, headers=None):
        return self._request("GET", path, headers=headers)

    def post(self, path, *, headers=None, json=None):
        return self._request("POST", path, headers=headers, payload=json)

    def _request(self, method, target, *, headers=None, payload=None):
        async def execute():
            parts = urlsplit(target)
            body = b"" if payload is None else json.dumps(payload).encode()
            sent = False
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": method, "scheme": "http",
                "path": parts.path, "raw_path": parts.path.encode(),
                "query_string": parts.query.encode(),
                "headers": [(key.lower().encode(), value.encode())
                            for key, value in (headers or {}).items()]
                           + ([(b"content-type", b"application/json")] if payload is not None else []),
                "client": ("test", 1), "server": ("test", 80), "root_path": "",
            }

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            async def send(message):
                messages.append(message)

            await self.app(scope, receive, send)
            status = next(item["status"] for item in messages
                          if item["type"] == "http.response.start")
            content = b"".join(item.get("body", b"") for item in messages
                               if item["type"] == "http.response.body")
            return ASGIResponse(status, content.decode())

        return asyncio.run(execute())


@pytest.fixture
def api(ready, monkeypatch):
    engine = ready.catalog.engine
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scheduled_tasks (id uuid PRIMARY KEY, task_type text, state text)"))
        connection.execute(text("CREATE TABLE task_runs (id uuid PRIMARY KEY, task_type text, status text)"))
        connection.execute(text("""CREATE TABLE data_store_legacy_maintenance (
            singleton integer PRIMARY KEY, phase text NOT NULL, plan_hash text,
            completed_json jsonb NOT NULL DEFAULT '[]', files_started boolean NOT NULL DEFAULT false)"""))
        connection.execute(text("""CREATE TABLE data_store_legacy_restrictions (
            origin_key text PRIMARY KEY, dataset text, scope_key text,
            captured_at timestamptz DEFAULT clock_timestamp())"""))
    monkeypatch.setattr(store_router, "get_engine", lambda: engine)
    monkeypatch.setattr(store_router, "CurrentStore",
                        partial(store_router.CurrentStore, limits=ready.limits))
    settings = Settings(
        api_token=TOKEN, cursor_signing_key="b" * 64,
        database_password="isolated-test", data_store_root=ready.files.root,
        environment="test", scheduler_enabled=False,
    )
    app = create_app(settings)

    def session_dependency():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    return ASGIClient(app), ready


def query_body(entry, preview_key, **overrides):
    body = {
        "dataset": entry.spec.name, "frequency": "report",
        "representation": preview_key["representation"],
        "subject": preview_key["subject"],
        "from_key": preview_key["object_key"],
        "to_key": preview_key["object_key"],
        "columns": ["representation", "subject", "object_key", "member_key"],
        "page_size": 1,
    }
    return {**body, **overrides}


def test_auth_legacy_refusal_and_fresh_empty_catalog(api):
    client, _ = api
    assert client.get("/api/admin/data-store/datasets").status_code == 401
    assert client.get("/api/admin/data-foundation/query").status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    catalog = client.get("/api/admin/data-store/datasets?limit=2", headers=headers)
    assert catalog.status_code == 200
    assert catalog.json()["phase"] == "ready"
    assert len(catalog.json()["items"]) == 2
    assert all(item["status"] == "not_checked" for item in catalog.json()["items"])
    retired = client.get("/api/admin/data-foundation/query?release_id=old", headers=headers)
    assert retired.status_code == 410
    assert retired.json()["detail"]["code"] == "LEGACY_FOUNDATION_REMOVED"


def test_existing_legacy_rows_and_reset_receipt_hold_queries(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    body = query_body(entry, {"representation": "r", "subject": "s", "object_key": "k"})
    with store.catalog.engine.begin() as connection:
        connection.execute(text("CREATE TABLE foundation_artifacts (id integer PRIMARY KEY)"))
        connection.execute(text("INSERT INTO foundation_artifacts VALUES (1)"))
    assert client.get("/api/admin/data-store/status", headers=headers).json()["phase"] == "rebuilding"
    blocked = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "DATA_STORE_REBUILDING"
    with store.catalog.engine.begin() as connection:
        connection.execute(text("DROP TABLE foundation_artifacts"))
        connection.execute(text("INSERT INTO data_store_legacy_maintenance(singleton,phase) VALUES (1,'reset_done')"))
    assert client.post("/api/admin/data-store/query", headers=headers, json=body).status_code == 503
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_legacy_maintenance SET phase='ready' WHERE singleton=1"))
    assert client.get("/api/admin/data-store/status", headers=headers).json()["phase"] == "ready"


def reset_complete(store):
    """A disposable fixture models the real completed reset receipt only."""
    with store.catalog.engine.begin() as connection:
        connection.execute(text("""INSERT INTO data_store_legacy_maintenance
            (singleton,phase,plan_hash,completed_json,files_started)
            VALUES (1,'reset_done',:hash,'["hooks","derived","originals","functions","files"]',true)"""),
            {'hash': 'a' * 64})


def test_verified_independent_domains_read_while_other_domain_and_finish_remain_blocked(api):
    from app.data_store.availability import require_ready
    from app.data_store.errors import DataStoreError
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    reset_complete(store)
    for eid in ('E69', 'E70'):
        entry = BY_ID[eid]
        result = run_entry(store, entry, NativeSources(store.catalog.engine))
        assert result['complete'] and result['qualified']
        detail = client.get(f'/api/admin/data-store/datasets/{entry.spec.name}', headers=headers).json()
        assert detail['status'] == 'available'
        response = client.post('/api/admin/data-store/query', headers=headers,
            json=query_body(entry, detail['preview_key'], frequency=store_router._frequency(entry)))
        assert response.status_code == 200, response.text
        assert response.json()['rows']
        assert response.json()['generation'] == detail['generation']

    missing = BY_ID['E44']
    body = query_body(missing, {'representation': 'r', 'subject': 's', 'object_key': 'k'}, frequency='object')
    blocked = client.post('/api/admin/data-store/query', headers=headers, json=body)
    assert blocked.status_code == 409
    assert blocked.json()['detail']['reason'] == 'FULL_RANGE_UNPROVEN'
    assert client.post('/api/admin/data-store/query', headers=headers,
                       json={**body, 'allow_partial': True}).status_code == 409
    status = client.get('/api/admin/data-store/status', headers=headers).json()
    assert status['phase'] == 'reset_done' and status['system_safe']
    assert status['r01_complete'] is False
    with Session(store.catalog.engine) as session, pytest.raises(DataStoreError):
        require_ready(session)

    # A real global safety fault blocks both previously readable capabilities.
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_legacy_maintenance SET phase='resetting'"))
    for eid in ('E69', 'E70'):
        entry = BY_ID[eid]
        response = client.post('/api/admin/data-store/query', headers=headers,
            json=query_body(entry, {'representation': 'r', 'subject': 's', 'object_key': 'k'},
                            frequency=store_router._frequency(entry)))
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'DATA_STORE_REBUILDING'


def test_zero_files_require_a_completed_empty_source_scan(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    reset_complete(store)
    entry = BY_ID['E05']
    store.register(entry.spec)
    body = query_body(entry, {'representation': 'r', 'subject': 's', 'object_key': 'k'}, frequency='object')
    assert client.post('/api/admin/data-store/query', headers=headers, json=body).status_code == 409
    with store.catalog.engine.begin() as connection:
        connection.execute(text('DELETE FROM tonghuashun_observations WHERE dataset=:d'), {'d': entry.native})
    result = run_entry(store, entry, NativeSources(store.catalog.engine))
    assert result['complete'] and result['qualified'] and result['source_rows'] == 0
    response = client.post('/api/admin/data-store/query', headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'empty' and response.json()['rows'] == []


def test_current_admission_fences_concurrent_destructive_phase_change(api):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.exc import OperationalError
    from app.data_store.availability import require_operable
    _, store = api
    reset_complete(store)
    def change_phase():
        with store.catalog.engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout='100ms'"))
            connection.execute(text("UPDATE data_store_legacy_maintenance SET phase='resetting'"))
    with Session(store.catalog.engine) as session, ThreadPoolExecutor(max_workers=1) as pool:
        assert require_operable(session).operable
        with pytest.raises(OperationalError):
            pool.submit(change_phase).result(timeout=3)


@pytest.mark.parametrize('state', ['paused', 'completed', 'archived'])
def test_inert_retired_task_history_preserved_while_real_writer_admission_stays_blocked(api, state):
    from uuid import uuid4
    client, store = api
    headers = {'Authorization': f'Bearer {TOKEN}'}
    reset_complete(store)
    entry = BY_ID['E69']
    assert run_entry(store, entry, NativeSources(store.catalog.engine))['qualified']
    task_id, run_id = uuid4(), uuid4()
    with store.catalog.engine.begin() as connection:
        connection.execute(text("INSERT INTO scheduled_tasks VALUES (:id,'foundation.formalize_local_updates',:state)"),
            {'id': task_id, 'state': state})
    detail = client.get('/api/admin/data-store/datasets/' + entry.spec.name, headers=headers).json()
    body = query_body(entry, detail['preview_key'], frequency=store_router._frequency(entry))
    def query():
        return client.post('/api/admin/data-store/query', headers=headers, json=body)
    # Actual current rows remain usable without deleting the inert legacy task
    # or changing the reset_done receipt into full R01 readiness.
    response = query()
    assert response.status_code == 200 and response.json()['rows']
    with store.catalog.engine.begin() as connection:
        assert connection.execute(text('SELECT state FROM scheduled_tasks WHERE id=:id'), {'id': task_id}).scalar_one() == state
        connection.execute(text("UPDATE scheduled_tasks SET state='active' WHERE id=:id"), {'id': task_id})
    assert query().status_code == 503
    with store.catalog.engine.begin() as connection:
        connection.execute(text('UPDATE scheduled_tasks SET state=:state WHERE id=:id'), {'id': task_id, 'state': state})
        connection.execute(text("INSERT INTO task_runs VALUES (:id,'foundation.formalize_local_updates','queued')"), {'id': run_id})
    assert query().status_code == 503
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE task_runs SET status='running' WHERE id=:id"), {'id': run_id})
    assert query().status_code == 503
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE task_runs SET status='interrupted' WHERE id=:id"), {'id': run_id})
    assert query().status_code == 200
    status = client.get('/api/admin/data-store/status', headers=headers).json()
    assert status['phase'] == 'reset_done' and status['system_safe'] and not status['r01_complete']


def test_empty_current_scope_reports_schema_and_does_not_hide_issues(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    store.register(entry.spec)
    body = query_body(entry, {"representation": "r", "subject": "s", "object_key": "k"})

    # Registration is not a source scan. An unbuilt scope must not impersonate
    # an authoritative empty local domain, even on a fresh installation.
    unbuilt = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert unbuilt.status_code == 409
    assert unbuilt.json()['detail']['reason'] == 'FULL_RANGE_UNPROVEN'
    from tests.test_data_store_local_pipeline import Inputs
    run_entry(store, entry, Inputs())

    empty = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert empty.status_code == 200
    assert empty.json()["status"] == "empty"
    assert empty.json()["schema_id"] == entry.spec.schema_id
    assert empty.json()["business_date_coverage_verified"] is False

    with store.catalog.engine.begin() as connection:
        connection.execute(text("""INSERT INTO data_store_issues
            (dataset, issue_key, scope_key, reason, evidence_token, target_json, resolution_json)
            VALUES (:dataset, 'test-issue', 'scope', 'SOURCE_CONFIRMATION_UNPROVEN',
                    'test-evidence', '{}', '{}')"""), {"dataset": entry.spec.name})
    blocked = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "DATA_RESTRICTED"
    partial = client.post("/api/admin/data-store/query", headers=headers,
                          json={**body, "allow_partial": True})
    assert partial.status_code == 409


def test_current_preview_and_generation_change_rejects_continuation(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    outcome = run_entry(store, entry, NativeSources(store.catalog.engine))
    assert outcome["complete"] and outcome["qualified"]
    descriptor = client.get(f"/api/admin/data-store/datasets/{entry.spec.name}", headers=headers)
    assert descriptor.status_code == 200
    detail = descriptor.json()
    assert detail["status"] == "available"
    assert not {"value_hash", "basis_valid"} & {field["column"] for field in detail["fields"]}
    assert "basis_state" in {field["column"] for field in detail["fields"]}
    assert detail["row_count"] >= 2
    assert detail["preview_key"] is not None
    body = query_body(entry, detail["preview_key"])
    first = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert len(first.json()["rows"]) == 1
    assert first.json()["next_cursor"]
    assert client.post("/api/admin/data-store/query?release_id=old", headers=headers, json=body).status_code == 422
    assert client.post("/api/admin/data-store/query", headers=headers,
                       json={**body, "release_id": "old"}).status_code == 422
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_datasets SET generation=generation+1 "
                                "WHERE name=:dataset"), {"dataset": entry.spec.name})
    continuation = client.post("/api/admin/data-store/query", headers=headers,
                               json={**body, "cursor": first.json()["next_cursor"]})
    assert continuation.status_code == 409
    assert continuation.json()["detail"]["code"] == "DATA_CHANGED"
