"""Publish typed importer receipts separately from their business contents.

An empty control item list describes no market observations. Imported counts
prove only the original importer's progress; typed business publication needs
its own release receipts. Pending batch-budget entries remain visible even
when this operational observation itself is successfully published.
"""
from app.data_foundation.canonical import FoundationError
from app.data_foundation.record_schemas import ImportProgress

CHANNELS = ('stock_daily_dump', 'stock_recent_dump', 'stock_actions_dump')


def progress_body(source, raw):
    admitted = {'item', 'artifact', 'failed_requests', 'imported_subjects',
                'superseded_subjects', 'total_subjects', 'requested_start', 'requested_end'}
    if set(raw) != admitted or raw['item'] != [] or source.dataset not in CHANNELS:
        raise FoundationError('SOURCE_SCHEMA_INVALID', '批量导入控制观察字段不完整或混入业务记录。')
    body = ImportProgress.model_validate(dict(
        import_channel=source.dataset, source_scope=source.subject,
        artifact=raw['artifact'], pending_requests=raw['failed_requests'],
        imported_subjects=raw['imported_subjects'], superseded_subjects=raw['superseded_subjects'],
        total_subjects=raw['total_subjects'], requested_start=raw['requested_start'],
        requested_end=raw['requested_end'], semantics='local_import_progress_only',
        business_publication_complete=None)).model_dump(mode='json')
    return body, {'business_publication_complete': 'REQUIRES_SEPARATE_BUSINESS_RELEASE_RECEIPTS'}
