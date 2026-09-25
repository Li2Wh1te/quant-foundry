"""Current-only SQLAlchemy metadata for Alembic inspection, no legacy imports."""
from sqlalchemy import (MetaData, Table, Column as C, String, Text, Integer, BigInteger,
                        Boolean, DateTime, LargeBinary, Uuid, ForeignKey, CheckConstraint,
                        Index, text)

metadata = MetaData()

def small(name, default=None):
    return C(name, Text, nullable=False, server_default=default)

def dataset_fk():
    return C('dataset', String(128), ForeignKey('data_store_datasets.name', ondelete='RESTRICT'), nullable=False)

def instant(name):
    return C(name, DateTime(timezone=True), nullable=False, server_default=text('clock_timestamp()'))

def bounded(*names):
    return [CheckConstraint(f'octet_length({n}) <= 65536') for n in names]

runtime = Table('data_store_runtime', metadata,
    C('singleton', Integer, primary_key=True, autoincrement=False),
    C('root_token', Uuid, nullable=False), small('policy_json'),
    CheckConstraint('singleton = 1'), *bounded('policy_json'))

datasets = Table('data_store_datasets', metadata,
    C('name', String(128), primary_key=True), small('descriptor_json'),
    C('schema_id', String(64), nullable=False), C('rule', String(128), nullable=False),
    C('generation', BigInteger, nullable=False, server_default='0'),
    C('row_count', BigInteger, nullable=False, server_default='0'),
    C('byte_count', BigInteger, nullable=False, server_default='0'),
    C('last_commit', Uuid), small('last_result', '{}'), instant('updated_at'),
    *bounded('descriptor_json','last_result'),
    CheckConstraint('generation >= 0'), CheckConstraint('row_count >= 0'), CheckConstraint('byte_count >= 0'))

files = Table('data_store_files', metadata,
    C('id', Uuid, primary_key=True), dataset_fk(), C('partition_key', String(128), nullable=False),
    C('key_min', LargeBinary, nullable=False), C('key_max', LargeBinary, nullable=False),
    C('path', String(256), nullable=False, unique=True), C('content_hash', String(64), nullable=False),
    C('row_count', BigInteger, nullable=False), C('byte_count', BigInteger, nullable=False),
    C('schema_id', String(64), nullable=False), C('rule', String(128), nullable=False),
    small('contract_json'), instant('created_at'), *bounded('contract_json'),
    CheckConstraint('octet_length(key_min) BETWEEN 1 AND 2048'),
    CheckConstraint('octet_length(key_max) BETWEEN 1 AND 2048 AND key_max >= key_min'),
    CheckConstraint('row_count > 0'), CheckConstraint('byte_count > 0'))
Index('ix_data_store_files_range', files.c.dataset, files.c.partition_key, files.c.key_min)

scopes = Table('data_store_scopes', metadata, dataset_fk(),
    C('scope_key', String(128), nullable=False), C('revision', BigInteger, nullable=False),
    C('partition_key', String(128), nullable=False), C('key_min', LargeBinary, nullable=False),
    C('key_end', LargeBinary, nullable=False), C('input_token', String(64), nullable=False),
    C('context_token', String(64), nullable=False), C('basis_hash', String(64), nullable=False),
    C('schema_id', String(64), nullable=False), C('rule', String(128), nullable=False),
    small('confirmation_json'), small('checkpoint_json'), C('qualified', Boolean, nullable=False),
    instant('updated_at'), CheckConstraint('revision > 0'), *bounded('confirmation_json','checkpoint_json'))

issues = Table('data_store_issues', metadata, dataset_fk(),
    C('issue_key', String(128), nullable=False), C('scope_key', String(128), nullable=False),
    C('reason', String(64), nullable=False), C('evidence_token', String(64), nullable=False),
    small('target_json'), small('resolution_json'),
    C('attempts', BigInteger, nullable=False, server_default='1'), instant('first_seen'), instant('last_seen'),
    CheckConstraint('attempts > 0'), *bounded('target_json','resolution_json'))
Index('ix_data_store_issues_scope', issues.c.dataset, issues.c.scope_key)

garbage = Table('data_store_garbage', metadata,
    C('path', String(256), primary_key=True), dataset_fk(), C('byte_count', BigInteger, nullable=False),
    instant('not_before'), instant('retry_after'),
    C('attempts', Integer, nullable=False, server_default='0'), C('reason', String(16), nullable=False),
    C('error_code', String(64)), CheckConstraint('byte_count >= 0'), CheckConstraint('attempts >= 0'),
    CheckConstraint("reason IN ('prepared', 'retired')"))
Index('ix_data_store_garbage_retry', garbage.c.dataset, garbage.c.retry_after, garbage.c.path)

from sqlalchemy import PrimaryKeyConstraint
scopes.append_constraint(PrimaryKeyConstraint('dataset','scope_key'))
issues.append_constraint(PrimaryKeyConstraint('dataset','issue_key'))
