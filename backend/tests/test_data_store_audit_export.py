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


def test_four_confirmed_empty_domains_count_actual_api_without_fake_rows(api, tmp_path):
    client, current = api
    identities = ('E05', 'E06', 'E61', 'E64')
    entries = tuple(BY_ID[identity] for identity in identities)
    with current.catalog.engine.begin() as connection:
        connection.execute(text('DELETE FROM tonghuashun_observations WHERE dataset=ANY(:datasets)'),
                           {'datasets': [entry.native for entry in entries if entry.source == 'tonghuashun']})
        connection.execute(text('DELETE FROM corporate_action_facts'))
        connection.execute(text('DELETE FROM trading_status_facts'))
    for entry in entries:
        result = run_entry(current, entry, NativeSources(current.catalog.engine))
        assert result['complete'] and result['qualified'] and result['state'] == 'empty'
    before = {entry.spec.name: current.catalog.dataset(entry.spec.name) for entry in entries}
    requests = []
    request = _request(client)

    def record(method, path, payload=None):
        requests.append((method, path, payload))
        return request(method, path, payload)

    output = tmp_path / 'four-empty-api'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, api_entries=identities, api_request=record,
                          limits=AuditLimits(api_samples=4))

    assert result['complete'] and not result['global_acceptance'], _checks(output)
    assert result['checked_files'] == result['checked_rows'] == 0
    assert result['api_attempted_entries'] == result['api_passed_entries'] == list(identities)
    assert len(requests) == 4 and all(method == 'GET' and payload is None for method, _, payload in requests)
    assert {entry.spec.name: current.catalog.dataset(entry.spec.name) for entry in entries} == before
    assert all(check['actual']['status'] == 'empty' for check in _checks(output)
               if check['check'] == 'api_sample')


@pytest.mark.parametrize('restriction', ['maintenance', 'quality'])
def test_empty_api_sample_cannot_hide_a_late_real_restriction(api, tmp_path, restriction):
    client, current = api
    entry = BY_ID['E05']
    with current.catalog.engine.begin() as connection:
        connection.execute(text('DELETE FROM tonghuashun_observations WHERE dataset=:dataset'),
                           {'dataset': entry.native})
    assert run_entry(current, entry, NativeSources(current.catalog.engine))['qualified']
    request = _request(client)

    def restrict_before_descriptor(method, path, payload=None):
        with current.catalog.engine.begin() as connection:
            if restriction == 'maintenance':
                connection.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'rebuilding')"))
            else:
                connection.execute(text("""INSERT INTO data_store_issues
                    (dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json)
                    VALUES (:dataset,'late','scope','SOURCE_CONFIRMATION_UNPROVEN','test','{}','{}')"""),
                    {'dataset': entry.spec.name})
        return request(method, path, payload)

    output = tmp_path / 'restricted-empty-api'
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_entries=(entry.id,), api_request=restrict_before_descriptor)

    assert not result['complete'] and result['api_passed_entries'] == []
    expected = 'DATA_STORE_REBUILDING' if restriction == 'maintenance' else 'API_EMPTY_DOMAIN_UNPROVEN'
    assert any(check.get('code') == expected for check in _checks(output)), _checks(output)


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


def test_explicit_eight_api_members_all_traverse_real_routes(api, tmp_path):
    client, current = api
    identities = tuple(f"E{number:02}" for number in range(7, 15))
    entries = tuple(BY_ID[identity] for identity in identities)
    native = NativeSources(current.catalog.engine)
    for entry in entries:
        assert run_entry(current, entry, native)["complete"]
    with current.catalog.engine.connect() as connection:
        files, rows, size = connection.execute(text(
            "SELECT count(*),sum(row_count),sum(byte_count) FROM data_store_files WHERE dataset=ANY(:names)"),
            {"names": [entry.spec.name for entry in entries]}).one()
    output = tmp_path / "eight-api"

    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, api_entries=identities, api_request=_request(client),
                          limits=AuditLimits(files=int(files), rows=int(rows), bytes=int(size)))

    assert result["complete"], _checks(output)
    assert result["checked_files"] == files and result["checked_rows"] == rows
    assert result["api_required_count"] == 8
    assert result["api_required_entries"] == list(identities)
    assert result["api_attempted_entries"] == result["api_passed_entries"] == list(identities)
    assert {check["scope"] for check in _checks(output)
            if check["check"] == "api_sample" and check["status"] == "pass"} == set(identities)


def test_caps_cannot_turn_partial_eight_member_audit_into_complete(api, tmp_path):
    client, current = api
    identities = tuple(f"E{number:02}" for number in range(7, 15))
    entries = tuple(BY_ID[identity] for identity in identities)
    for entry in entries:
        run_entry(current, entry, NativeSources(current.catalog.engine))
    output = tmp_path / "file-cap"

    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, api_entries=identities, limits=AuditLimits(files=1),
                          api_request=_request(client))

    assert not result["complete"] and result["checked_files"] <= 1
    assert result["api_required_count"] == 8 and len(result["api_passed_entries"]) < 8
    # The same finite file limit also caps directory discovery. With sixteen
    # scratch slots that earlier cap may stop this deliberately tiny run first;
    # either honest budget stop must preserve all eight required API members.
    assert any(check.get("code") in {"AUDIT_BUDGET_EXCEEDED", "DIRECTORY_BUDGET_EXCEEDED"}
               for check in _checks(output))
    assert any(check.get("code") == "API_SAMPLE_SET_INCOMPLETE" for check in _checks(output))


