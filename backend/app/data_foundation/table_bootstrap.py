"""Compare a fixed local capture with live tables without copying unchanged rows.

Each table runs in one repeatable-read transaction. A small temporary index of
primary keys, hashes and fixed chunk locators supports a streaming merge with
the live table. Only changed rows become durable outbox events. Concurrent
writes are also retained by the installed row triggers; later consumers must
deduplicate equal values and fence head activation against current source.
"""
import argparse
import json
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Column, MetaData, Table, Text, Integer, Uuid, cast, func, select, text
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key
from app.data_foundation.models import Baseline, SourceRef, DependencyEntry
from app.data_foundation.source_refs import read_source, register_baseline
from app.data_foundation.table_intake import MANIFEST_DATASET, TABLE_SOURCES
from app.data_foundation.update_models import TableChange
from app.data_foundation.staged_inputs import require_space

RECEIPT_DATASET = 'local_table_bootstrap_receipt'
IGNORED_TOUCH_FIELDS = {'updated_at', 'last_seen_at'}


def stable_content(row):
    """Match the trigger's timestamp-only suppression without lossy floats."""
    return {key: value for key, value in row.items() if key not in IGNORED_TOUCH_FIELDS}


def original_row(session, ref_id, ordinal, cache):
    if cache[0] != ref_id:
        cache[:] = [ref_id, read_source(session, ref_id)]
    return cache[1][ordinal]


