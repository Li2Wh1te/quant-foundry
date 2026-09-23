"""Real releases retain whole activity sets and preserve their earlier versions."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_market_activity import member,ladder
from app.data_foundation.market_activity import DOMAINS
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.models import Definition
from app.data_foundation.work_models import Work,Candidate


@pytest.mark.parametrize('native',['auction_benchmark','limit_up','limit_down','limit_break'])
def test_dated_activity_replacement_empty_publication_and_date_contract(session,native):
    create=lambda rows:setup(session,rows,native,'2026-09-15',requested_start='2026-09-15',requested_end='2026-09-15')
    first=release_records(session,create([member(tags=[],auction_pct='-1',last_price='10')]))
    second=release_records(session,create([]),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key==native+':2026-09-15'))
    domain=DOMAINS[native]
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[ident],fields=['members','trading_date'],business_range={'from':'2026-09-15','to':'2026-09-15'})
    for release,count in [(first,1),(second,0)]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and len(result['items'])==1
        assert len(result['items'][0]['members'])==count
    contract=session.get(Definition,session.get(Work,second.work_id).contract_id)
    assert json.loads(contract.definition_json)['time_semantics']['business']==['trading_date']
    for change in [dict(fields=['complete_market_coverage']),dict(time_mode='strict_public_pit')]:
        denied=json.loads(service(session).query_official(req.model_copy(update=change)))
        assert not denied['request_satisfied'] and not denied['items']


def test_final_auction_exact_values_are_readable_without_execution_semantics(session):
    native='stock_auction';domain=DOMAINS[native]
    create=lambda value:setup(session,[member(auction_unmatched=value)],native,'A.SH',auction_phase='closed',data_status='final')
    first=release_records(session,create('-1.25'));second=release_records(session,create('0'),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='A.SH'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[ident],fields=['reported_auction_unmatched'])
    for release,value in [(first,'-1.25'),(second,'0')]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and result['items'][0]['reported_auction_unmatched']==value
    denied=json.loads(service(session).query_official(req.model_copy(update={'fields':['executable_quote']})))
    assert not denied['request_satisfied'] and not denied['items']


def test_anomaly_replacement_never_promotes_source_narratives_to_verified_facts(session):
    native='anomaly_list';domain=DOMAINS[native]
    row=dict(thscode='A.SH',stock_name='Name',tag_name='tag',keyword_list=[],analysis_content='source narrative')
    first=release_records(session,setup(session,[row,row],native,'market'))
    second=release_records(session,setup(session,[],native,'market'),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='anomaly_list:market'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',subjects=[ident],fields=['members'])
    for release,count in [(first,2),(second,0)]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and len(result['items'][0]['members'])==count
    denied=json.loads(service(session).query_official(req.model_copy(update={'fields':['factual_verification']})))
    assert not denied['request_satisfied'] and not denied['items']


def test_ladder_window_replaces_removed_dates_and_retains_reported_limits(session):
    native='limit_ladder';domain=DOMAINS[native];days,metadata=ladder()
    first=release_records(session,setup(session,days,native,'market',**metadata))
    metadata['window'].update(length=1,date_list=['2026-09-15'])
    second=release_records(session,setup(session,days[:1],native,'market',**metadata),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='limit_ladder:market'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',subjects=[ident],fields=['days','declared_board_caps'])
    for release,count in [(first,2),(second,1)]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and len(result['items'][0]['days'])==count
        assert result['items'][0]['declared_board_caps']['two_board']==2


def test_invalid_child_quarantines_whole_limit_list(session):
    work,_,_=setup(session,[member(last_price='10'),member(thscode='B.SH',last_price='NaN')],
        'limit_up','2026-09-15')
    candidates=session.scalars(select(Candidate).where(Candidate.work_id==work.id)).all()
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'
