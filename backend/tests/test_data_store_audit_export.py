"""LF-D05 audit evidence against isolated PostgreSQL, Parquet and real routes."""
from __future__ import annotations

import csv
from functools import partial
import json
import os

import pytest
from sqlalchemy import text

from app.data_store.adapters.registry import BY_ID
from app.data_store.audit_export import AuditLimits, export_audit
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import run_entry
from app.data_store.storage import CurrentStore
from tests.test_data_store_api import TOKEN, api  # noqa: F401 - real ASGI fixture
from tests.test_data_store_domain_samples import ready  # noqa: F401 - seeded native fixture
from tests.test_data_store_kernel import database, limits, store  # noqa: F401 - fixtures


pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1",
                                reason="isolated PostgreSQL required")


def _request(client):
    def call(method, path, payload=None):
        target = "/api/admin/data-store" + path
        headers = {"Authorization": "Bearer " + TOKEN}
        response = (client.get(target, headers=headers) if method == "GET" else
                    client.post(target, headers=headers, json=payload))
        return response.status_code, response.json()
    return call


def _checks(directory):
    return [json.loads(line) for line in (directory / "checks.jsonl").read_text().splitlines()]


def test_current_audit_is_read_only_and_asserts_real_api(api, tmp_path):
    client, current = api
    entry = BY_ID["E41"]
    assert run_entry(current, entry, NativeSources(current.catalog.engine))["complete"]
    before = current.catalog.dataset(entry.spec.name)
    root_id = (current.files.root / ".store-id").read_bytes()
    result = export_audit(current.catalog.engine, current.files.root, tmp_path / "audit",
                          entries=(entry,), api_request=_request(client))

    assert result["complete"], _checks(tmp_path / "audit")
    assert current.catalog.dataset(entry.spec.name) == before
    assert (current.files.root / ".store-id").read_bytes() == root_id
    assert not (current.files.root / "audit").exists()
    checks = _checks(tmp_path / "audit")
    assert any(c["check"] == "full_file_scan" and c["status"] == "pass" for c in checks)
    assert any(c["check"] == "api_sample" and c["status"] == "pass" for c in checks)
    with (tmp_path / "audit" / "source_disposition.csv").open(newline="") as handle:
        dispositions = list(csv.DictReader(handle))
    assert len(dispositions) == 71
    e41 = next(row for row in dispositions if row["entry_id"] == "E41")
    assert int(e41["source_rows_processed"]) >= 1
    assert int(e41["current_key_count"]) >= 1
    assert "postgresql://" not in (tmp_path / "audit" / "runtime.json").read_text()


@pytest.mark.parametrize('phase', ['reset_done', 'rebuilding'])
def test_audit_reports_real_maintenance_gate_without_descriptor_mismatch(api, tmp_path, phase):
    client, current = api
    entry = BY_ID['E41']
    run_entry(current, entry, NativeSources(current.catalog.engine))
    before = current.catalog.dataset(entry.spec.name)
    with current.catalog.engine.begin() as connection:
        connection.execute(text('INSERT INTO data_store_legacy_maintenance VALUES (1,:phase)'),
                           {'phase': phase})

    output = tmp_path / 'maintenance-audit'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=_request(client))

    assert not result['complete']
    checks = _checks(output)
    assert any(check['check'] == 'api_sample' and check['status'] == 'incomplete'
               and check.get('code') == 'DATA_STORE_REBUILDING' for check in checks)
    assert not any(check.get('code') == 'API_DESCRIPTOR_MISMATCH' for check in checks)
    assert current.catalog.dataset(entry.spec.name) == before
    status, page = _request(client)('POST', '/query', {
        'dataset': entry.spec.name, 'frequency': 'object',
        'representation': 'r', 'subject': 's', 'from_key': 'k', 'to_key': 'k',
        'columns': ['representation', 'subject', 'object_key', 'member_key'],
        'page_size': 1,
    })
    assert status == 503 and page['detail']['code'] == 'DATA_STORE_REBUILDING'
    with current.catalog.engine.connect() as connection:
        assert connection.execute(text(
            'SELECT phase FROM data_store_legacy_maintenance WHERE singleton=1')).scalar_one() == phase


def test_audit_reports_maintenance_entered_between_descriptor_and_query(api, tmp_path):
    client, current = api
    entry = BY_ID['E41']
    run_entry(current, entry, NativeSources(current.catalog.engine))
    request = _request(client)

    def enter_before_query(method, path, payload=None):
        if method == 'POST' and path == '/query':
            with current.catalog.engine.begin() as connection:
                connection.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'reset_done')"))
        return request(method, path, payload)

    output = tmp_path / 'late-maintenance-audit'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=enter_before_query)

    assert not result['complete']
    assert any(check['check'] == 'api_sample' and check['status'] == 'incomplete'
               and check.get('code') == 'DATA_STORE_REBUILDING' for check in _checks(output))


