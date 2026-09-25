"""Callable current-only Parquet kernel; one bounded atomic source range/object.

D02 supplies verified input tokens, sorted normalized Arrow batches and source
conditions. D03 manages legacy maintenance. D04 registers public routes/tasks.
Nothing in this module starts a service or changes an existing application route.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import errno
import time
from functools import wraps
import json
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping
from uuid import UUID, uuid4

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .budget import Budget, Metrics, QuotaFile
from .catalog import Catalog, SourceUpdate, Issue, basis_hash
from .errors import DataStoreError
from .filesystem import LocalFiles, digest_file
from .limits import StoreLimits
from .locking import DatasetLocks
from .schema import DatasetSpec, identifier, prefix_end
from .values import control_json


def _public_errors(function):
    """Do not expose native SQL, paths or bound source values at the interface."""
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SQLAlchemyError as error:
            state = getattr(error.orig, 'sqlstate', None) if hasattr(error, 'orig') else None
            code = {'57014':'QUERY_TIMEOUT', '55P03':'LOCK_TIMEOUT'}.get(state, 'CATALOG_UNAVAILABLE')
            raise DataStoreError(code) from None
        except OSError as error:
            raise DataStoreError('DISK_PRESSURE' if error.errno in (errno.ENOSPC, errno.EDQUOT)
                                 else 'STORAGE_UNAVAILABLE') from None
        except MemoryError:
            raise DataStoreError('MEMORY_PRESSURE') from None
    return call


@dataclass(frozen=True)
class CommitResult:
    generation: int
    changed: bool
    idempotent: bool
    qualified: bool
    metrics: Metrics = field(compare=False)
    cleanup_pending: bool = False


class CurrentStore:
    """Use as a context manager with a caller-owned PostgreSQL Engine.

    Root must already exist on a supported shared local filesystem. initialize
    explicitly creates only this kernel's directories/catalog root binding;
    Alembic creates schema separately. No import-time I/O, automatic migration,
    legacy model dependency, business-data copy or reset is performed.
    """
    @_public_errors
    def __init__(self, engine, root: str | Path, *, cursor_key: bytes,
                 limits: StoreLimits | None = None, initialize: bool = False,
                 _fault: Callable[[str], None] | None = None):
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32:
            raise DataStoreError('INVALID_CONFIGURATION')
        self.limits = limits or StoreLimits()
        self.cursor_key = cursor_key
        self.fault = _fault or (lambda point: None)
        self.catalog = Catalog(engine, self.limits)
        self.files = LocalFiles(root)
        self.locks = None
        try:
            if initialize:
                self.files.initialize()
            self.locks = DatasetLocks(self.files.root / '.locks')
            self.catalog.bind_root(self.files.root_token(self.locks))
            self.budget = Budget(self.files, self.locks, self.limits)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.locks is not None:
            self.locks.close()
        self.files.close()

    @_public_errors
    def register(self, spec: DatasetSpec, *, prepare_rebuild: bool = False):
        """Add one static contract or explicitly mark changed semantics/rules.

        prepare_rebuild NEVER deletes business files: queries reject incompatible
        current shards until replace_partition has rebuilt the affected range.
        """
        with self.locks.writer(spec.name, timeout_ms=self.limits.lock_timeout_ms) as guard:
            with guard.commit(timeout_ms=self.limits.lock_timeout_ms):
                self.catalog.register(spec, rebuild=prepare_rebuild)

    def _spec_current(self, spec: DatasetSpec, state: dict):
        if state['schema_id'] != spec.schema_id or state['rule'] != spec.rule:
            raise DataStoreError('REBUILD_REQUIRED')

    @_public_errors
    def describe_capability(self, spec: DatasetSpec) -> dict:
        """Small current descriptor; no file paths or old generations accepted."""
        with self.locks.read(spec.name, timeout_ms=self.limits.lock_timeout_ms):
            state = self.catalog.dataset(spec.name)
            self._spec_current(spec, state)
            with self.catalog.transaction() as c:
                issue_count = c.execute(text('SELECT count(*) FROM data_store_issues WHERE dataset=:d'),
                                        {'d': spec.name}).scalar_one()
                formats = c.execute(text('SELECT DISTINCT contract_json FROM data_store_files '
                                         'WHERE dataset=:d LIMIT 129'), {'d': spec.name}).scalars().all()
                if len(formats) > 128:
                    raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
                mismatched = any(not spec.accepts(DatasetSpec.from_descriptor(json.loads(f))) for f in formats)
            return {'dataset': spec.name, 'generation': state['generation'],
                    'schema_id': spec.schema_id, 'rule': spec.rule,
                    'semantics': dict(spec.semantics), 'business_key': list(spec.key),
                    'fields': [{'name': f.name, 'type': str(f.type), 'nullable': f.nullable}
                               for f in spec.schema],
                    'row_count': state['row_count'], 'byte_count': state['byte_count'],
                    'status': 'rebuild_required' if mismatched else
                              'restricted' if issue_count else 'available' if state['row_count'] else 'empty',
                    'issues': issue_count, 'updated_at': state['updated_at'].isoformat(),
                    'history_queries': False, 'schema_checked_on_read': True}

    @_public_errors
    def source_state(self, dataset: str, scope: str) -> dict | None:
        return self.catalog.scope(identifier(dataset), identifier(scope))

    @_public_errors
    def upsert(self, spec: DatasetSpec, partition: str, batches: Iterable[pa.RecordBatch],
               source: SourceUpdate, *, lower: tuple, upper: tuple,
               source_check: Callable[[dict | None], bool], cancelled=None,
               issues: tuple[Issue, ...] = (), resolved: Mapping[str, str] | None = None) -> CommitResult:
        """Merge [lower,upper); keys strictly ordered; one finite microbatch.

        Boundary/source tokens describe verified actual input. Replaying a token
        only skips work while that scope's current file basis, rule and context
        still match. No-op does not call the input iterable. Source order itself
        is a D02 condition, never inferred from completion time or revision CAS.
        """
        if spec.report_prefix:
            raise DataStoreError('REPORT_INCOMPLETE')
        return self._apply(spec, partition, batches, source, spec.key_bytes(lower),
                           spec.key_bytes(upper), 'upsert', (), source_check, cancelled, issues,
                           resolved or {})

    @_public_errors
    def replace_report(self, spec: DatasetSpec, partition: str, report_key: tuple,
                       batches: Iterable[pa.RecordBatch], source: SourceUpdate, *,
                       complete: bool, source_check: Callable[[dict | None], bool],
                       cancelled=None, issues: tuple[Issue, ...] = (),
                       resolved: Mapping[str, str] | None = None) -> CommitResult:
        """One complete report prefix across ALL necessary member shards.

        Empty with complete=True is explicit replacement/withdrawal, not a
        guessed provider empty response. Incomplete input never publishes a
        subset of members. D02 proves completeness and source order.
        """
        if complete is not True or not spec.report_prefix or len(report_key) != spec.report_prefix:
            raise DataStoreError('REPORT_INCOMPLETE')
        lower = spec.key_bytes(report_key)
        return self._apply(spec, partition, batches, source, lower, prefix_end(lower),
                           'report', report_key, source_check, cancelled, issues, resolved or {})

    @_public_errors
    def replace_partition(self, spec: DatasetSpec, partition: str,
                          batches: Iterable[pa.RecordBatch], source: SourceUpdate, *,
                          complete: bool, source_check: Callable[[dict | None], bool],
                          cancelled=None, issues: tuple[Issue, ...] = (),
                          resolved: Mapping[str, str] | None = None) -> CommitResult:
        """Explicit bounded local rebuild, including an incompatible old shard.

        It never reads/reinterprets incompatible old bytes, never copies the
        full catalog and never grants permission to delete source originals.
        The input must cover this whole declared partition within commit limits.
        """
        if complete is not True:
            raise DataStoreError('REPORT_INCOMPLETE')
        return self._apply(spec, partition, batches, source, b'', b'\xff'*2048,
                           'partition', (), source_check, cancelled, issues, resolved or {})

    def _apply(self, spec, partition, batches, source, lower, upper, mode, report_key,
               source_check, cancelled, issues, resolved):
        identifier(partition)
        if lower >= upper or not callable(source_check) or (issues and source.qualified):
            raise DataStoreError('INVALID_VALUE')
        if not isinstance(issues, tuple) or len(issues) + len(resolved) > 1000:
            raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
        resolved = dict(resolved)  # caller mutation cannot change a pending commit
        metrics = Metrics()
        try:
            with self.locks.writer(spec.name, timeout_ms=self.limits.lock_timeout_ms,
                                   cancelled=cancelled) as guard:
                state = self.catalog.dataset(spec.name)
                self._spec_current(spec, state)
                old = self.catalog.files(spec.name, partition, lower, upper)
                scope = self.catalog.scope(spec.name, source.scope_key)
                if source_check(scope) is not True:
                    raise DataStoreError('SOURCE_CONFLICT')
                basis = basis_hash(old)
                if (scope is not None and scope['input_token'] == source.input_token
                        and scope['context_token'] == source.context_token
                        and scope['schema_id'] == spec.schema_id and scope['rule'] == spec.rule
                        and scope['basis_hash'] == basis and scope['partition_key'] == partition
                        and bytes(scope['key_min']) == lower and bytes(scope['key_end']) == upper
                        and scope['confirmation'] == json.loads(source.confirmation_json)
                        and scope['checkpoint'] == json.loads(source.checkpoint_json)
                        and scope['qualified'] == source.qualified and not issues and not resolved):
                    result = CommitResult(state['generation'], False, True, source.qualified, metrics)
                else:
                    if (scope['revision'] if scope else 0) != source.expected_revision:
                        raise DataStoreError('SOURCE_CONFLICT')
                    with self.catalog.transaction() as c:
                        self.catalog.check_garbage(c, count=1)
                    with self.budget.reserve('write', cancelled=cancelled, metrics=metrics) as space:
                        incoming, count = self._stage_input(spec, partition, batches, space, lower, upper)
                        current_bytes = sum(f['byte_count'] for f in old)
                        if current_bytes > self.limits.commit_bytes:
                            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                        new = []
                        changed = bool(count or old)
                        if mode == 'partition':
                            # Rebuild must not interpret bytes under an incompatible rule/schema.
                            output = self._input_batches(incoming, spec)
                            new = self._stage_output(spec, partition, output, space)
                        else:
                            with self._relations(spec, old, incoming, space) as con:
                                equality = ' AND '.join(f'i."{k}"=o."{k}"' for k in spec.key)
                                values = ' AND '.join(f'i."{f.name}" IS NOT DISTINCT FROM o."{f.name}"'
                                                      for f in spec.schema)
                                changed = con.execute('SELECT EXISTS(SELECT 1 FROM incoming i '
                                                      'WHERE NOT EXISTS(SELECT 1 FROM old o WHERE '
                                                      + equality + ' AND ' + values + '))').fetchone()[0]
                                params = list(report_key)
                                target = ' AND '.join(f'o."{k}"=?' for k in spec.key[:spec.report_prefix])
                                if mode == 'report':
                                    changed = changed or con.execute('SELECT EXISTS(SELECT 1 FROM old o WHERE '
                                        + target + ' AND NOT EXISTS(SELECT 1 FROM incoming i WHERE '
                                        + equality + '))', params).fetchone()[0]
                                if changed:
                                    keep = ('NOT (' + target + ')' if mode == 'report' else
                                            'NOT EXISTS(SELECT 1 FROM incoming i WHERE ' + equality + ')')
                                    order = ','.join('"'+k+'"' for k in spec.key)
                                    sql = ('SELECT * FROM (SELECT o.* FROM old o WHERE ' + keep
                                           + ' UNION ALL BY NAME SELECT * FROM incoming) ORDER BY ' + order)
                                    stream = con.execute(sql, params if mode == 'report' else []).fetch_record_batch(
                                        spec.bounded_rows(self.limits.batch_rows, self.limits.batch_bytes))
                                    new = self._stage_output(spec, partition, stream, space)
                        # Output is durable and recoverable BEFORE the directory can refer to it.
                        for f in new:
                            space.check()
                            self.catalog.prepare_file(spec.name, f['path'], f['byte_count'])
                            self.fault('before_promote')
                            self.files.promote(f.pop('_temporary'), f['path'], fault=self.fault)
                            space.promoted_bytes += f['byte_count']
                        space.check()
                        result = self._commit(spec, partition, source, lower, upper, state, old if changed else [],
                                              new, scope, guard, source_check, issues, resolved, metrics,
                                              changed, cancelled)
                    if space.cleanup_pending:
                        result = CommitResult(result.generation, result.changed, result.idempotent,
                                              result.qualified, result.metrics, True)
                # Old readers have left before cleanup can obtain the commit lock.
                # Successful directory commit is never relabelled failed by deletion/log failure.
            try:
                pending = self.cleanup(spec.name, limit=self.limits.cleanup_batch)
                result = CommitResult(result.generation, result.changed, result.idempotent,
                                      result.qualified, result.metrics, result.cleanup_pending or pending['pending'] > 0
                                      or pending.get('log_cleanup_pending', False))
            except (DataStoreError, OSError, SQLAlchemyError):
                result = CommitResult(result.generation, result.changed, result.idempotent,
                                      result.qualified, result.metrics, True)
            self._summary(spec.name, 'noop' if result.idempotent else 'committed', metrics)
            return result
        except Exception as error:
            self._summary(spec.name, error.code if isinstance(error, DataStoreError) else 'failed', metrics)
            raise

    def _stage_input(self, spec, partition, batches, space, lower, upper):
        path = space.new_file()
        count, nbytes, last = 0, 0, None
        with QuotaFile(path, space) as sink:
            with pq.ParquetWriter(sink, spec.schema, compression='zstd', version='2.6',
                                  use_dictionary=False, write_page_checksum=True) as writer:
                for batch in batches:
                    space.check()
                    keys = spec.validate_batch(batch, max_rows=self.limits.batch_rows,
                                               max_bytes=self.limits.batch_bytes)
                    spec.check_partition(batch, partition)
                    count += batch.num_rows; nbytes += batch.nbytes
                    if count > self.limits.commit_rows or nbytes > self.limits.commit_bytes:
                        raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                    if keys:
                        if keys[0] < lower or keys[-1] >= upper or (last is not None and keys[0] <= last):
                            raise DataStoreError('KEY_ORDER_INVALID')
                        last = keys[-1]
                    writer.write_batch(batch)
        space.metrics.input_rows, space.metrics.input_bytes = count, nbytes
        return path, count

    def _input_batches(self, path, spec):
        with pq.ParquetFile(path) as source:
            yield from source.iter_batches(batch_size=spec.bounded_rows(self.limits.batch_rows, self.limits.batch_bytes))

    def _stage_output(self, spec, partition, batches, space):
        files, writer, sink, path = [], None, None, None
        count, first, last, total_rows, total_bytes = 0, None, None, 0, 0

        def finish():
            nonlocal writer, sink
            if writer is None:
                return
            writer.close(); writer = None
            sink.close(); sink = None
            self.fault('after_write')
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
            self.fault('after_file_fsync')
            digest = digest_file(path, space.check)
            # Full readability/type/key validation in bounded Arrow batches.
            seen, previous = 0, None
            with pq.ParquetFile(path, page_checksum_verification=True) as source:
                for check_batch in source.iter_batches(batch_size=spec.bounded_rows(self.limits.batch_rows, self.limits.batch_bytes)):
                    keys = spec.validate_batch(check_batch, max_rows=self.limits.batch_rows,
                                               max_bytes=self.limits.batch_bytes)
                    if keys and previous is not None and keys[0] <= previous:
                        raise DataStoreError('KEY_ORDER_INVALID')
                    previous = keys[-1] if keys else previous
                    seen += check_batch.num_rows
                    space.check()
            if seen != count:
                raise DataStoreError('FILE_INVALID')
            final = self.files.object_path(spec.name)
            size = path.stat().st_size
            files.append({'id': UUID(Path(final).stem), 'dataset': spec.name,
                          'partition_key': partition, 'key_min': first, 'key_max': last,
                          'path': final, 'content_hash': digest, 'byte_count': size,
                          'row_count': count, 'schema_id': spec.schema_id, 'rule': spec.rule,
                          'contract_json': control_json(spec.descriptor()),
                          '_temporary': path})
            space.metrics.written_bytes += size
            space.metrics.files_written += 1

        try:
            for original in batches:
                # DuckDB returns UTC as Etc/UTC; an exact safe Arrow cast normalizes
                # the declared representation after checking compatible SQL types.
                try:
                    batch = pa.Table.from_batches([original]).cast(spec.schema, safe=True).to_batches()[0]
                except (pa.ArrowException, IndexError):
                    raise DataStoreError('FILE_INVALID') from None
                space.check()
                for start in range(0, batch.num_rows, min(self.limits.batch_rows, self.limits.file_rows)):
                    piece = batch.slice(start, min(self.limits.batch_rows, self.limits.file_rows))
                    keys = spec.validate_batch(piece, max_rows=self.limits.batch_rows,
                                               max_bytes=self.limits.batch_bytes)
                    spec.check_partition(piece, partition)
                    if not keys:
                        continue
                    total_rows += piece.num_rows; total_bytes += piece.nbytes
                    if total_rows > self.limits.commit_rows * 2 or total_bytes > self.limits.commit_bytes * 2:
                        raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                    if writer is not None and count + piece.num_rows > self.limits.file_rows:
                        finish(); count, first, last = 0, None, None
                    if writer is None:
                        if len(files) >= self.limits.changed_files:
                            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                        path = space.new_file(); sink = QuotaFile(path, space)
                        writer = pq.ParquetWriter(sink, spec.schema, compression='zstd', version='2.6',
                                                 use_dictionary=False, write_page_checksum=True)
                    if last is not None and keys[0] <= last:
                        raise DataStoreError('KEY_ORDER_INVALID')
                    first = first if first is not None else keys[0]
                    last = keys[-1]; count += piece.num_rows
                    writer.write_batch(piece)
            finish()
        finally:
            if writer is not None:
                writer.close()
            if sink is not None:
                sink.close()
        if any(a['key_max'] >= b['key_min'] for a, b in zip(files, files[1:])):
            raise DataStoreError('KEY_ORDER_INVALID')
        return files

    @contextmanager
    def _relations(self, spec, refs, incoming, space):
        with ExitStack() as stack:
            paths = self._pin_files(spec, refs, stack, space)
            con = stack.enter_context(space.connection())
            con.register('_layout', pa.Table.from_batches([], schema=spec.schema))
            if paths:
                con.read_parquet(paths, hive_partitioning=False, union_by_name=True).create_view('old_raw')
                con.execute('CREATE TEMP VIEW old AS SELECT * FROM old_raw UNION ALL BY NAME SELECT * FROM _layout')
            else:
                con.execute('CREATE TEMP VIEW old AS SELECT * FROM _layout')
            con.read_parquet(str(incoming), hive_partitioning=False).create_view('incoming')
            yield con

    def _pin_files(self, spec, refs, stack, space):
        paths = []
        for f in refs:
            space.check()
            stored_spec = DatasetSpec.from_descriptor(json.loads(f['contract_json']))
            if not spec.accepts(stored_spec) or f['schema_id'] != stored_spec.schema_id:
                raise DataStoreError('REBUILD_REQUIRED')
            fd = stack.enter_context(self.files.open_object(f['path']))
            if os.fstat(fd).st_size != f['byte_count']:
                raise DataStoreError('FILE_INVALID')
            path = f'/proc/self/fd/{fd}'
            with pq.ParquetFile(path) as p:
                if (not p.schema_arrow.equals(stored_spec.schema, check_metadata=True)
                        or p.metadata.num_rows != f['row_count']):
                    raise DataStoreError('REBUILD_REQUIRED')
            if digest_file(Path(path), space.check) != f['content_hash']:
                raise DataStoreError('FILE_INVALID')
            paths.append(path)
            space.metrics.current_read_bytes += f['byte_count']
            space.metrics.files_read += 1
        return paths

    def _commit(self, spec, partition, source, lower, upper, state, old, new, scope,
                guard, source_check, issues, resolved, metrics, changed, cancelled):
        commit_id = uuid4()
        counts = {'rows': sum(f['row_count'] for f in new) - sum(f['row_count'] for f in old),
                  'bytes': sum(f['byte_count'] for f in new) - sum(f['byte_count'] for f in old)}
        with guard.commit(timeout_ms=self.limits.lock_timeout_ms, cancelled=cancelled):
            deadline = time.monotonic() + min(5000, self.limits.write_timeout_ms) / 1000
            def check_commit():
                if cancelled and cancelled():
                    raise DataStoreError('OPERATION_CANCELLED')
                if time.monotonic() >= deadline:
                    raise DataStoreError('QUERY_TIMEOUT')
            c = self.catalog.engine.connect()
            try:
                self.catalog.configure(c)
                current = self.catalog.dataset(spec.name, c)
                if current['generation'] != state['generation']:
                    raise DataStoreError('DATA_CHANGED')
                active = self.catalog.scope(spec.name, source.scope_key, c)
                if (active['revision'] if active else 0) != source.expected_revision or source_check(active) is not True:
                    raise DataStoreError('SOURCE_CONFLICT')
                self.catalog.check_garbage(c, count=len(old), nbytes=sum(f['byte_count'] for f in old))
                check_commit()
                for f in old:
                    check_commit()
                    c.execute(text('DELETE FROM data_store_files WHERE id=:i AND dataset=:d'),
                              {'i': f['id'], 'd': spec.name})
                    c.execute(text("INSERT INTO data_store_garbage (dataset,path,byte_count,reason) "
                                   "VALUES (:d,:p,:b,'retired')"),
                              {'d': spec.name, 'p': f['path'], 'b': f['byte_count']})
                for f in new:
                    check_commit()
                    c.execute(text('INSERT INTO data_store_files (id,dataset,partition_key,key_min,key_max,path,'
                                   'content_hash,row_count,byte_count,schema_id,rule,contract_json) VALUES '
                                   '(:id,:dataset,:partition_key,:key_min,:key_max,:path,:content_hash,'
                                   ':row_count,:byte_count,:schema_id,:rule,:contract_json)'), f)
                    c.execute(text('DELETE FROM data_store_garbage WHERE path=:p'), {'p': f['path']})
                basis = basis_hash(self.catalog.files(spec.name, partition, lower, upper, c=c))
                values = {'d': spec.name, 's': source.scope_key, 'v': source.expected_revision + 1,
                          'p': partition, 'lo': lower, 'hi': upper, 'i': source.input_token,
                          'x': source.context_token, 'b': basis, 'schema': spec.schema_id, 'r': spec.rule,
                          'co': source.confirmation_json, 'cp': source.checkpoint_json,
                          'q': source.qualified}
                c.execute(text('INSERT INTO data_store_scopes (dataset,scope_key,revision,partition_key,key_min,key_end,'
                               'input_token,context_token,basis_hash,schema_id,rule,confirmation_json,checkpoint_json,qualified) '
                               'VALUES (:d,:s,:v,:p,:lo,:hi,:i,:x,:b,:schema,:r,:co,:cp,:q) '
                               'ON CONFLICT (dataset,scope_key) DO UPDATE SET '
                               'revision=EXCLUDED.revision,partition_key=EXCLUDED.partition_key,key_min=EXCLUDED.key_min,'
                               'key_end=EXCLUDED.key_end,input_token=EXCLUDED.input_token,context_token=EXCLUDED.context_token,'
                               'basis_hash=EXCLUDED.basis_hash,schema_id=EXCLUDED.schema_id,rule=EXCLUDED.rule,'
                               'confirmation_json=EXCLUDED.confirmation_json,checkpoint_json=EXCLUDED.checkpoint_json,'
                               'qualified=EXCLUDED.qualified,updated_at=clock_timestamp()'), values)
                self.catalog.change_issues(c, spec.name, issues, resolved, check=check_commit)
                metrics.files_retired = len(old)
                metrics.catalog_file_changes = len(old) + len(new)
                result = {'changed': bool(changed), 'qualified': source.qualified, 'rows_delta': counts['rows'],
                          'metrics': asdict(metrics)}
                c.execute(text('UPDATE data_store_datasets SET generation=generation+1, '
                               'row_count=row_count+:rows,byte_count=byte_count+:bytes,last_commit=:id,'
                               'last_result=:result,updated_at=clock_timestamp() WHERE name=:d'),
                          {**counts, 'id': commit_id, 'result': control_json(result), 'd': spec.name})
                self.fault('before_catalog_commit')
                check_commit()
                c.commit()
                self.fault('after_catalog_commit')
            except Exception as error:
                try:
                    c.rollback()
                except SQLAlchemyError:
                    pass
                c.close()
                # Still hold writer + exclusive commit locks. No later writer can
                # overwrite the sole acknowledgement marker during confirmation.
                try:
                    check = self.catalog.dataset(spec.name)
                except SQLAlchemyError:
                    raise DataStoreError('COMMIT_UNKNOWN') from None
                if check['last_commit'] != commit_id:
                    raise error
            finally:
                c.close()
        return CommitResult(state['generation']+1, bool(changed), False, source.qualified, metrics)

    @_public_errors
    def record_problems(self, spec: DatasetSpec, partition: str, source: SourceUpdate,
                        issues: tuple[Issue, ...], *, lower: tuple, upper: tuple,
                        source_check: Callable[[dict | None], bool]):
        """D02 retry handoff: failed/processed scope, bounded aggregated restrictions.

        Does not open business files or publish a subset of a bad report. The
        supplied source predicate must also guard an older error from reopening
        a newer state. Budget overflow fails explicitly; callers must aggregate
        their actual affected range, never report truncated issues as complete.
        """
        if source.qualified or not issues:
            raise DataStoreError('INVALID_VALUE')
        identifier(partition)
        lo, hi = spec.key_bytes(lower), spec.key_bytes(upper)
        if lo >= hi:
            raise DataStoreError('INVALID_VALUE')
        with self.locks.writer(spec.name, timeout_ms=self.limits.lock_timeout_ms) as guard:
            state = self.catalog.dataset(spec.name)
            self._spec_current(spec, state)
            scope = self.catalog.scope(spec.name, source.scope_key)
            return self._commit(spec, partition, source, lo, hi, state, [], [], scope, guard,
                                source_check, issues, {}, Metrics(), False, None)

    @_public_errors
    def cleanup(self, dataset: str, *, limit: int | None = None):
        from .maintenance import cleanup
        return cleanup(self, dataset, limit=limit)

    @_public_errors
    def recover(self, dataset: str):
        """Authoritative current status + bounded orphan/deletion recovery.

        Does not replay unknown input. The caller re-reads source_state before
        resubmission; current files remain authoritative after ambiguous commit.
        """
        self.budget.sweep()
        state = self.catalog.dataset(dataset)
        result = self.cleanup(dataset)
        return {'generation': state['generation'], 'row_count': state['row_count'], **result}

    @_public_errors
    def read(self, spec, query):
        from .readers import read_many
        return read_many(self, [(spec, query)])[0]

    @_public_errors
    def read_many(self, queries, *, expected_generations=None, cancelled=None):
        from .readers import read_many
        return read_many(self, queries, expected_generations=expected_generations, cancelled=cancelled)

    def _summary(self, dataset, outcome, metrics):
        from .maintenance import write_summary
        try:
            write_summary(self, dataset, outcome, metrics)
        except (OSError, DataStoreError):
            # Never mask the result/commit acknowledgement with a log failure.
            pass
