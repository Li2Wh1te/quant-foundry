"""One bounded failed-publication diagnostic; never a source response store."""

import re

from .contracts import ASSET_TYPES, CODE, CollectionError, exact_json


MESSAGE = '已有标的的资产类型发生冲突，需要核对原始数据。'
TARGETS = ('fund-etf', 'fund-otc')
MAX_BYTES = 512


def bounded_ticker_conflict(subject, context):
    """Rebuild scalar allowlisted fields at both the exception and log sink.

    The hash describes the already validated, assembled directory using the
    existing content_hash/exact_json contract, not a physical HTTP body or a
    committed observation. Unknown fields, names, raw rows and credentials
    cannot enter this additional diagnostic. Malformed identities are omitted
    instead of truncated into seemingly valid evidence.
    """
    if type(subject) is not str or subject not in TARGETS or type(context) is not dict:
        return None
    code = context.get('source_code')
    old, new = context.get('old_asset_type'), context.get('new_asset_type')
    snapshot, digest = context.get('provider_snapshot'), context.get('directory_content_hash')
    if (type(code) is not str or len(code) > 57 or CODE.fullmatch(code) is None
            or type(old) is not str or old not in ASSET_TYPES
            or type(new) is not str or new != subject or old == new
            or type(snapshot) is not int or not 0 <= snapshot < 2**63
            or type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
        return None
    safe = {'stage': 'publication_validation', 'interface': 'meta.tickers.list',
            'source_code': code, 'old_asset_type': old, 'new_asset_type': new,
            'provider_snapshot': snapshot, 'directory_content_hash': digest,
            'hash_basis': 'validated_directory_exact_json', 'publication_committed': False}
    return safe if len(exact_json(safe).encode('utf-8')) <= MAX_BYTES else None


class TickerAssetTypeConflict(CollectionError):
    """Keep the original error text and only an execution-local scalar proof."""

    def __init__(self, subject, *, source_code, old_asset_type, new_asset_type,
                 provider_snapshot, directory_content_hash):
        super().__init__(MESSAGE)
        self.diagnostic = bounded_ticker_conflict(subject, {
            'source_code': source_code, 'old_asset_type': old_asset_type,
            'new_asset_type': new_asset_type, 'provider_snapshot': provider_snapshot,
            'directory_content_hash': directory_content_hash,
        })


PAIR_STAGES = frozenset(('pair_head_admission', 'pair_membership_validation',
                         'pair_original_staging', 'pair_publication'))
PAIR_REASONS = frozenset(('scope_mismatch', 'revision_changed', 'snapshot_regression',
    'committed_head_unverifiable', 'original_hash_mismatch', 'incomplete_original',
    'context_budget_exceeded', 'snapshot_mismatch', 'membership_overlap',
    'unproven_category_change', 'publication_rejected'))


class DirectoryPublicationRejected(CollectionError):
    """Shared head-admission rejection, including ordinary single directories."""
    def __init__(self, reason, subject=None, **context):
        super().__init__('目录发布被安全校验拒绝。')
        self.reason, self.subject = reason, subject
        self.context = context


class PairPublicationRejected(DirectoryPublicationRejected):
    """Fixed pair stage and scalar context; never an error/payload dump."""
    def __init__(self, stage, reason, subject=None, **context):
        super().__init__(reason, subject, **context)
        self.stage = stage


def bounded_pair_rejection(entries, error, *, elapsed_ms=None):
    typed = isinstance(error, PairPublicationRejected)
    stage = error.stage if typed and error.stage in PAIR_STAGES else 'pair_publication'
    reason = error.reason if typed and error.reason in PAIR_REASONS else 'publication_rejected'
    safe = {'stage': stage, 'reason': reason, 'publication_committed': False, 'candidates': []}
    if typed and error.subject in TARGETS:
        safe['subject'] = error.subject
    if type(elapsed_ms) is int and 0 <= elapsed_ms < 2**63:
        safe['elapsed_ms'] = elapsed_ms
    for entry in entries[:2]:
        if entry.subject not in TARGETS:
            continue
        row = {'subject': entry.subject}
        if type(entry.revision) is int and 0 <= entry.revision < 2**63:
            row['expected_revision'] = entry.revision
        for key, value in [('directory_content_hash', entry.conflict.get('directory_content_hash')),
                           ('request_content_hash', entry.request_hash)]:
            if type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value):
                row[key] = value
        value = entry.conflict.get('provider_snapshot')
        if type(value) is int and 0 <= value < 2**63:
            row['provider_snapshot'] = value
        safe['candidates'].append(row)
    if typed:
        for key in ('current_revision', 'current_snapshot'):
            value = error.context.get(key)
            if type(value) is int and 0 <= value < 2**63:
                safe[key] = value
        for key in ('current_head_hash', 'actual_content_hash', 'actual_request_hash'):
            value = error.context.get(key)
            if type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value):
                safe[key] = value
        subject, diagnostic = error.subject, error.context.get('ticker_conflict')
        conflict = bounded_ticker_conflict(subject, diagnostic)
        if conflict:
            safe['ticker_conflict'] = conflict
    # At most two fixed-size candidate records and one existing 512-byte
    # conflict. Unknown provider messages, rows, URLs and credentials vanish.
    if len(exact_json(safe).encode()) <= 1792:
        return safe
    return {key: safe[key] for key in ('stage', 'reason', 'publication_committed', 'elapsed_ms') if key in safe}
