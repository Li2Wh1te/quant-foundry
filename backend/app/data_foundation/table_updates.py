"""Bind one atomic local table change to immutable normalization input."""
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, select, text

from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.models import Baseline, SourceRef
from app.data_foundation.source_refs import read_source, register_baseline
from app.data_foundation.table_intake import TABLE_SOURCES
from app.data_foundation.table_bootstrap import stable_content
from app.data_foundation.update_models import TableChange

FORMAT = 'local-table-change-v1'


def spec_for(native):
    spec = next((item for item in TABLE_SOURCES if item.dataset == native), None)
    if spec is None:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地表变化没有登记来源表。')
    return spec


def freeze_change(session, *, change_id, execution_id):
    change = session.get(TableChange, change_id)
    if change is None:
        raise FoundationError('SOURCE_UNAVAILABLE', '本地表变化不存在。')
    spec_for(change.dataset)
    before = json.loads(change.before_json, parse_float=Decimal) if change.before_json else None
    after = json.loads(change.after_json, parse_float=Decimal) if change.after_json else None
    if not isinstance(before or after, dict):
        raise FoundationError('SOURCE_INVALID', '本地表变化缺少可验证的行内容。')
    operation = 'delete' if after is None else 'insert' if before is None else 'update'
    scope = dict(kind=FORMAT, change_id=change.id, operation=operation,
                 key_json=json.loads(change.key_json))
    return register_baseline(session, source='tushare', dataset=change.dataset,
        scope=scope, rows=[after if after is not None else before],
        observed_at=change.observed_at, decoder_id=execution_id,
        event_key=digest('local-table-change-source-v1', change.id))


def change_context(session, source, change_id):
    if source.representation != 'local_table_baseline' or source.source != 'tushare':
        raise FoundationError('SCOPE_MISMATCH', '本地表变化指针需要固定表来源。')
    baseline = session.get(Baseline, source.baseline_id)
    scope = json.loads(baseline.scope_json)
    if scope.get('kind') != FORMAT or scope.get('change_id') != change_id:
        raise FoundationError('SOURCE_CONTEXT_CHANGED', '固定表来源与变化编号不一致。')
    change = session.get(TableChange, change_id)
    if change is None or change.dataset != source.dataset:
        raise FoundationError('SOURCE_CONTEXT_CHANGED', '固定表变化来源与领域不一致。')
    expected = json.loads(change.after_json or change.before_json, parse_float=Decimal)
    if read_source(session, source.id) != [json.loads(encode(expected))]:
        raise FoundationError('SOURCE_MUTATED', '固定表变化内容与来源引用不一致。')
    return change, scope


def is_deleted_change(session, source):
    if source.source != 'tushare' or source.representation != 'local_table_baseline':
        return False
    scope = json.loads(session.get(Baseline, source.baseline_id).scope_json)
    return scope.get('kind') == FORMAT and scope.get('operation') == 'delete'


def is_current_change(session, source, change_id):
    """Lock the same primary-key advisory gate as source ingestion.

    The trigger takes this lock before changing a row; publication takes it
    before checking the source value, so an absent-row check is also fenced.
    """
    change, scope = change_context(session, source, change_id)
    spec = spec_for(change.dataset)
    table = spec.model.__table__
    key = json.loads(change.key_json)
    if set(key) != {column.name for column in table.primary_key.columns}:
        raise FoundationError('SOURCE_INVALID', '本地表变化主键不完整。')
    typed_key = {}
    for column in table.primary_key.columns:
        value = key[column.name]
        target = column.type.python_type
        if target is date and isinstance(value, str):
            value = date.fromisoformat(value)
        elif target is datetime and isinstance(value, str):
            value = datetime.fromisoformat(value)
        elif target is UUID and isinstance(value, str):
            value = UUID(value)
        typed_key[column.name] = value
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:table_name || '|' || CAST(:key AS jsonb)::text, 0))"),
                    dict(table_name=table.name, key=encode(key)))
    # A writer may already hold the row lock while waiting for our advisory
    # gate in its BEFORE trigger. A plain MVCC read observes the prior committed
    # value, making publication occur before that new write without deadlock.
    current = session.scalar(select(spec.model).where(and_(*[
        table.c[name] == value for name, value in typed_key.items()]))
        .execution_options(populate_existing=True))
    if current is not None and spec.source_column and getattr(current, spec.source_column) != 'tushare':
        current = None
    if change.after_json is None:
        return current is None
    if current is None:
        return False
    from sqlalchemy import cast, func, Text
    row_text = session.scalar(select(cast(func.row_to_json(table.table_valued()), Text)).where(and_(*[
        table.c[name] == value for name, value in typed_key.items()])))
    actual = json.loads(row_text, parse_float=Decimal)
    fixed = json.loads(change.after_json, parse_float=Decimal)
    return digest('local-table-stable-row-v1', stable_content(actual)) == digest(
        'local-table-stable-row-v1', stable_content(fixed))
