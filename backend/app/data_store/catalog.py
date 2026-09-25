"""PostgreSQL current catalog. No imports of the legacy foundation models.

All SQL touches only current files or a bounded range. One source row represents
an actual source scope, not a success event. `last_commit` is an acknowledgement
probe for the *current* writer, never a release identifier or a history lookup.
DDL is frozen separately in the additive Alembic revision for reproducible installs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import json
from typing import Mapping
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from .errors import DataStoreError
from .limits import StoreLimits
from .schema import DatasetSpec, fingerprint, identifier
from .values import control_json

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


@dataclass(frozen=True)
class SourceUpdate:
    """D02 handoff: tokens bind VERIFIED local input + actual rule/context.

    scope_key is a stable real range/object, NOT a run UUID. expected_revision
    is the state read during source selection (0 means absent). It is a CAS,
    not a source ordering strategy. D02 must additionally supply source_check
    to compare authoritative source order and to maintain sparse confirmations.
    A qualified checkpoint is committed only with the complete atomic unit.
    """
    scope_key: str
    input_token: str
    context_token: str
    expected_revision: int
    confirmation: Mapping = field(default_factory=dict)
    checkpoint: Mapping = field(default_factory=dict)
    qualified: bool = True

    def __post_init__(self):
        identifier(self.scope_key)
        for token in (self.input_token, self.context_token):
            if (type(token) is not str or len(token) != 64
                    or any(c not in '0123456789abcdef' for c in token)):
                raise DataStoreError('INVALID_VALUE')
        if type(self.expected_revision) is not int or not 0 <= self.expected_revision < 2**63-1:
            raise DataStoreError('INVALID_VALUE')
        if type(self.qualified) is not bool:
            raise DataStoreError('INVALID_VALUE')
        control_json(dict(self.confirmation))
        control_json(dict(self.checkpoint))


@dataclass(frozen=True)
class Issue:
    """One unresolved key/range/report/field aggregate, never a success ledger."""
    key: str
    scope: str
    reason: str
    evidence_token: str
    target: Mapping
    resolution: Mapping

    def __post_init__(self):
        for v in (self.key, self.scope, self.reason):
            identifier(v)
        if len(self.reason) > 64 or len(self.evidence_token) != 64:
            raise DataStoreError('INVALID_VALUE')
        control_json(dict(self.target))
        control_json(dict(self.resolution))


class Catalog:
    def __init__(self, engine: Engine, limits: StoreLimits):
        if engine.dialect.name != 'postgresql':
            raise DataStoreError('INVALID_CONFIGURATION')
        self.engine, self.limits = engine, limits

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            self.configure(connection)
            yield connection

    def configure(self, c):
        c.execute(text("SELECT set_config('statement_timeout', :v, true), "
                       "set_config('lock_timeout', :v, true)"),
                  {'v': str(self.limits.lock_timeout_ms)})

    def bind_root(self, root_token: UUID):
        policy = control_json(asdict(self.limits))
        with self.transaction() as c:
            c.execute(text('INSERT INTO data_store_runtime VALUES (1, :root, :policy) '
                           'ON CONFLICT (singleton) DO NOTHING'), {'root': root_token, 'policy': policy})
            row = c.execute(text('SELECT root_token, policy_json FROM data_store_runtime '
                                 'WHERE singleton=1')).mappings().one()
            if row['root_token'] != root_token or row['policy_json'] != policy:
                raise DataStoreError('CATALOG_MISMATCH')

    def dataset(self, name: str, c=None) -> dict:
        if c is None:
            with self.transaction() as connection:
                return self.dataset(name, connection)
        row = c.execute(text('SELECT * FROM data_store_datasets WHERE name=:n'),
                        {'n': name}).mappings().first()
        if row is None:
            raise DataStoreError('DATASET_MISSING')
        return dict(row)

    def register(self, spec: DatasetSpec, *, rebuild: bool = False):
        """Called under writer/commit locks. Incompatible change is explicit.

        rebuild=True changes the current descriptor only; it never deletes
        files. Readers reject old incompatible partitions until D02 rebuilds
        them through replace_partition. Unrelated files are not reinterpreted.
        """
        with self.transaction() as c:
            row = c.execute(text('SELECT descriptor_json FROM data_store_datasets WHERE name=:n'),
                            {'n': spec.name}).first()
            if row is None:
                c.execute(text('INSERT INTO data_store_datasets '
                               '(name, descriptor_json, schema_id, rule) VALUES (:n,:d,:s,:r)'),
                          {'n': spec.name, 'd': control_json(spec.descriptor()),
                           's': spec.schema_id, 'r': spec.rule})
            else:
                old = DatasetSpec.from_descriptor(json.loads(row[0]))
                if not rebuild and not spec.accepts(old):
                    raise DataStoreError('REBUILD_REQUIRED')
                if old.descriptor() != spec.descriptor():
                    c.execute(text('UPDATE data_store_datasets SET descriptor_json=:d, schema_id=:s, '
                                   'rule=:r, generation=generation+1, updated_at=clock_timestamp() '
                                   'WHERE name=:n'), {'n': spec.name, 'd': control_json(spec.descriptor()),
                                                     's': spec.schema_id, 'r': spec.rule})

    def files(self, dataset: str, partition: str, lower: bytes | None = None,
              upper: bytes | None = None, *, cap: int | None = None, c=None) -> list[dict]:
        if c is None:
            with self.transaction() as connection:
                return self.files(dataset, partition, lower, upper, cap=cap, c=connection)
        cap = cap or self.limits.changed_files
        conditions = ['dataset=:d', 'partition_key=:p']
        params = {'d': dataset, 'p': partition, 'cap': cap+1}
        if lower is not None:
            conditions.append('key_max >= :lo'); params['lo'] = lower
        if upper is not None:
            conditions.append('key_min < :hi'); params['hi'] = upper
        rows = c.execute(text('SELECT * FROM data_store_files WHERE ' + ' AND '.join(conditions)
                              + ' ORDER BY key_min LIMIT :cap'), params).mappings().all()
        if len(rows) > cap:
            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
        return [dict(r) for r in rows]

    def scope(self, dataset: str, scope: str, c=None) -> dict | None:
        if c is None:
            with self.transaction() as connection:
                return self.scope(dataset, scope, connection)
        row = c.execute(text('SELECT * FROM data_store_scopes WHERE dataset=:d AND scope_key=:s'),
                        {'d': dataset, 's': scope}).mappings().first()
        if row is None:
            return None
        result = dict(row)
        result['confirmation'] = json.loads(result.pop('confirmation_json'))
        result['checkpoint'] = json.loads(result.pop('checkpoint_json'))
        return result

    def garbage_usage(self, c=None) -> tuple[int, int]:
        if c is None:
            with self.transaction() as connection:
                return self.garbage_usage(connection)
        row = c.execute(text('SELECT count(*), COALESCE(sum(byte_count),0) FROM data_store_garbage')).one()
        return int(row[0]), int(row[1])

    def check_garbage(self, c, *, count: int = 0, nbytes: int = 0):
        # Serialize only queue admission, not a dataset's planning or reads.
        c.execute(text('SELECT singleton FROM data_store_runtime WHERE singleton=1 FOR UPDATE'))
        used_count, used_bytes = self.garbage_usage(c)
        if (used_count + count > self.limits.garbage_count
                or used_bytes + nbytes > self.limits.garbage_bytes):
            raise DataStoreError('GARBAGE_BUDGET_EXCEEDED')

    def prepare_file(self, dataset: str, path: str, size: int):
        """Record recovery intent BEFORE a unique file leaves scratch space."""
        with self.transaction() as c:
            self.check_garbage(c, count=1, nbytes=size)
            c.execute(text('INSERT INTO data_store_garbage '
                           '(dataset,path,byte_count,reason,not_before) VALUES '
                           "(:d,:p,:b,'prepared',clock_timestamp()+:grace*interval '1 second')"),
                      {'d': dataset, 'p': path, 'b': size, 'grace': self.limits.orphan_grace_seconds})

    def issues(self, dataset: str, *, scope: str | None = None, limit: int = 100,
               after: str = '') -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        with self.transaction() as c:
            rows = c.execute(text('SELECT * FROM data_store_issues WHERE dataset=:d AND issue_key>:a '
                                  + ('AND scope_key=:s ' if scope else '')
                                  + 'ORDER BY issue_key LIMIT :cap'),
                             {'d': dataset, 's': scope, 'a': after, 'cap': limit}).mappings().all()
            return [dict(r) for r in rows]

    def change_issues(self, c, dataset: str, issues: tuple[Issue, ...],
                      resolved: Mapping[str, str]):
        if len(issues) + len(resolved) > 1000:
            raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
        c.execute(text('SELECT singleton FROM data_store_runtime WHERE singleton=1 FOR UPDATE'))
        for key, evidence in resolved.items():
            # Clearing one member's error cannot clear another member/field.
            c.execute(text('DELETE FROM data_store_issues WHERE dataset=:d AND issue_key=:k '
                           'AND evidence_token=:e'), {'d': dataset, 'k': identifier(key), 'e': evidence})
        for issue in issues:
            c.execute(text('INSERT INTO data_store_issues '
                           '(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json) '
                           'VALUES (:d,:k,:s,:r,:e,:t,:v) ON CONFLICT (dataset,issue_key) DO UPDATE SET '
                           'scope_key=EXCLUDED.scope_key,reason=EXCLUDED.reason,'
                           'evidence_token=EXCLUDED.evidence_token,target_json=EXCLUDED.target_json,'
                           'resolution_json=EXCLUDED.resolution_json,'
                           'attempts=LEAST(data_store_issues.attempts,9223372036854775806)+1,'
                           'last_seen=clock_timestamp()'),
                      {'d': dataset, 'k': issue.key, 's': issue.scope, 'r': issue.reason,
                       'e': issue.evidence_token, 't': control_json(dict(issue.target)),
                       'v': control_json(dict(issue.resolution))})
        total = c.execute(text('SELECT count(*) FROM data_store_issues')).scalar_one()
        if total > self.limits.issue_count:
            raise DataStoreError('ISSUE_BUDGET_EXCEEDED')


def basis_hash(files: list[dict]) -> str:
    return fingerprint([(str(f['id']), f['content_hash'], f['schema_id'], f['rule']) for f in files])
