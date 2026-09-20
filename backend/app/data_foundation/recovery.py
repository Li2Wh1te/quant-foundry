"""Snapshot evidence and a fail-closed gate for isolated database recovery.

The exporter owns a repeatable-read transaction until the host finishes pg_dump.
Only digests and immutable dependency locators leave stdout; secrets and source
payloads never enter the backup manifest. Operator errors are Chinese summaries.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.data_foundation.canonical import FoundationError, encode
from app.data_foundation.work_models import RuntimeArchive, Head, Release


def ensure_serving(session):
    if session.scalar(text("SELECT current_setting('qf.foundation_recovery_pending', true)")) == 'on':
        raise FoundationError('RECOVERY_REVIEW_REQUIRED', '恢复数据尚未完成当前问题与权限复核，数据底座暂不提供读取。')


def snapshot_evidence(session):
    """Hash persisted foundation rows in stable primary-key order, incrementally.

    Table and column names come only from the installed SQLAlchemy metadata.
    JSON is rendered by the same PostgreSQL major version with UTC timestamps;
    restored values must compare exactly, including Decimal and reference IDs.
    """
    import app.models  # Register every mapped foundation table before hashing.
    from app.db.base import Base
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    tables = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        if not table.name.startswith('foundation_'):
            continue
        order = ','.join('"'+column.name+'"' for column in table.primary_key.columns)
        if not order:
            raise FoundationError('RECOVERY_SCHEMA_INVALID', '恢复核对表缺少稳定主键。')
        digest = hashlib.sha256()
        count = 0
        statement = text(f'SELECT row_to_json(t)::text FROM "{table.name}" t ORDER BY {order}')
        for row in session.execute(statement.execution_options(stream_results=True, yield_per=200)):
            digest.update(row[0].encode()); digest.update(b'\n'); count += 1
        tables.append(dict(name=table.name, rows=count, sha256=digest.hexdigest()))
    from app.data_foundation.models import SourceRef
    from app.data_foundation.source_refs import read_source
    sources = list(session.scalars(select(SourceRef).order_by(SourceRef.id)))
    for source in sources:
        read_source(session, source.id)
    archives = [dict(key=a.archive_key, sha256=a.archive_hash, bytes=a.byte_count,
        image_digest=a.image_digest) for a in session.scalars(select(RuntimeArchive).order_by(RuntimeArchive.id))]
    heads = [dict(scope=h.scope_key, release_id=h.release_id, revision=h.revision,
        manifest_hash=session.get(Release,h.release_id).manifest_hash) for h in session.scalars(select(Head).order_by(Head.scope_key))]
    return dict(schema=session.scalar(text('SELECT version_num FROM alembic_version')),
        postgres=session.scalar(text('SHOW server_version')), tables=tables, heads=heads,
        verified_sources=len(sources), archives=archives)


def main():
    parser = argparse.ArgumentParser(description='导出一致备份证据或验证隔离恢复库；不调用供应商。')
    parser.add_argument('command', choices=['export', 'verify'])
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    from app.db.session import get_engine
    with get_engine().connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            with Session(bind=connection) as session:
                if args.command == 'export':
                    snapshot = session.scalar(text('SELECT pg_export_snapshot()'))
                    evidence = snapshot_evidence(session)
                    print(encode(dict(snapshot=snapshot,evidence=evidence)), flush=True)
                    # A closed pipe aborts the snapshot, never confirms a backup.
                    if sys.stdin.readline().strip() != 'complete':
                        raise FoundationError('BACKUP_ABORTED', '备份协调进程未确认完成，一致快照已关闭。')
                else:
                    if not args.evidence:
                        parser.error('--evidence is required')
                    expected = json.loads(args.evidence.read_text())
                    actual = snapshot_evidence(session)
                    # Patch versions may differ, but major versions must match.
                    version = actual.pop('postgres').split('.')[0]
                    expected = dict(expected)
                    expected_version = expected.pop('postgres').split('.')[0]
                    if version != expected_version or encode(actual) != encode(expected):
                        raise FoundationError('RESTORE_MISMATCH', '恢复后的正式值、来源或依赖摘要与备份不一致。')
                    print(encode(dict(status='verified',message='恢复库的底座数据与依赖核对通过，仍需当前问题和权限复核。')))


if __name__ == '__main__':
    try:
        main()
    except FoundationError as exc:
        print(encode(dict(status='failed',code=exc.code,message=str(exc))),file=sys.stderr)
        raise SystemExit(1)