def bootstrap_table(session, *, capture_ref_id, native_dataset, execution_id, archive_root,
                    minimum_free_bytes=10 * 1024**3):
    """Seal one exact table comparison; caller commits or rolls back atomically."""
    if session.bind.dialect.name != 'postgresql' or session.connection().get_isolation_level() != 'REPEATABLE READ':
        raise FoundationError('SNAPSHOT_REQUIRED', '本地表差异核对需要 PostgreSQL 可重复读事务。')
    spec = next((item for item in TABLE_SOURCES if item.dataset == native_dataset), None)
    if spec is None:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地表差异核对领域不在固定表清单。')
    capture = session.get(SourceRef, capture_ref_id)
    if capture is None or capture.source != 'tushare' or capture.dataset != MANIFEST_DATASET:
        raise FoundationError('SOURCE_INVALID', '本地表差异核对缺少固定全量捕获。')
    snapshot_at = session.scalar(text('SELECT transaction_timestamp()'))
    snapshot_id = session.scalar(text('SELECT pg_current_snapshot()::text'))
    event = digest('local-table-bootstrap-v1', [capture_ref_id, native_dataset])
    lock_key(session, 'local-table-bootstrap', event)
    existing = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id).where(
        Baseline.source == 'foundation', Baseline.dataset == RECEIPT_DATASET, Baseline.event_key == event))
    if existing:
        return existing, read_source(session, existing.id)[0]
    manifest = read_source(session, capture.id)
    if len(manifest) != 1 or manifest[0].get('format') != 'local-tables-v1':
        raise FoundationError('SOURCE_INVALID', '固定本地表捕获清单格式无效。')
    group = next((entry for entry in manifest[0]['tables'] if entry['dataset'] == native_dataset), None)
    table = spec.model.__table__
    if group is None or group['table'] != table.name or not group['chunks']:
        raise FoundationError('SOURCE_INVALID', '固定本地表捕获缺少对应表或分块。')
    dependency_ids = [UUID(chunk['source_ref_id']) for chunk in group['chunks']]
    proofs = {row.source_ref_id: row.content_hash for row in session.scalars(select(DependencyEntry).where(
        DependencyEntry.manifest_id == UUID(manifest[0]['dependency_id']),
        DependencyEntry.source_ref_id.in_(dependency_ids)))}
    if len(proofs) != len(dependency_ids) or any(proofs.get(ref) != item['hash']
            for ref, item in zip(dependency_ids, group['chunks'])):
        raise FoundationError('SOURCE_INVALID', '固定本地表差异核对缺少完整保留依赖。')
    require_space(archive_root, minimum_free_bytes)
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    session.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
    columns = tuple(table.primary_key.columns)
    temporary = Table('qf_bootstrap_keys', MetaData(), *[
        Column(f'k{index}', column.type, primary_key=True) for index, column in enumerate(columns)],
        Column('old_hash', Text, nullable=False), Column('source_ref_id', Uuid, nullable=False),
        Column('ordinal', Integer, nullable=False), prefixes=['TEMPORARY'])
    # A dedicated connection cannot leak the temporary index across a retry.
    temporary.create(session.connection())
    inserted = changed = deleted = original_count = current_count = 0
    raw_connection = session.connection().connection.driver_connection
    copy_columns = ','.join([f'k{index}' for index in range(len(columns))] + ['old_hash', 'source_ref_id', 'ordinal'])
    for chunk_index, item in enumerate(group['chunks']):
        require_space(archive_root, minimum_free_bytes)
        ref_id = UUID(item['source_ref_id'])
        reference = session.get(SourceRef, ref_id)
        rows = read_source(session, ref_id)
        if (reference.dataset != native_dataset or reference.content_hash != item['hash']
                or item['ordinal'] != chunk_index or len(rows) != item['rows']):
            raise FoundationError('SOURCE_INVALID', '固定表分块校验失败，未核对可变表。')
        with raw_connection.cursor() as cursor, cursor.copy(f'COPY qf_bootstrap_keys ({copy_columns}) FROM STDIN') as stream:
            for ordinal, row in enumerate(rows):
                if not isinstance(row, dict) or any(name.name not in row for name in columns):
                    raise FoundationError('SOURCE_INVALID', '固定表记录缺少主键。')
                stream.write_row(tuple(row[column.name] for column in columns) +
                                 (digest('local-table-stable-row-v1', stable_content(row)), ref_id, ordinal))
            original_count += len(rows)
    if original_count != group['rows']:
        raise FoundationError('CAPTURE_COUNT_MISMATCH', '固定表分块与原始总数不一致。')
    join = [table.c[column.name] == temporary.c[f'k{index}'] for index, column in enumerate(columns)]
    from sqlalchemy import and_
    row_json = cast(func.row_to_json(table.table_valued()), Text)
    live = select(row_json, temporary.c.old_hash, temporary.c.source_ref_id, temporary.c.ordinal).select_from(
        table.outerjoin(temporary, and_(*join)))
    if spec.source_column:
        live = live.where(table.c[spec.source_column] == 'tushare')
    cache = [None, None]
    result = session.execute(live.execution_options(stream_results=True, yield_per=1000))
    for partition in result.partitions(1000):
        require_space(archive_root, minimum_free_bytes)
        writes = []
        for json_value, old_hash, source_ref_id, ordinal in partition:
            current_count += 1
            row = json.loads(json_value, parse_float=Decimal)
            if old_hash == digest('local-table-stable-row-v1', stable_content(row)):
                continue
            key = {column.name: row[column.name] for column in columns}
            prior = original_row(session, source_ref_id, ordinal, cache) if source_ref_id else None
            writes.append(dict(dataset=native_dataset, key_json=encode(key),
                before_json=encode(prior) if prior else None, after_json=encode(row),
                event_key=digest('local-table-bootstrap-delta-v1', [capture_ref_id, native_dataset, key])))
            if prior: changed += 1
            else: inserted += 1
        if writes:
            session.execute(TableChange.__table__.insert(), writes)
    result.close()
    # A left join identifies baseline keys absent from this fixed live view.
    missing_join = join + ([table.c[spec.source_column] == 'tushare'] if spec.source_column else [])
    missing = select(temporary.c.source_ref_id, temporary.c.ordinal, *[
        temporary.c[f'k{index}'] for index in range(len(columns))]).select_from(
        temporary.outerjoin(table, and_(*missing_join))).where(table.c[columns[0].name].is_(None))
    result = session.execute(missing.execution_options(stream_results=True, yield_per=1000))
    for partition in result.partitions(1000):
        require_space(archive_root, minimum_free_bytes)
        writes = []
        for source_ref_id, ordinal, *key_values in partition:
            prior = original_row(session, source_ref_id, ordinal, cache)
            key = {column.name: value for column, value in zip(columns, key_values)}
            writes.append(dict(dataset=native_dataset, key_json=encode(key), before_json=encode(prior),
                after_json=None, event_key=digest('local-table-bootstrap-delta-v1', [capture_ref_id, native_dataset, key])))
            deleted += 1
        if writes:
            session.execute(TableChange.__table__.insert(), writes)
    result.close()
    if original_count + inserted - deleted != current_count:
        raise FoundationError('CAPTURE_COUNT_MISMATCH', '本地表差异核对总数不守恒。')
    temporary.drop(session.connection())
    result_body = dict(dataset=native_dataset, capture_ref_id=str(capture_ref_id),
        fixed_original_rows=original_count, snapshot_current_rows=current_count,
        unchanged=current_count-inserted-changed, inserted=inserted, changed=changed, deleted=deleted,
        snapshot_at=snapshot_at, snapshot_id=snapshot_id, semantics='local_table_difference_only')
    receipt = register_baseline(session, source='foundation', dataset=RECEIPT_DATASET,
        scope=dict(capture_ref_id=str(capture_ref_id), dataset=native_dataset), rows=[result_body],
        observed_at=capture.observed_at, decoder_id=execution_id, event_key=event)
    return receipt, json.loads(encode(result_body))


