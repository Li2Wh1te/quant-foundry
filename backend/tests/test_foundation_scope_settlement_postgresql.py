"""Empty captures receive operational releases, never invented market facts."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from tests.test_foundation_record_updates_postgresql import campaign, observation
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.local_intake import register_campaign, capture_dataset
from app.data_foundation.intake import scan_page
from app.data_foundation.scope_settlement import freeze_empty_scan, DOMAIN
from app.data_foundation.full_formalization import run_campaign
from app.data_foundation.record_models import RecordSubject
from app.data_foundation import record_work
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.source_refs import read_source
from app.data_foundation.canonical import FoundationError


def test_empty_scan_receipt_is_published_readable_and_restartable(session,tmp_path):
    (_, _, execution), image=fixture(session,tmp_path)
    campaign_id=register_campaign(session,event_key=uuid4().hex,decoder_id=execution).id
    scan=capture_dataset(session,campaign_id,'anomaly_stock')
    scan_page(session,scan.id)
    ref=freeze_empty_scan(session,campaign_id=campaign_id,native_dataset='anomaly_stock',execution_id=execution)
    assert read_source(session,ref.id)[0]['fixed_rows']==0
    assert freeze_empty_scan(session,campaign_id=campaign_id,native_dataset='anomaly_stock',execution_id=execution).id==ref.id
    session.commit()
    options=dict(campaign_id=campaign_id,execution_id=execution,runtime_digest=image,
                 archive_root=tmp_path,datasets=['anomaly_stock'],publish=True,steps=100)
    result=run_campaign(session.bind,**options)
    assert result['status']=='completed'
    group=result['domains'][0]
    assert group['fixed_versions']==0 and group['scope_receipt']
    assert group['results']=={'published':1}
    subject=session.scalar(select(RecordSubject.id).where(RecordSubject.source=='foundation'))
    request=RecordRequirement(dataset_id=DOMAIN, semantic_series_id=record_work.series_for(DOMAIN, 'foundation'),
        subjects=[subject],fields=['fixed_rows','semantics'])
    actual=json.loads(service(session).query_official(request))
    assert actual['request_satisfied'] and actual['items'][0]['fixed_rows']==0, actual
    denied=json.loads(service(session).query_official(request.model_copy(update={'fields':['market_absence']})))
    assert not denied['request_satisfied'] and not denied['items']
    again=run_campaign(session.bind,**options)
    assert again['domains'][0]['scope_receipts_published']==1
    assert again['domains'][0]['results']=={}


def test_nonempty_and_missing_scans_cannot_be_settled_as_empty(session,tmp_path):
    (_, _, execution), _=fixture(session,tmp_path)
    observation(session)
    campaign_id=campaign(session,execution)
    with pytest.raises(FoundationError):
        freeze_empty_scan(session,campaign_id=campaign_id,native_dataset='fund_company',execution_id=execution)
    with pytest.raises(FoundationError):
        freeze_empty_scan(session,campaign_id=campaign_id,native_dataset='rank_trend',execution_id=execution)
