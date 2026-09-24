"""Online indexes have exact predicates and resumable, collision-safe creation."""
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import pytest
from sqlalchemy import text
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine


def test_online_indexes_are_valid_and_reexecution_preserves_unrelated_definitions(pipeline_engine):
    path = Path(__file__).resolve().parents[1]/'app/db/migrations/versions/20261004_02_foundation_settlement_indexes.py'
    spec = spec_from_file_location('settlement_index_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pipeline_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        for definition in migration.INDEXES:
            migration.ensure_index(conn, *definition)
            assert conn.scalar(text('SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid=to_regclass(:name)'),
                               {'name': definition[0]})
        name = 'qf_test_unexpected_index'
        conn.execute(text(f'CREATE INDEX {name} ON foundation_source_refs (decoder_id)'))
        try:
            with pytest.raises(RuntimeError, match='Unexpected index definition'):
                migration.ensure_index(conn, name, 'foundation_source_refs', 'observation_id', 'observation_id IS NOT NULL')
            assert conn.scalar(text('SELECT pg_get_indexdef(to_regclass(:name),1,true)'), {'name': name}) == 'decoder_id'
        finally:
            conn.execute(text(f'DROP INDEX {name}'))
