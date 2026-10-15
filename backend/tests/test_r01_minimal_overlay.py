"""Reject stale assembly inputs and preserve private controls during adaptation."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('r01_minimal_overlay', ROOT / 'scripts/r01_minimal_overlay.py')
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


def test_all_base_hashes_are_checked_before_any_output(tmp_path):
    source = tmp_path / 'source'
    first = next(iter(overlay.BASE_HASHES))
    path = source / first
    path.parent.mkdir(parents=True)
    path.write_text('stale source, not an authorized image')
    output = tmp_path / 'output'
    with pytest.raises(ValueError, match='base source hash mismatch'):
        overlay.prepare(source, output)
    assert not output.exists()


def private_service_fixture():
    source = (ROOT / 'backend/app/data_ingestion/tonghuashun/service.py').read_text()
    source = source.replace(
        '        from .directory_proof import DirectoryAcquisition\n'
        '        acquisition = DirectoryAcquisition(client) if directory_replays is not None else Acquisition(client)\n',
        '        acquisition = Acquisition(client, repair_native_keys=repair_native_keys)\n')
    needle = '    directory_replays = _replays\n'
    assert source.count(needle) == 1
    return source.replace(needle, needle +
        '    from .control import default_repair, execution_deadline\n'
        '    repair = default_repair.get()\n'
        '    limit = execution_deadline.get()\n'
        '    repair_native_keys = None\n'
        '    if repair is not None:\n'
        '        repair.validate()\n'
        '        repair.baseline(None)\n'
        '    expected_revision = 7\n')


def test_pair_code_is_added_without_removing_existing_control_and_log_methods():
    before = private_service_fixture()
    merged = (ROOT / 'backend/app/data_ingestion/tonghuashun/service.py').read_text()
    result = overlay.e40_service(before, merged)
    assert 'repair.validate()' in result and 'repair.baseline(None)' in result
    assert 'execution_deadline.get()' in result
    assert 'expected=expected_revision' in result or 'expected_revision + 1' in result
    assert 'publish_pair(pair' in result and 'replay_data, replay_requests = entry.payload()' in result
    assert 'record_outcome(spec.key, succeeded, kind, error_message, result.get("version_id"))' in result
    assert 'current_interface=None' in result and "request_stage='publication_validation'" in result
    assert "event_logger.warning('tonghuashun_directory_pair_rejected'" in result
    assert 'ETF 目录与场外基金目录' in result and '候选 2 个，新增拉取 0 条' in result
    def unchanged(source):
        return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(source).body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name != '_collect'}
    assert unchanged(before) == unchanged(result)


def test_publication_boundary_keeps_both_existing_hard_deadline_and_public_monitor():
    merged = (ROOT / 'backend/app/data_ingestion/tonghuashun/directory_proof.py').read_text()
    result = overlay.e40_proof(merged)
    body = ast.get_source_segment(result, overlay.function(result, 'check_publication'))
    assert body.index('check_execution()') < body.index('monitor.check()')
    assert 'monitor.detail[\'requests\'] <= monitor.max_requests' in body
    ast.parse(result)


def test_recipe_pins_only_five_files_and_writes_no_runtime_configuration():
    assert set(overlay.BASE_HASHES) == set(overlay.MERGED_HASHES)
    assert len(overlay.BASE_HASHES) == 5
    assert all(name.endswith('.py') for name in overlay.BASE_HASHES)
    assert not any('/models/' in name or '/migrations/' in name for name in overlay.BASE_HASHES)
    assert overlay.SOURCE_REVISION == '4454337feaf3ce4e0f1b62c211bf0035f1e7a1fc'
    # This historical recipe stays pinned when current application code moves.
    # Freeze its complete identity, rather than requiring HEAD to remain old.
    pinned = {'base_image': overlay.BASE_IMAGE, 'source_revision': overlay.SOURCE_REVISION,
              'base_hashes': overlay.BASE_HASHES, 'merged_hashes': overlay.MERGED_HASHES}
    assert hashlib.sha256(json.dumps(pinned, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == (
        '8fa259796febf286bfbaf1cb8ad9ce666c3b9b9e29a2beb1ed7e2d98ec62dd8e')


def test_newer_merged_source_is_rejected_before_any_output(tmp_path, monkeypatch):
    name = 'app/data_store/pipeline.py'
    base, approved = b'synthetic base source', b'synthetic approved merged source'
    monkeypatch.setattr(overlay, 'BASE_HASHES', {name: hashlib.sha256(base).hexdigest()})
    monkeypatch.setattr(overlay, 'MERGED_HASHES', {name: hashlib.sha256(approved).hexdigest()})
    source, repository = tmp_path / 'source', tmp_path / 'repository'
    for path, data in ((source / name, base),
                       (repository / 'backend' / name, b'newer unapproved source')):
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    output = tmp_path / 'output'

    with pytest.raises(ValueError, match='merged source hash mismatch'):
        overlay.prepare(source, output, repository=repository)

    assert not output.exists()
