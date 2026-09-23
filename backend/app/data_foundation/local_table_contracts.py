"""Strict source-local contracts for Tushare fact and audit tables.

Native columns are classified by their declared SQL type. Scalar evidence is
published exactly under its named source column; nested JSON is retained only
as a digest with the immutable raw source available through lineage. These
contracts do not infer a market-effective event, public PIT, ETF economic
identity or complete market coverage from the ingestion store.
"""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, Numeric, String, Text, Uuid

from app.data_foundation.canonical import FoundationError, digest, normalized
from app.data_foundation.table_intake import TABLE_SOURCES

DOMAINS = {
    'etf_code_mapping_audits': 'operations.etf_mapping_audit',
    'etf_daily_revision_audits': 'operations.etf_daily_revision',
    'corporate_action_source_facts': 'operations.corporate_action_source_fact',
    'corporate_action_facts': 'operations.corporate_action_fact',
    'corporate_action_coverage_facts': 'operations.corporate_action_coverage',
    'trading_status_source_facts': 'operations.trading_status_source_fact',
    'trading_status_facts': 'operations.trading_status_fact',
    'trading_status_coverage_facts': 'operations.trading_status_coverage',
    'trading_status_revision_audits': 'operations.trading_status_revision',
}


def table_body(source, raw):
    spec = next((item for item in TABLE_SOURCES if item.dataset == source.dataset), None)
    if spec is None or source.dataset not in DOMAINS or not isinstance(raw, dict):
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地事实或审计来源未安装明确契约。')
    table = spec.model.__table__
    columns = {column.name: column for column in table.columns}
    if set(raw) != set(columns):
        raise FoundationError('SOURCE_SCHEMA_INVALID', '本地事实或审计行与明确表字段不一致。')
    if spec.source_column and raw[spec.source_column] != 'tushare':
        raise FoundationError('IDENTITY_CONFLICT', '本地事实或审计行来源命名空间不一致。')
    key = {column.name: raw[column.name] for column in table.primary_key.columns}
    if any(value is None for value in key.values()):
        raise FoundationError('IDENTITY_UNRESOLVED', '本地事实或审计行缺少来源主键。')
    fields = dict(text={}, numbers={}, dates={}, times={}, identifiers={}, flags={}, nested_hashes={})
    for name, column in columns.items():
        value = raw[name]
        category = column.type
        if value is None:
            if not column.nullable and name not in ('created_at', 'updated_at'):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地事实或审计行必填字段缺失。')
            if isinstance(category, (JSON,)):
                fields['nested_hashes'][name] = None
            elif isinstance(category, (DateTime,)):
                fields['times'][name] = None
            elif isinstance(category, (Date,)):
                fields['dates'][name] = None
            elif isinstance(category, (Numeric,)):
                fields['numbers'][name] = None
            elif isinstance(category, (Boolean,)):
                fields['flags'][name] = None
            elif isinstance(category, (Uuid,)):
                fields['identifiers'][name] = None
            elif isinstance(category, (Integer,)):
                fields['numbers'][name] = None
            else:
                fields['text'][name] = None
            continue
        if isinstance(category, JSON):
            if not isinstance(value, (dict, list)):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地嵌套证据缺少可核验对象或列表。')
            fields['nested_hashes'][name] = digest('source-local-nested-column-v1', value)
        elif isinstance(category, DateTime):
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(parsed, datetime) or parsed.tzinfo is None:
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地时间字段缺少明确时区。')
            fields['times'][name] = parsed
        elif isinstance(category, Date):
            parsed = date.fromisoformat(value) if isinstance(value, str) else value
            if type(parsed) is not date:
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地日期字段无效。')
            fields['dates'][name] = parsed
        elif isinstance(category, Boolean):
            if type(value) is not bool:
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地布尔字段无效。')
            fields['flags'][name] = value
        elif isinstance(category, Uuid):
            try:
                fields['identifiers'][name] = str(UUID(str(value)))
            except ValueError:
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地标识字段无效。') from None
        elif isinstance(category, (Numeric, Integer)):
            if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地数值字段缺少精确十进制表示。')
            number = Decimal(value)
            if not number.is_finite() or isinstance(category, Integer) and number != number.to_integral_value():
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地数值字段无效。')
            fields['numbers'][name] = normalized(number)
        elif isinstance(category, (String, Text)):
            if not isinstance(value, str):
                raise FoundationError('SOURCE_SCHEMA_INVALID', '本地来源文本字段无效。')
            fields['text'][name] = value
        else:
            raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '本地事实或审计表含未声明字段类型。')
    identity = digest('source-local-table-key-v1', [source.dataset, key])
    body = dict(native_dataset=source.dataset, native_table=table.name,
        source_identity=identity, source_row_hash=digest('source-local-table-row-v1', raw),
        **{f'reported_{name}': value for name, value in fields.items()},
        nested_semantics='source_hash_only', verified_public_time=None,
        verified_market_completeness=None)
    quality = dict(verified_public_time='HISTORICAL_PUBLIC_TIME_UNVERIFIED',
                   verified_market_completeness='LOCAL_SOURCE_ROW_ONLY')
    if fields['nested_hashes']:
        quality['reported_nested_hashes'] = 'RAW_NESTED_EVIDENCE_AVAILABLE_BY_SOURCE_LINEAGE'
    return body, quality, identity