def test_all_domain_audit_reports_unknown_catalog_dataset(api, tmp_path):
    from app.data_store.adapters.registry import ENTRIES
    from tests.test_data_store_kernel import spec
    _, current = api
    for entry in ENTRIES:
        if entry.business:
            current.register(entry.spec)
    current.register(spec("unaccounted"))
    output = tmp_path / "unknown-catalog-dataset"

    result = export_audit(current.catalog.engine, current.files.root, output)

    assert not result["complete"] and not result["global_acceptance"]
    assert any(check["check"] == "catalog_dataset_membership"
               and check.get("code") == "CATALOG_MISMATCH" and "unaccounted" in check["actual"]
               for check in _checks(output))


@pytest.mark.parametrize("explicit", [False, True])
def test_missing_api_member_cannot_be_replaced_or_reduce_sample_count(api, tmp_path, explicit):
    client, current = api
    entries = tuple(BY_ID[identity] for identity in ("E07", "E08", "E09"))
    native = NativeSources(current.catalog.engine)
    with current.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM tonghuashun_observations WHERE dataset=:dataset"),
                           {"dataset": entries[1].native})
    for entry in entries:
        if entry.id == 'E08':
            # Registered zero-row metadata without a completed source scan is
            # a missing acceptance member, not a confirmed empty business domain.
            current.register(entry.spec)
            continue
        assert run_entry(current, entry, native)["complete"]
    output = tmp_path / "missing-api-member"
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, limits=AuditLimits(api_samples=2),
                          api_entries=("E07", "E08") if explicit else None,
                          api_request=_request(client))

    if explicit:
        assert not result["complete"]
        assert result["api_required_entries"] == ["E07", "E08"]
        assert result["api_passed_entries"] == ["E07"]
        assert any(check["scope"] == "E08" and check.get("code") == "API_SAMPLE_MISSING"
                   for check in _checks(output))
    else:
        # The legacy first-available mode may sample another selected domain,
        # but two API passes cannot complete the unscanned selected domain.
        assert not result["complete"], _checks(output)
        assert result["api_passed_entries"] == ["E07", "E09"]
        assert any(check['scope'] == 'E08' and check.get('code') == 'INPUT_STATUS_INCOMPLETE'
                   for check in _checks(output))


def test_default_api_count_cannot_pass_with_fewer_available_members(api, tmp_path):
    client, current = api
    entries = tuple(BY_ID[identity] for identity in ("E07", "E08"))
    with current.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM tonghuashun_observations WHERE dataset=:dataset"),
                           {"dataset": entries[1].native})
    for entry in entries:
        if entry.id == 'E08':
            current.register(entry.spec)
            continue
        run_entry(current, entry, NativeSources(current.catalog.engine))
    output = tmp_path / "short-api-count"

    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, api_request=_request(client))

    assert not result["complete"]
    assert result["api_required_count"] == 2 and result["api_passed_entries"] == ["E07"]
    assert any(check.get("code") == "API_SAMPLE_COUNT_INCOMPLETE" for check in _checks(output))


@pytest.mark.parametrize("identities", [("E41", "E41"), ("E01",), ("E99",), ("E07",), ()])
def test_invalid_required_api_selection_never_creates_export(api, tmp_path, identities):
    _, current = api
    output = tmp_path / "invalid-selection"
    with pytest.raises(ValueError):
        export_audit(current.catalog.engine, current.files.root, output,
                     entries=(BY_ID["E41"],), api_entries=identities)
    assert not output.exists()


def test_explicit_api_selection_is_limited_by_configured_budget(api, tmp_path):
    _, current = api
    with pytest.raises(ValueError):
        export_audit(current.catalog.engine, current.files.root, tmp_path / "too-many-api",
                     entries=(BY_ID["E07"], BY_ID["E08"]), api_entries=("E07", "E08"),
                     limits=AuditLimits(api_samples=1))


@pytest.mark.parametrize("phase", ["reset_done", "rebuilding"])
def test_explicit_api_members_preserve_each_maintenance_failure(api, tmp_path, phase):
    client, current = api
    identities = ("E07", "E08")
    entries = tuple(BY_ID[identity] for identity in identities)
    for entry in entries:
        run_entry(current, entry, NativeSources(current.catalog.engine))
    with current.catalog.engine.begin() as connection:
        connection.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,:phase)"),
                           {"phase": phase})
    output = tmp_path / "explicit-maintenance"

    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=entries, api_entries=identities, api_request=_request(client))

    assert not result["complete"] and result["api_passed_entries"] == []
    assert result["api_attempted_entries"] == list(identities)
    assert {check["scope"] for check in _checks(output)
            if check.get("code") == "DATA_STORE_REBUILDING"} == set(identities)
    with current.catalog.engine.connect() as connection:
        assert connection.execute(text(
            "SELECT phase FROM data_store_legacy_maintenance WHERE singleton=1")).scalar_one() == phase