@pytest.mark.parametrize('field', ['generation', 'row_count'])
def test_audit_still_rejects_ready_descriptor_disagreement(api, tmp_path, field):
    client, current = api
    entry = BY_ID['E41']
    run_entry(current, entry, NativeSources(current.catalog.engine))
    request = _request(client)

    def disagree(method, path, payload=None):
        status, response = request(method, path, payload)
        if method == 'GET' and status == 200:
            response[field] += 1
        return status, response

    output = tmp_path / 'descriptor-mismatch-audit'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=disagree)

    assert not result['complete']
    assert any(check.get('code') == 'API_DESCRIPTOR_MISMATCH' for check in _checks(output))


def test_nonmaintenance_api_error_is_incomplete_without_forging_readiness(api, tmp_path):
    _, current = api
    entry = BY_ID['E41']
    run_entry(current, entry, NativeSources(current.catalog.engine))
    output = tmp_path / 'unavailable-api-audit'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=lambda *_: (503, {'detail': 'Unavailable'}))

    assert not result['complete']
    assert any(check.get('code') == 'API_DESCRIPTOR_MISMATCH' for check in _checks(output))
    assert not any(check.get('code') == 'DATA_STORE_REBUILDING' for check in _checks(output))


def test_audit_marks_budget_truncation_and_missing_api_incomplete(api, tmp_path):
    _, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    result = export_audit(current.catalog.engine, current.files.root, tmp_path / "limited",
                          entries=(entry,), limits=AuditLimits(bytes=1))
    assert result["complete"] is False
    checks = _checks(tmp_path / "limited")
    assert any(c.get("code") == "AUDIT_BUDGET_EXCEEDED" for c in checks)
    assert any(c.get("code") == "API_NOT_CONFIGURED" for c in checks)


def test_audit_rejects_corrupt_current_file(api, tmp_path):
    client, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    with current.catalog.transaction() as connection:
        path = connection.execute(text(
            "SELECT path FROM data_store_files WHERE dataset=:dataset LIMIT 1"),
            {"dataset": entry.spec.name}).scalar_one()
    with (current.files.root / path).open("ab") as handle:
        handle.write(b"corrupt")

    result = export_audit(current.catalog.engine, current.files.root, tmp_path / "corrupt",
                          entries=(entry,), api_request=_request(client))
    assert result["complete"] is False
    assert any(c["check"] == "full_file_scan" and c["status"] == "fail"
               for c in _checks(tmp_path / "corrupt"))


def test_audit_marks_missing_source_counts_incomplete(api, tmp_path):
    client, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    with current.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_entry_status SET summary_json=:summary "
                                "WHERE entry_id=:entry_id"),
                           {"summary": '{"complete":true}', "entry_id": entry.id})

    result = export_audit(current.catalog.engine, current.files.root, tmp_path / "missing-counts",
                          entries=(entry,), api_request=_request(client))
    assert result["complete"] is False
    assert any(c.get("code") == "INPUT_STATUS_INCOMPLETE"
               for c in _checks(tmp_path / "missing-counts"))


def test_audit_cli_never_needs_write_signing_key(api, tmp_path, monkeypatch, capsys):
    _, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    before = current.catalog.dataset(entry.spec.name)
    monkeypatch.delenv("QF_CURSOR_SIGNING_KEY", raising=False)
    monkeypatch.setattr("app.db.session.get_engine", lambda: current.catalog.engine)
    from app.data_store.__main__ import main

    status = main(["audit-export", "--root", str(current.files.root),
                   "--entry", entry.id, "--output", str(tmp_path / "cli-audit")])
    result = json.loads(capsys.readouterr().out)
    assert status == 2 and not result["complete"]  # No API sample was configured.
    assert current.catalog.dataset(entry.spec.name) == before
    assert any(c.get("code") == "API_NOT_CONFIGURED"
               for c in _checks(tmp_path / "cli-audit"))


def test_cleanup_cli_uses_maintenance_path_without_pipeline_mode(api, tmp_path,
                                                                  monkeypatch, capsys):
    _, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    before = current.catalog.dataset(entry.spec.name)["row_count"]
    monkeypatch.setenv("QF_CURSOR_SIGNING_KEY", "isolated-cleanup-signing-key-at-least-32-bytes")
    monkeypatch.setattr("app.db.session.get_engine", lambda: current.catalog.engine)
    monkeypatch.setattr("app.data_store.storage.CurrentStore",
                        partial(CurrentStore, limits=current.limits))
    from app.data_store.__main__ import main

    status = main(["cleanup", "--root", str(current.files.root), "--entry", entry.id,
                   "--output", str(tmp_path / "cleanup.json")])
    result = json.loads(capsys.readouterr().out)
    assert status == 0 and result["complete"], result
    assert current.catalog.dataset(entry.spec.name)["row_count"] == before
