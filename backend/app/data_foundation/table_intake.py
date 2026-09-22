"""Freeze all existing local Tushare tables into the shared immutable registry.

A single repeatable-read transaction fixes every table, including empty source
families and identity/correction evidence. Source rows stream in bounded chunks;
only the small chunk manifest is held in memory. All chunk references and the
manifest commit atomically. A crash cannot leave a supposedly complete capture
with missing pages, and retry of the event reads the original immutable capture.
No provider client, network fetch or source-table mutation is performed.
"""
import argparse
from dataclasses import dataclass
from decimal import Decimal
import json
import platform
from uuid import UUID

from sqlalchemy import select, func, cast, Text, text
from sqlalchemy.orm import Session

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import lock_key, register_dependencies
from app.data_foundation.execution import installed_code_hash
from app.data_foundation.models import Baseline, Execution, SourceRef, DependencyEntry
from app.data_foundation.source_refs import register_baseline, read_source
from app.data_ingestion.models.etf import EtfCode, EtfCodeMappingAudit
from app.data_ingestion.models.etf_daily import EtfDailyBar, EtfDailyBarRevisionAudit
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.trading_calendar import (TradingCalendarDay, TradingStatusSourceFact,
    TradingStatusFact, TradingStatusCoverageFact, TradingStatusFactRevisionAudit)
from app.data_ingestion.models.corporate_action import (CorporateActionSourceFact,
    CorporateActionFact, CorporateActionCoverageFact)


@dataclass(frozen=True)
class TableSource:
    family: str
    dataset: str
    model: type
    source_column: str | None = 'source'
    date_column: str | None = None


TABLE_SOURCES = (
    TableSource('trade_calendar', 'exchange_calendar', TradingCalendarDay, None, 'calendar_date'),
    TableSource('etf_basic', 'etf_directory', EtfCode),
    TableSource('etf_basic', 'etf_code_mapping_audits', EtfCodeMappingAudit),
    TableSource('fund_daily', 'etf_daily', EtfDailyBar, date_column='trade_date'),
    TableSource('fund_daily', 'etf_daily_revision_audits', EtfDailyBarRevisionAudit, date_column='trade_date'),
    TableSource('fund_adj', 'etf_adjustment_factors', EtfAdjustmentFactor, date_column='trade_date'),
    TableSource('fund_div', 'corporate_action_source_facts', CorporateActionSourceFact, date_column='ann_date'),
    TableSource('fund_div', 'corporate_action_facts', CorporateActionFact, date_column='ex_date'),
    TableSource('fund_div', 'corporate_action_coverage_facts', CorporateActionCoverageFact, date_column='start_date'),
    TableSource('suspend_d', 'trading_status_source_facts', TradingStatusSourceFact),
    TableSource('suspend_d', 'trading_status_facts', TradingStatusFact, date_column='trade_date'),
    TableSource('suspend_d', 'trading_status_coverage_facts', TradingStatusCoverageFact, date_column='start_date'),
    TableSource('suspend_d', 'trading_status_revision_audits', TradingStatusFactRevisionAudit, 'previous_source', 'trade_date'),
)
MANIFEST_DATASET = 'full_local_capture_manifest'


def verify_capture(session, reference):
    body = read_source(session, reference.id)
    if not isinstance(body, list) or len(body) != 1 or body[0].get('format') != 'local-tables-v1':
        raise FoundationError('SOURCE_INVALID', '全量本地表捕获清单格式无效。')
    document = body[0]
    entries = session.scalars(select(DependencyEntry).where(
        DependencyEntry.manifest_id == UUID(document['dependency_id']))).all()
    expected = {item['source_ref_id']: item['hash'] for group in document['tables'] for item in group['chunks']}
    actual = {str(entry.source_ref_id): entry.content_hash for entry in entries}
    if len(actual) != len(entries) or actual != expected:
        raise FoundationError('SOURCE_INVALID', '全量捕获的来源引用与保留依赖不一致。')
    for group in document['tables']:
        count = 0
        for ordinal, item in enumerate(group['chunks']):
            ref = session.get(SourceRef, UUID(item['source_ref_id']))
            rows = read_source(session, ref.id)
            if ref.dataset != group['dataset'] or ref.content_hash != item['hash'] or len(rows) != item['rows'] or item['ordinal'] != ordinal:
                raise FoundationError('SOURCE_INVALID', '全量捕获分块内容或顺序不一致。')
            count += len(rows)
        if not group['chunks'] or count != group['rows']:
            raise FoundationError('SOURCE_INVALID', '全量捕获分块行数不能覆盖原表。')
    return document