def bootstrap_all(engine, *, capture_ref_id, execution_id, runtime_digest, archive_root,
                  datasets=None, minimum_free_bytes=10 * 1024**3):
    """Commit each table independently; retain errors and continue its peers."""
    from app.data_foundation.staged_inputs import verify_runtime
    selected = [item.dataset for item in TABLE_SOURCES] if datasets is None else list(dict.fromkeys(datasets))
    available = {item.dataset for item in TABLE_SOURCES}
    if not selected or any(native not in available for native in selected):
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地表差异核对包含未登记领域或空范围。')
    results = []
    for native in selected:
        try:
            with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection, connection.begin():
                with Session(bind=connection) as session:
                    verify_runtime(session, execution_id, runtime_digest, archive_root)
                    receipt, body = bootstrap_table(session, capture_ref_id=capture_ref_id,
                        native_dataset=native, execution_id=execution_id, archive_root=archive_root,
                        minimum_free_bytes=minimum_free_bytes)
                    results.append(dict(status='sealed', receipt_ref_id=str(receipt.id), **body))
        except Exception as exc:
            # A failed table transaction leaves no partial receipt or orphaned
            # bootstrap deltas. Other independent tables can still be fixed.
            results.append(dict(status='failed', dataset=native,
                error_code=getattr(exc, 'code', type(exc).__name__), error_type=type(exc).__name__))
    return dict(status='completed_with_issues' if any(row['status'] == 'failed' for row in results) else 'completed',
                tables=results, capture_ref_id=str(capture_ref_id))


def main():
    import app.models  # Register ORM dependencies before reading fixed input.
    from app.db.session import get_engine
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.data_foundation.canonical import encode
    from app.data_foundation.scope_settlement import TABLE_NAMES
    import structlog
    parser = argparse.ArgumentParser(description='按固定全量来源核对本地表变化，仅保留差异。')
    parser.add_argument('--capture-ref-id', required=True, type=UUID)
    parser.add_argument('--execution-id', required=True, type=UUID)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--archive-root', default='/app/data/foundation-runtime-archives')
    parser.add_argument('--dataset', action='append')
    parser.add_argument('--minimum-free-gib', type=int, default=10, choices=range(0, 10001))
    args = parser.parse_args()
    runtime = configure_logging(get_settings())
    try:
        outcome = bootstrap_all(get_engine(), capture_ref_id=args.capture_ref_id,
            execution_id=args.execution_id, runtime_digest=args.runtime,
            archive_root=args.archive_root, datasets=args.dataset,
            minimum_free_bytes=args.minimum_free_gib * 1024**3)
        print(encode(outcome), flush=True)
        for row in outcome['tables']:
            title = TABLE_NAMES[row['dataset']]
            if row['status'] == 'sealed':
                structlog.get_logger(__name__).info('foundation_table_bootstrap_sealed',
                    dataset=row['dataset'], result=row,
                    message=f"{title}本地表差异核对，观察日期 {row['snapshot_at'][:10]} 至 {row['snapshot_at'][:10]}，原有 {row['fixed_original_rows']} 条、现有 {row['snapshot_current_rows']} 条、未变 {row['unchanged']} 条、新增 {row['inserted']} 条、修订 {row['changed']} 条、删除 {row['deleted']} 条；来源差异检查点已推进，业务发布仍待正式凭据。")
            else:
                structlog.get_logger(__name__).error('foundation_table_bootstrap_failed',
                    dataset=row['dataset'], error_code=row['error_code'], error_type=row['error_type'],
                    message=f"{title}本地表差异核对，观察日期起止无法核实，失败 1 个领域；检查点未推进，其余领域继续。")
        if outcome['status'] != 'completed':
            raise SystemExit(2)
    finally:
        runtime.stop()


if __name__ == '__main__':
    main()
