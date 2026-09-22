"""Index reverse sealing guards without weakening immutable membership checks."""
from alembic import op
from sqlalchemy import text

revision = '20261001_01'
down_revision = '20260930_01'
branch_labels = None
depends_on = None

INDEXES = (
    ('foundation_release_block_refs', 'block_id'),
    ('foundation_candidate_entries', 'candidate_id'),
)


def ensure_index(connection, table, column):
    """Resume a partially completed concurrent build using catalog evidence.

    Alembic cannot atomically commit concurrent index creation and its revision
    row. Reuse an exact valid index after interruption, rebuild an exact invalid
    one, and refuse to replace an unrelated object with the same name.
    """
    name = f'ix_{table}_{column}'
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
                or existing['column_name'] != column or existing['is_unique']
                or existing['attributes'] != 1 or existing['predicate'] is not None):
            raise RuntimeError(f'Unexpected index definition: {name}')
        if existing['valid'] and existing['ready']:
            return
        connection.execute(text(f'DROP INDEX CONCURRENTLY {name}'))
    # Identifiers come from the static migration tuple, never operator data.
    connection.execute(text(f'CREATE INDEX CONCURRENTLY {name} ON {table} ({column})'))


def upgrade():
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = connection.scalar(text('SHOW lock_timeout'))
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            for table, column in INDEXES:
                ensure_index(connection, table, column)
        finally:
            connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {'value': previous})


def downgrade():
    # These indexes contain no independent evidence. Dropping them leaves all
    # original rows, foreign keys and sealing triggers intact.
    with op.get_context().autocommit_block():
        for table, column in reversed(INDEXES):
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS ix_{table}_{column}')
