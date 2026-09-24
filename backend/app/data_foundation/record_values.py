"""Stable typed-record value and partition semantics shared with validation."""
from app.data_foundation.canonical import FoundationError, digest

VALUE_FIELDS = ('subject_id', 'business_key', 'business_date', 'schema_key', 'body_json', 'field_quality_json')
MEMBER_FIELDS = ('target_key', 'subject_id', 'business_date', 'state', 'official_id', 'decision_id')


def fields(row, names=VALUE_FIELDS):
    return {name: getattr(row, name) for name in names}


def value_hash(row):
    return digest('typed-record-value-v1', fields(row))


def subject_partitioned(dataset, partitions):
    # Existing hash-partitioned lineages must retain their original layout.
    if not partitions:
        return dataset in ('market.adjustment_factor', 'market.fund_daily')
    flags = [key.startswith('subject:') for key in partitions]
    if any(flags) and (not all(flags) or dataset not in ('market.adjustment_factor', 'market.fund_daily')):
        raise FoundationError('MANIFEST_INVALID', '领域发布分块方式不一致。')
    return all(flags)


def partition_key(key, subject_id, by_subject):
    return 'subject:' + str(subject_id) if by_subject else key[:2]
