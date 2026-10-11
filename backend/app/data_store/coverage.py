"""Current full-range evidence shared by readiness and read-only audit.

The proof is a compact receipt of an exhausted, sealed source scan and its
ordered partition cursor. It is never reconstructed from current output files
or from the latest operation's success flag. One current receipt is retained;
the bounded continuation owns unfinished input values, not a history ledger.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from .adapters.contracts import digest
from .schema import DatasetSpec

CAPTURED_RANGE_ENTRIES = frozenset({'E23', 'E44', 'E50', 'E51', 'E52', 'E69', 'E70'})


def check_coverage(connection, entry, status, *, require_all_quality=True):
    """Judge current input evidence; full readiness also requires all quality.

    A bounded object read may delegate quality to the kernel's existing
    request-scope gate. Reset completion and audit retain the default whole
    entry judgment. This never substitutes output presence for input proof.
    """
    status = status or {}
    proof = status.get('full_coverage') or {}
    reason = None
    if (proof.get('version') != 1 or proof.get('entry_id') != entry.id or
            not proof.get('boundary') or not proof.get('range_digest')):
        reason = 'FULL_RANGE_UNPROVEN'
    elif proof.get('contract') != digest(entry.spec.descriptor()):
        reason = 'FULL_RANGE_CONTRACT_CHANGED'
    elif (not proof.get('scan_complete') or not proof.get('complete') or
          proof.get('remaining') != 0 or proof.get('after') != proof.get('end')):
        reason = 'FULL_RANGE_PENDING'
    elif status.get('coverage_pending'):
        reason = 'CURRENT_INPUT_PENDING'
    if reason is None and entry.id in CAPTURED_RANGE_ENTRIES:
        if connection.execute(text("SELECT to_regclass('data_store_source_ranges') IS NOT NULL")).scalar_one():
            if connection.execute(text('SELECT EXISTS(SELECT 1 FROM data_store_source_ranges WHERE source=:s AND dataset=:d)'),
                                  {'s':entry.source,'d':entry.native}).scalar_one():
                reason='CURRENT_INPUT_PENDING'
    row = connection.execute(text(
        'SELECT generation,descriptor_json FROM data_store_datasets WHERE name=:d'),
        {'d': entry.spec.name}).mappings().first()
    if reason is None:
        if row is None or digest(json.loads(row['descriptor_json'])) != proof['contract']:
            reason = 'FULL_RANGE_CONTRACT_CHANGED'
        elif row['generation'] != proof.get('generation'):
            reason = 'FULL_RANGE_GENERATION_CHANGED'
        elif any(not entry.spec.accepts(DatasetSpec.from_descriptor(json.loads(contract)))
                 for (contract,) in connection.execute(text(
                     'SELECT DISTINCT contract_json FROM data_store_files WHERE dataset=:d'),
                     {'d': entry.spec.name})):
            reason = 'FULL_RANGE_CONTRACT_CHANGED'
        elif require_all_quality and (connection.execute(text(
                'SELECT EXISTS(SELECT 1 FROM data_store_issues WHERE dataset=:d)'),
                {'d': entry.spec.name}).scalar_one() or status.get(
                    'overflow_restriction', {}).get('blocking_objects')):
            reason = 'CURRENT_QUALITY_UNRESOLVED'
    return {'satisfied': reason is None, 'reason': reason, 'evidence': proof,
            'last_complete_evidence': status.get('last_complete_coverage'),
            'pending_operations': status.get('coverage_pending', [])}
