"""Typed empty-scope receipts backed by sealed local capture evidence.

Zero captured rows must not become a zero market value or an assertion that no
market events occurred. The receipt has its own operational series and links
to the fixed scan/table identity. Existing nonempty business sources continue
through their ordinary adapters; this module cannot settle them as empty.
"""
from uuid import UUID
from sqlalchemy import select, func
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.catalog import register_dependencies
from app.data_foundation.source_refs import register_baseline, read_source
from app.data_foundation.models import SourceRef
from app.data_foundation.intake_models import CampaignScan, Scan, ScanSeal, ScanEntry, ScanTarget, ScanFailure

DATASET = 'empty_local_scope'
DOMAIN = 'operations.empty_local_scope'
TABLE_NAMES = {
    'exchange_calendar': '交易日历', 'etf_directory': 'ETF标的目录',
    'etf_code_mapping_audits': 'ETF代码映射修订', 'etf_daily': 'ETF日线',
    'etf_daily_revision_audits': 'ETF日线修订', 'etf_adjustment_factors': 'ETF复权因子',
    'corporate_action_source_facts': '公司行动来源事实', 'corporate_action_facts': '公司行动事实',
    'corporate_action_coverage_facts': '公司行动覆盖范围', 'trading_status_source_facts': '交易状态来源事实',
    'trading_status_facts': '交易状态事实', 'trading_status_coverage_facts': '交易状态覆盖范围',
    'trading_status_revision_audits': '交易状态修订',
}


def freeze_receipt(session, *, provider, native, scope_id, capture_id, captured_at,
                   evidence_basis, execution_id, dependencies=()):
    # Register typed dependencies before the receipt so retained table evidence
    # remains available even if mutable source tables subsequently change.
    dependency = register_dependencies(session, [dict(source_ref_id=ref, purpose='empty_scope_evidence') for ref in dependencies])
    body = dict(provider=provider, native_dataset=native, scope_identity=str(scope_id),
                capture_identity=str(capture_id), fixed_rows=0, evidence_basis=evidence_basis,
                captured_at=captured_at, semantics='no_rows_in_fixed_local_capture',
                market_absence=None, market_coverage_complete=None)
    return register_baseline(session, source='foundation', dataset=DATASET,
        scope=dict(provider=provider, dataset=native, capture_identity=str(capture_id),
                   scope_identity=str(scope_id), dependency_id=str(dependency.id)), rows=[body],
        observed_at=captured_at, decoder_id=execution_id,
        event_key=digest('empty-local-scope-v1', [provider, native, scope_id, capture_id]))


def freeze_empty_scan(session, *, campaign_id, native_dataset, execution_id):
    link = session.get(CampaignScan, (campaign_id, native_dataset))
    scan = session.get(Scan, link.scan_id) if link else None
    seal = session.get(ScanSeal, link.scan_id) if link else None
    if scan is None or seal is None or scan.status != 'completed' or scan.mode != 'frozen' or seal.target_count != 0 or scan.completed_at is None:
        raise FoundationError('CAPTURE_NOT_EMPTY', '空范围结算需要已封存并完整完成的零来源扫描。')
    for model in (ScanTarget, ScanEntry, ScanFailure):
        if session.scalar(select(func.count()).select_from(model).where(model.scan_id == scan.id)):
            raise FoundationError('CAPTURE_NOT_EMPTY', '扫描存在来源或错误，不能按空范围结算。')
    return freeze_receipt(session, provider='tonghuashun', native=native_dataset,
        scope_id=scan.id, capture_id=campaign_id, captured_at=scan.completed_at,
        evidence_basis='sealed_empty_scan', execution_id=execution_id)


def freeze_empty_tables(session, *, capture_ref_id, execution_id):
    from app.data_foundation.table_intake import verify_capture, MANIFEST_DATASET
    capture = session.get(SourceRef, capture_ref_id)
    if capture is None or capture.source != 'tushare' or capture.dataset != MANIFEST_DATASET:
        raise FoundationError('SOURCE_INVALID', '空表结算缺少明确的固定本地表捕获。')
    document = verify_capture(session, capture)
    receipts = []
    for group in document['tables']:
        if group['rows'] != 0:
            continue
        refs = [UUID(item['source_ref_id']) for item in group['chunks']]
        if not refs or any(read_source(session, ref) != [] for ref in refs):
            raise FoundationError('CAPTURE_NOT_EMPTY', '固定表分块不为空，不能创建空范围结算。')
        receipt = freeze_receipt(session, provider='tushare', native=group['dataset'],
            scope_id=group['table'], capture_id=capture.id, captured_at=capture.observed_at,
            evidence_basis='fixed_empty_table', execution_id=execution_id, dependencies=[capture.id, *refs])
        receipts.append((group['dataset'], receipt))
    return receipts


def settlement_body(raw):
    from app.data_foundation.record_schemas import EmptyLocalScope
    body = EmptyLocalScope.model_validate(raw).model_dump(mode='json')
    key = digest('empty-scope-subject', [body['provider'], body['native_dataset'], body['scope_identity'], body['capture_identity']])
    return body, key, dict(market_absence='LOCAL_EMPTY_CAPTURE_ONLY', market_coverage_complete='LOCAL_EMPTY_CAPTURE_ONLY')