@pytest.mark.parametrize("mutation", ["quality", "file_reference", "maintenance", "descriptor"])
def test_final_fence_detects_change_without_generation_or_status_write(api, tmp_path, mutation):
    client, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    before_generation = current.catalog.dataset(entry.spec.name)["generation"]
    with current.catalog.engine.connect() as connection:
        before_status = connection.execute(text(
            "SELECT summary_json FROM data_store_entry_status WHERE entry_id=:id"),
            {"id": entry.id}).scalar_one()
    request = _request(client)

    def change_after_last_api(method, path, payload=None):
        response = request(method, path, payload)
        if path == "/query?release_id=retired":
            # The only mutation is in this disposable schema, after all real
            # API assertions have observed the original current generation.
            with current.catalog.engine.begin() as connection:
                if mutation == "quality":
                    connection.execute(text("""INSERT INTO data_store_issues
                        (dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json)
                        VALUES (:dataset,'late','scope','SOURCE_CONFIRMATION_UNPROVEN','test','{}','{}')"""),
                        {"dataset": entry.spec.name})
                elif mutation == "file_reference":
                    connection.execute(text("UPDATE data_store_files SET content_hash=:hash WHERE dataset=:dataset"),
                                       {"dataset": entry.spec.name, "hash": "0" * 64})
                elif mutation == "maintenance":
                    connection.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'reset_done')"))
                else:
                    connection.execute(text("UPDATE data_store_datasets SET row_count=row_count+1 WHERE name=:dataset"),
                                       {"dataset": entry.spec.name})
        return response

    output = tmp_path / "late-fence"
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=change_after_last_api)

    assert not result["complete"] and result["api_passed_entries"] == [entry.id]
    assert current.catalog.dataset(entry.spec.name)["generation"] == before_generation
    with current.catalog.engine.connect() as connection:
        assert connection.execute(text(
            "SELECT summary_json FROM data_store_entry_status WHERE entry_id=:id"),
            {"id": entry.id}).scalar_one() == before_status
    expected_check = {"quality": "final_coverage_stability", "file_reference": "final_file_catalog_stability",
                      "maintenance": "final_maintenance_stability", "descriptor": "final_generation_stability"}[mutation]
    assert any(check["check"] == expected_check and check["status"] == "fail"
               and check.get("code") == "DATA_CHANGED" for check in _checks(output))


def test_late_native_commit_invalidates_coverage_without_current_generation_change(api, tmp_path):
    from app.data_store.verify_coverage import verify_existing
    client, current = api
    entry = BY_ID["E69"]
    native = NativeSources(current.catalog.engine)
    assert run_entry(current, entry, native)["complete"]
    assert verify_existing(current, entry, native)["complete"]
    before = current.catalog.dataset(entry.spec.name)["generation"]
    request = _request(client)

    def commit_after_sample(method, path, payload=None):
        response = request(method, path, payload)
        if path == "/query?release_id=retired":
            with current.catalog.engine.begin() as connection:
                connection.execute(text("UPDATE etf_adjustment_factors SET adj_factor=adj_factor+1 WHERE source='tushare'"))
        return response

    output = tmp_path / "late-native"
    result = export_audit(current.catalog.engine, current.files.root, output,
                          entries=(entry,), api_request=commit_after_sample)

    assert not result["complete"] and result["api_passed_entries"] == [entry.id]
    assert current.catalog.dataset(entry.spec.name)["generation"] == before
    assert any(check["check"] == "final_generation_stability" and check["status"] == "pass"
               for check in _checks(output))
    assert any(check["check"] == "final_coverage_stability" and check["status"] == "fail"
               for check in _checks(output))


def test_audit_cli_preserves_explicit_api_members(api, tmp_path, monkeypatch, capsys):
    client, current = api
    entry = BY_ID["E41"]
    run_entry(current, entry, NativeSources(current.catalog.engine))
    monkeypatch.setattr("app.db.session.get_engine", lambda: current.catalog.engine)
    monkeypatch.setenv("QF_AUDIT_API_TOKEN", "synthetic-audit-token")
    monkeypatch.setattr("app.data_store.audit_export._api_client", lambda *_: _request(client))
    from app.data_store.__main__ import main

    assert main(["audit-export", "--root", str(current.files.root), "--entry", entry.id,
                 "--output", str(tmp_path / "cli-explicit"), "--api-base-url", "http://127.0.0.1:1",
                 "--audit-api-entry", entry.id]) == 0
    # The in-process HTTP transport emits request logs before the CLI result;
    # the CLI's final line remains the actual machine-readable result.
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["api_passed_entries"] == [entry.id]
