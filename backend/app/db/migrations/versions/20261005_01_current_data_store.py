"""Add the LF-D01 current-only catalog; never reset legacy or shared data.

Revision ID: 20261005_01
Revises: 20261004_02
"""
from alembic import op
import sqlalchemy as sa

revision = '20261005_01'
down_revision = '20261004_02'
branch_labels = None
depends_on = None

# This DDL is frozen here. Historical upgrades do not import the live store.
DDL = """
CREATE TABLE data_store_runtime (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    root_token uuid NOT NULL, policy_json text NOT NULL CHECK (octet_length(policy_json) <= 65536)
);
CREATE TABLE data_store_datasets (
    name varchar(128) PRIMARY KEY,
    descriptor_json text NOT NULL CHECK (octet_length(descriptor_json) <= 65536),
    schema_id varchar(64) NOT NULL, rule varchar(128) NOT NULL,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    row_count bigint NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    byte_count bigint NOT NULL DEFAULT 0 CHECK (byte_count >= 0),
    last_commit uuid, last_result text NOT NULL DEFAULT '{}' CHECK (octet_length(last_result) <= 65536),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE data_store_files (
    id uuid PRIMARY KEY,
    dataset varchar(128) NOT NULL REFERENCES data_store_datasets(name) ON DELETE RESTRICT,
    partition_key varchar(128) NOT NULL,
    key_min bytea NOT NULL CHECK (octet_length(key_min) BETWEEN 1 AND 2048),
    key_max bytea NOT NULL CHECK (octet_length(key_max) BETWEEN 1 AND 2048 AND key_max >= key_min),
    path varchar(256) NOT NULL UNIQUE,
    content_hash varchar(64) NOT NULL,
    row_count bigint NOT NULL CHECK (row_count > 0), byte_count bigint NOT NULL CHECK (byte_count > 0),
    schema_id varchar(64) NOT NULL, rule varchar(128) NOT NULL,
    contract_json text NOT NULL CHECK (octet_length(contract_json) <= 65536),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_data_store_files_range ON data_store_files(dataset, partition_key, key_min);
CREATE TABLE data_store_scopes (
    dataset varchar(128) NOT NULL REFERENCES data_store_datasets(name) ON DELETE RESTRICT,
    scope_key varchar(128) NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    partition_key varchar(128) NOT NULL, key_min bytea NOT NULL, key_end bytea NOT NULL,
    input_token varchar(64) NOT NULL, context_token varchar(64) NOT NULL,
    basis_hash varchar(64) NOT NULL, schema_id varchar(64) NOT NULL, rule varchar(128) NOT NULL,
    confirmation_json text NOT NULL CHECK (octet_length(confirmation_json) <= 65536),
    checkpoint_json text NOT NULL CHECK (octet_length(checkpoint_json) <= 65536),
    qualified boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (dataset, scope_key)
);
CREATE TABLE data_store_issues (
    dataset varchar(128) NOT NULL REFERENCES data_store_datasets(name) ON DELETE RESTRICT,
    issue_key varchar(128) NOT NULL, scope_key varchar(128) NOT NULL,
    reason varchar(64) NOT NULL, evidence_token varchar(64) NOT NULL,
    target_json text NOT NULL CHECK (octet_length(target_json) <= 65536),
    resolution_json text NOT NULL CHECK (octet_length(resolution_json) <= 65536),
    attempts bigint NOT NULL DEFAULT 1 CHECK (attempts > 0),
    first_seen timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (dataset, issue_key)
);
CREATE INDEX ix_data_store_issues_scope ON data_store_issues(dataset, scope_key);
CREATE TABLE data_store_garbage (
    path varchar(256) PRIMARY KEY,
    dataset varchar(128) NOT NULL REFERENCES data_store_datasets(name) ON DELETE RESTRICT,
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    not_before timestamptz NOT NULL DEFAULT clock_timestamp(),
    retry_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    reason varchar(16) NOT NULL CHECK (reason IN ('prepared', 'retired')),
    error_code varchar(64)
);
CREATE INDEX ix_data_store_garbage_retry ON data_store_garbage(dataset, retry_after, path);
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    # Only a completely unused installation can roll back additively-created
    # empty structures. Any current state requires the reviewed D03 maintenance
    # path. No existing legacy/shared table is touched by this revision.
    tables = ('data_store_garbage', 'data_store_issues', 'data_store_scopes',
              'data_store_files', 'data_store_datasets', 'data_store_runtime')
    connection = op.get_bind()
    for table in tables:
        if connection.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table})')).scalar_one():
            raise RuntimeError('current store contains persisted evidence; reviewed maintenance is required')
    for table in tables:
        op.drop_table(table)
