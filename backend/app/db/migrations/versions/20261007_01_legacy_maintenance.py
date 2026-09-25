"""Add non-destructive LF-D03 maintenance receipts.

Revision ID: 20261007_01
Revises: 20261006_01

The revision creates only new small control tables. Neither an upgrade nor an
application import reads or drops legacy data.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261007_01'
down_revision = '20261006_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE data_store_legacy_maintenance (
        singleton integer PRIMARY KEY CHECK (singleton=1),
        phase varchar(24) NOT NULL CHECK (phase IN ('rebuilding','resetting','reset_done','ready')),
        plan_hash varchar(64),
        completed_json jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(completed_json)='array'),
        files_started boolean NOT NULL DEFAULT false,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    op.execute("""CREATE TABLE data_store_legacy_task_state (
        task_id uuid PRIMARY KEY,
        task_type varchar(64) NOT NULL,
        previous_state varchar(16) NOT NULL,
        paused_version integer NOT NULL,
        captured_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")
    op.execute("""CREATE TABLE data_store_legacy_restrictions (
        origin_key varchar(128) PRIMARY KEY,
        dataset varchar(128),
        scope_key varchar(128),
        target_key varchar(128),
        fields_json text NOT NULL CHECK (octet_length(fields_json)<=65536),
        reason text NOT NULL CHECK (octet_length(reason)<=65536),
        payload_json text NOT NULL CHECK (octet_length(payload_json)<=65536),
        located boolean NOT NULL,
        captured_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")


def downgrade():
    connection = op.get_bind()
    for table in ('data_store_legacy_restrictions','data_store_legacy_task_state',
                  'data_store_legacy_maintenance'):
        if connection.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table})')).scalar_one():
            raise RuntimeError('Legacy maintenance evidence requires reviewed cleanup')
        op.drop_table(table)