def capture_tables(session, *, event_key, decoder_id, chunk_size=2000, sources=TABLE_SOURCES):
    """The caller owns one repeatable-read transaction until the manifest seals."""
    if session.bind.dialect.name != 'postgresql' or session.connection().get_isolation_level() != 'REPEATABLE READ':
        raise FoundationError('SNAPSHOT_REQUIRED', '全量表捕获需要 PostgreSQL 可重复读事务。')
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 128 or not 1 <= chunk_size <= 5000:
        raise ValueError('Invalid capture identity or chunk size')
    execution = session.get(Execution, decoder_id)
    manifest = json.loads(execution.manifest_json) if execution else {}
    if manifest.get('parser', {}).get('hash') != installed_code_hash() or manifest.get('python_version') != platform.python_version():
        raise FoundationError('DEPENDENCY_MISSING', '全量本地表捕获的固定解码执行与当前代码不一致。')
    lock_key(session, 'full-local-table-capture', event_key)
    old = session.scalar(select(SourceRef).join(Baseline, Baseline.id == SourceRef.baseline_id).where(
        Baseline.source == 'tushare', Baseline.dataset == MANIFEST_DATASET, Baseline.event_key == event_key))
    if old:
        result = verify_capture(session, old)
        if old.decoder_id != decoder_id or result['chunk_size'] != chunk_size or [g['dataset'] for g in result['tables']] != [s.dataset for s in sources]:
            raise FoundationError('INPUT_CHANGED', '同一全量捕获事件不能替换解码版本、表清单或分块参数。')
        return old, result
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    session.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
    observed = session.scalar(text('SELECT transaction_timestamp()'))
    snapshot = session.scalar(text('SELECT pg_current_snapshot()::text'))
    tables, dependencies = [], []
    for spec in sources:
        table = spec.model.__table__
        # PostgreSQL renders NUMERIC and JSON values directly into text. Decode
        # JSON numbers as Decimal so no intermediate binary float corrupts the
        # retained source evidence (including numbers nested in JSON columns).
        statement = select(cast(func.row_to_json(table.table_valued()), Text)).order_by(*table.primary_key.columns)
        if spec.source_column:
            statement = statement.where(table.c[spec.source_column] == 'tushare')
        result = session.execute(statement.execution_options(stream_results=True, yield_per=chunk_size))
        chunks, total, start, end = [], 0, None, None
        def store(rows):
            ordinal = len(chunks)
            scope = dict(capture_event=event_key, snapshot=snapshot, table=table.name, family=spec.family,
                ordinal=ordinal, primary_key=[c.name for c in table.primary_key.columns],
                first_key={c.name: rows[0][c.name] for c in table.primary_key.columns} if rows else None,
                last_key={c.name: rows[-1][c.name] for c in table.primary_key.columns} if rows else None)
            ref = register_baseline(session, source='tushare', dataset=spec.dataset, scope=scope, rows=rows,
                observed_at=observed, decoder_id=decoder_id, event_key=digest('table-capture-part', [event_key, spec.dataset, ordinal]))
            chunks.append(dict(ordinal=ordinal, source_ref_id=str(ref.id), hash=ref.content_hash, rows=len(rows)))
            dependencies.append(dict(source_ref_id=ref.id, purpose='fixed_local_table_chunk'))
        for partition in result.partitions(chunk_size):
            rows = [json.loads(row[0], parse_float=Decimal) for row in partition]
            store(rows)
            total += len(rows)
            for row in rows:
                value = row.get(spec.date_column) if spec.date_column else None
                if value is not None:
                    start = value if start is None else min(start, value)
                    end = value if end is None else max(end, value)
        result.close()
        if not chunks:
            store([])
        tables.append(dict(family=spec.family, dataset=spec.dataset, table=table.name,
            source_filter={spec.source_column: 'tushare'} if spec.source_column else None, rows=total, start=start, end=end, chunks=chunks))
    dependency = register_dependencies(session, dependencies)
    document = dict(format='local-tables-v1', event_key=event_key, snapshot=snapshot,
        observed_at=observed, chunk_size=chunk_size, dependency_id=str(dependency.id), tables=tables,
        limitation='current_local_values_only_prior_mutable_values_not_reconstructed')
    reference = register_baseline(session, source='tushare', dataset=MANIFEST_DATASET,
        scope={'event_key': event_key, 'families': sorted({s.family for s in sources})}, rows=[document],
        observed_at=observed, decoder_id=decoder_id, event_key=event_key)
    return reference, document


def main():
    from app.db.session import get_engine
    parser = argparse.ArgumentParser(description='固定全部已有本地 Tushare 表及身份/修订证据，不连接供应商。')
    parser.add_argument('--event-key', required=True)
    parser.add_argument('--decoder-id', required=True, type=UUID)
    parser.add_argument('--chunk-size', type=int, default=2000)
    args = parser.parse_args()
    with get_engine().connect().execution_options(isolation_level='REPEATABLE READ') as connection, connection.begin():
        with Session(bind=connection) as session:
            reference, document = capture_tables(session, event_key=args.event_key,
                decoder_id=args.decoder_id, chunk_size=args.chunk_size)
            response = dict(capture_ref_id=reference.id, manifest_hash=reference.content_hash,
                observed_at=document['observed_at'], tables=[{k:v for k,v in group.items() if k != 'chunks'} for group in document['tables']],
                message='全部已有本地 Tushare 表已按同一数据库快照固定，空表已明确记录；来源固定不等于业务正式发布。')
    print(encode(response), flush=True)


if __name__ == '__main__':
    main()
