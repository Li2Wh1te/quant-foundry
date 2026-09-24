"""Proof identity changes only with its declared semantics, not runtime chores."""
from pathlib import Path
import pytest
from app.data_foundation.record_validation import validation_hash, SEMANTIC_FILES
from app.data_foundation.execution import installed_code_hash
from app.data_foundation.record_work import plan_validation_hash


def test_operational_change_keeps_block_proof_but_changes_full_execution_identity(monkeypatch):
    block, runtime, plan = validation_hash(), installed_code_hash(), plan_validation_hash()
    original = Path.read_bytes
    def changed(path):
        value = original(path)
        return value + b'\n# unrelated reconciliation-only repair\n' if path.name == 'reconciliation.py' else value
    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert validation_hash() == block
    assert installed_code_hash() != runtime and plan_validation_hash() != plan


@pytest.mark.parametrize('name', [*SEMANTIC_FILES, 'uv.lock'])
def test_each_declared_semantic_dependency_invalidates_receipts(monkeypatch, name):
    previous = validation_hash()
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) + (b'\n# changed\n' if path.name == name else b''))
    assert validation_hash() != previous
