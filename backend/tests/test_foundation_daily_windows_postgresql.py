"""Actual governance replaces a whole reported price window, retaining history."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_daily_windows import point
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


@pytest.mark.parametrize('native,adjust,domain',[
 ('stock_daily','none','market.stock_daily_window'),
 ('etf_daily','forward','market.etf_daily_window'),
 ('index_daily','not_applicable','market.index_daily_window')])
def test_window_replacement_historical_reads_and_financial_unit_gates(session,native,adjust,domain):
    create=lambda rows:setup(session,rows,native,'A.SH',adjust=adjust,coverage='observed_rows_only',
        requested_start='2016-01-01',requested_end='2026-09-22')
    first=release_records(session,create([point('2026-09-01'),point('2026-09-02')]))
    second=release_records(session,create([point('2026-09-02',close_price='10.5')]),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='A.SH'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',subjects=[ident],fields=['points','sequence_start','sequence_end'])
    for release,days in [(first,['2026-09-01','2026-09-02']),(second,['2026-09-02'])]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied']
        body=result['items'][0]
        assert [p['trade_date'] for p in body['points']]==days
        assert body['sequence_start']==days[0] and body['sequence_end']==days[-1]
    for changes in [{'fields':['currency']},{'fields':['volume_unit']},{'fields':['adjustment_anchor']},{'time_mode':'strict_public_pit'}]:
        denied=json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not denied['request_satisfied'] and not denied['items']


def test_bad_candle_is_not_dropped_from_the_published_window(session):
    work,_,_=setup(session,[point(),point('2026-09-02',high_price='1')],'stock_daily','A.SH',adjust='none',coverage='observed_rows_only')
    candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'
