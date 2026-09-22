"""Reverse guard indexes remain usable and concurrent migrations can resume."""
import importlib
import json
import pytest
from sqlalchemy import text
from tests.test_foundation_publication_postgresql import session, pytestmark

migration = importlib.import_module('app.db.migrations.versions.20261001_01_foundation_reverse_indexes')


@pytest.mark.parametrize('table,column', migration.INDEXES)
def test_guard_reverse_lookup_has_a_valid_usable_index(session, table, column):
    name = f'ix_{table}_{column}'
    row = session.execute(text('SELECT indisvalid,indisready FROM pg_index WHERE indexrelid=to_regclass(:name)'),
        {'name': name}).one()
    assert row.indisvalid and row.indisready
    # Small isolated fixtures may prefer sequential scans. Disable them only
    # for this EXPLAIN to prove the exact guard predicate can use the index.
    session.execute(text('SET LOCAL enable_seqscan=off'))
    plan = session.scalar(text(f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {table} WHERE {column}='00000000-0000-0000-0000-000000000000' LIMIT 1"))
    assert name in json.dumps(plan)


@pytest.mark.parametrize('table,column', migration.INDEXES)
def test_completed_concurrent_index_is_reused_without_rebuilding(session, table, column):
    name = f'ix_{table}_{column}'
    before = session.scalar(text('SELECT to_regclass(:name)::oid'), {'name': name})
    migration.ensure_index(session.connection(), table, column)
    assert session.scalar(text('SELECT to_regclass(:name)::oid'), {'name': name}) == before
