"""Hash-reviewed, explicit report operations over already captured local data.

The operator supplies durable identities and documentary evidence in a manifest.
Review does not write; apply refuses a changed manifest, source or mapper. This
entry point never calls a provider or substitutes current collection state.
"""
import argparse
import json
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.catalog import now, register_dependencies
from app.data_foundation.contracts import register_holdings_catalog
from app.data_foundation.holdings import DATASET, SERIES, ResolvedIdentity, normalize_report
from app.data_foundation.holding_work import DOMAIN_KEY, domain_hash, create_governance, plan_actions, report_input
from app.data_foundation.identity import register_report_binding
from app.data_foundation.source_refs import read_source, register_baseline
from app.data_foundation.models import SourceRef, Definition
from app.data_foundation.work import create_work
from app.data_foundation.work_models import Work, CandidateManifest, Head, IssueScope


class ReviewedIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_code: str = Field(min_length=1, max_length=64)
    member_code: str = Field(min_length=1, max_length=48)
    asset_class: Literal['fund_share', 'stock']
    instrument_id: UUID
    valid_from: date
    valid_to: date
    directory_evidence: dict
    references: list[dict] = Field(min_length=1)

    @model_validator(mode='after')
    def evidence_matches(self):
        expected_type = 'fund-otc' if self.asset_class == 'fund_share' else 'a-share'
        if (self.valid_from >= self.valid_to or self.directory_evidence.get('thscode') != self.source_code
                or self.directory_evidence.get('asset_type') != expected_type):
            raise ValueError('身份与固定来源目录类型或代码不匹配')
        if self.asset_class == 'stock' and not self.directory_evidence.get('exchange'):
            raise ValueError('股票身份需要明确的市场证据')
        if (self.asset_class == 'stock' and self.source_code != self.member_code + '.' + self.directory_evidence['exchange']
                or self.asset_class == 'fund_share' and self.member_code != self.source_code):
            raise ValueError('报告成员命名空间与来源完整代码不匹配')
        for item in self.references:
            if (set(item) != {'url', 'sha256', 'location', 'finding'} or not item['url'].startswith('https://')
                    or len(item['sha256']) != 64 or any(c not in '0123456789abcdef' for c in item['sha256'])
                    or not item['location'] or not item['finding']):
                raise ValueError('历史身份证据需要原始文件摘要、位置和核验结论')
        return self


class ReportReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    source_ref_id: UUID
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    subject: str
    report_keys: list[str] = Field(min_length=1, max_length=100)
    start: date
    end: date
    reviewer: str = Field(min_length=1, max_length=128)
    binding_version: str = Field(min_length=1, max_length=64)
    identities: list[ReviewedIdentity] = Field(default_factory=list, max_length=2000)

    @model_validator(mode='after')
    def scope_matches(self):
        if self.start > self.end or len(set(self.report_keys)) != len(self.report_keys):
            raise ValueError('报告范围或选择器重复')
        keys = [(i.asset_class, i.member_code) for i in self.identities]
        if len(set(keys)) != len(keys):
            raise ValueError('同次审核不能包含歧义身份')
        return self


def review(session, manifest):
    ref = session.get(SourceRef, manifest.source_ref_id)
    if (ref is None or ref.source != 'tonghuashun' or ref.dataset != 'fund_stock_history'
            or ref.content_hash != manifest.source_hash):
        raise FoundationError('SOURCE_CHANGED', '固定持仓来源与审核清单不一致。')
    subject, container, receipts = report_input(session, ref)
    if subject != manifest.subject:
        raise FoundationError('SOURCE_CHANGED', '报告来源主体与审核清单不一致。')
    def resolve(code, kind, start, end):
        matches = [i for i in manifest.identities if i.member_code == code and i.asset_class == kind
                   and i.valid_from <= start <= end < i.valid_to]
        if len(matches) != 1:
            return None
        item = matches[0]
        # Dry-run IDs are deliberately labelled by the manifest; no binding is
        # persisted until apply. Admission hash includes the actual mapper hash.
        return ResolvedIdentity(item.instrument_id, item.instrument_id, item.valid_from, item.valid_to, now())
    reports = []
    for key in manifest.report_keys:
        result = normalize_report(subject=subject, selected_key=key, container=container,
                                  resolve_identity=resolve, receipt_requests=receipts)
        head = result['header']
        if head and not manifest.start <= head['period_end'] <= manifest.end:
            raise FoundationError('SCOPE_MISMATCH', '选中报告超出审核日期范围。')
        reports.append({'source_report_key': key, 'readiness': result['readiness'], 'reasons': result['reasons'],
            'member_count': head['member_count'] if head else None, 'transport_complete': result['transport_complete'],
            'portfolio_complete': result['portfolio_complete']})
    payload = manifest.model_dump(mode='json')
    review_hash = digest('report-admission-review-v1', {'manifest': payload, 'domain_hash': domain_hash(), 'reports': reports})
    return {'review_hash': review_hash, 'reports': reports, 'source_ref_id': ref.id, 'source_hash': ref.content_hash,
        'domain_hash': domain_hash(), 'identity_count': len(manifest.identities), 'public_pit_supported': False}


