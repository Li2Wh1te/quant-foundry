"""Current LF-D02 entry status, not a formal empty/import/support dataset.

Revision ID: 20261006_01
Revises: 20261005_01
"""
from alembic import op
import sqlalchemy as sa
revision = '20261006_01'
down_revision = '20261005_01'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""CREATE TABLE data_store_entry_status (
        entry_id varchar(16) PRIMARY KEY CHECK (entry_id ~ '^(E[0-9]{2}|B05(-M)?)$'),
        summary_json text NOT NULL CHECK (octet_length(summary_json) <= 65536),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")

def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM data_store_entry_status)')).scalar_one():
        raise RuntimeError('Populated local entry state requires reviewed maintenance')
    op.execute('DROP TABLE data_store_entry_status')
