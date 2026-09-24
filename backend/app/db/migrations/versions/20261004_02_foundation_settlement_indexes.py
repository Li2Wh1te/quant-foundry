"""Bound source/manifest lookups and negative quality checks with online indexes."""
from alembic import op
from sqlalchemy import text
import re

revision = '20261004_02'
down_revision = '20261004_01'
branch_labels = None
depends_on = None

INDEXES = (
    ('ix_foundation_source_refs_observation_lookup', 'foundation_source_refs', 'observation_id', 'observation_id IS NOT NULL'),
    ('ix_foundation_work_source_lookup', 'foundation_work', 'source_ref_id', 'source_ref_id IS NOT NULL'),
    ('ix_foundation_work_manifest_lookup', 'foundation_work', 'candidate_manifest_id', 'candidate_manifest_id IS NOT NULL'),
    ('ix_foundation_candidates_not_ready', 'foundation_candidates', 'work_id', "readiness <> 'ready'"),
)


def normalized_predicate(value):
    # pg_get_expr inserts casts/parentheses around varchar comparisons.
    return re.sub(r'::text|::character varying|[()\s]', '', value or '').lower()


def ensure_index(connection, name, table, column, predicate):
    existing = connection.execute(text('''
        SELECT i.indisvalid AS valid, i.indisready AS ready, t.relname AS table_name,
               am.amname AS method, i.indisunique AS is_unique, i.indnatts AS attributes,
               pg_get_expr(i.indpred, i.indrelid) AS predicate,
               pg_get_indexdef(i.indexrelid, 1, true) AS column_name
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_class t ON t.oid=i.indrelid
        JOIN pg_am am ON am.oid=c.relam
        WHERE n.nspname=current_schema() AND c.relname=:name
    '''), {'name': name}).mappings().first()
    if existing:
        if (existing['table_name'] != table or existing['method'] != 'btree'
                or existing['column_name'] != column or existing['is_unique'] or existing['attributes'] != 1
                or normalized_predicate(existing['predicate']) != normalized_predicate(predicate)):
            raise RuntimeError(f'Unexpected index definition: {name}')
        if existing['valid'] and existing['ready']:
            return
        connection.execute(text(f'DROP INDEX CONCURRENTLY {name}'))
    connection.execute(text(f'CREATE INDEX CONCURRENTLY {name} ON {table} ({column}) WHERE {predicate}'))


def upgrade():
    # This revision is independently restartable after an interrupted build.
    # Never hold a normal migration transaction while building large indexes.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = connection.scalar(text('SHOW lock_timeout'))
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            for specification in INDEXES:
                ensure_index(connection, *specification)
        finally:
            connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {'value': previous})


def downgrade():
    with op.get_context().autocommit_block():
        for name, *_ in reversed(INDEXES):
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