def apply(session, manifest, *, expected_hash, execution_id):
    checked = review(session, manifest)
    if checked['review_hash'] != expected_hash:
        raise FoundationError('REVIEW_CHANGED', '报告审核摘要已改变，未创建工作。')
    contract, policy = register_holdings_catalog(session)
    rows = [i.model_dump(mode='json') for i in manifest.identities]
    directory_scope = {'reviewer': manifest.reviewer, 'binding_version': manifest.binding_version}
    directory = register_baseline(session, source='tonghuashun', dataset='report_identity_directory',
        scope=directory_scope, rows=rows, observed_at=now(), decoder_id=execution_id,
        event_key='report-identity:' + digest('reviewed-report-identities', {'scope': directory_scope, 'rows': rows}))
    bindings = []
    for identity in manifest.identities:
        binding = register_report_binding(session, source_ref_id=directory.id, source_code=identity.source_code,
            member_code=identity.member_code, asset_class=identity.asset_class, instrument_id=identity.instrument_id,
            valid_from=identity.valid_from, valid_to=identity.valid_to, binding_version=manifest.binding_version,
            evidence={'reviewer': manifest.reviewer, 'reference': identity.references,
                'valid_from': str(identity.valid_from), 'valid_to': str(identity.valid_to),
                'source_code': identity.source_code, 'asset_class': identity.asset_class})
        bindings.append(binding)
    deps = register_dependencies(session, [{'binding_id': b.id, 'purpose': 'reviewed_report_identity'} for b in bindings]
        + [{'source_ref_id': directory.id, 'purpose': 'fixed_directory_and_document_review'},
           {'source_ref_id': manifest.source_ref_id, 'purpose': 'fixed_report_object'},
           {'execution_id': execution_id, 'purpose': 'report_normalization'}])
    work = create_work(session, kind='A', contract_id=contract.id, execution_id=execution_id,
        dependency_id=deps.id, source_ref_id=manifest.source_ref_id, parameters={
            'dataset': DATASET, 'major': 1, 'profile': 'default', 'series': SERIES,
            'start': str(manifest.start), 'end': str(manifest.end), 'domain': DOMAIN_KEY,
            'domain_hash': domain_hash(), 'report_keys': manifest.report_keys}, total=len(manifest.report_keys))
    return {'work_id': work.id, 'policy_id': policy.id, 'review': checked, 'binding_ids': [b.id for b in bindings]}


def governance_review(session, work_id, policy_id):
    work = session.get(Work, work_id)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == work_id))
    policy = session.get(Definition, policy_id)
    if work is None or manifest is None or policy is None:
        raise FoundationError('CANDIDATE_NOT_SEALED', '报告候选或治理政策不存在。')
    head = session.get(Head, work.scope_key)
    issue = session.get(IssueScope, work.scope_key)
    parent = head.release_id if head else None
    actions = plan_actions(session, manifest.id, policy_id, parent)
    payload = {'manifest_hash': manifest.manifest_hash, 'policy_hash': policy.content_hash,
        'parent_release_id': parent, 'head_revision': head.revision if head else 0,
        'issue_epoch': issue.epoch, 'actions': actions}
    return {**payload, 'review_hash': digest('report-governance-review-v1', payload)}


def main():
    from app.db.session import get_engine
    parser = argparse.ArgumentParser(description='显式审核和处理固定的基金报告，不启动供应商采集。')
    parser.add_argument('command', choices=['review', 'normalize', 'governance-review', 'governance'])
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--expected-hash')
    parser.add_argument('--execution-id', type=UUID)
    parser.add_argument('--work-id', type=UUID)
    parser.add_argument('--policy-id', type=UUID)
    args = parser.parse_args()
    if args.command in ('normalize', 'governance') and (not args.execution_id or not args.expected_hash):
        parser.error('--execution-id and --expected-hash are required for writes')
    with Session(get_engine()) as session, session.begin():
        if args.command in ('review', 'normalize'):
            if args.manifest is None:
                parser.error('--manifest is required')
            manifest = ReportReview.model_validate_json(args.manifest.read_text())
            result = review(session, manifest) if args.command == 'review' else apply(session, manifest,
                expected_hash=args.expected_hash, execution_id=args.execution_id)
        else:
            if not args.work_id or not args.policy_id:
                parser.error('--work-id and --policy-id are required')
            result = governance_review(session, args.work_id, args.policy_id)
            if args.command == 'governance':
                if result['review_hash'] != args.expected_hash or not args.execution_id:
                    raise FoundationError('REVIEW_CHANGED', '报告治理审核摘要或执行版本缺失。')
                work = create_governance(session, normalization_id=args.work_id, policy_id=args.policy_id,
                    execution_id=args.execution_id, parent_release_id=result['parent_release_id'],
                    expected_head_revision=result['head_revision'], expected_issue_epoch=result['issue_epoch'])
                result = {'work_id': work.id, 'review_hash': args.expected_hash}
        if args.command in ('review', 'governance-review'):
            session.rollback()
    print(encode(result))


if __name__ == '__main__':
    try:
        main()
    except FoundationError as exc:
        print(encode({'status': 'failed', 'code': exc.code, 'message': str(exc)}))
        raise SystemExit(1)
