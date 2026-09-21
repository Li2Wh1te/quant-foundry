"""Applicability unions preserve unknown holes under ordering and grouping."""
from itertools import permutations
import pytest
from app.data_foundation.coverage import merge_coverage, covers_request
from app.data_foundation.canonical import FoundationError


def part(days):
    return dict(status='pass',start=min(days),end=max(days),subjects=[dict(instrument_id='I1',code='ETF',exchange='SSE')],
        expected_keys=[dict(instrument_id='I1',trade_date=d) for d in days])


def test_nested_coverage_is_order_and_group_invariant_without_filling_holes():
    parts=[part(['2026-09-01','2026-09-02']),part(['2026-09-07','2026-09-08']),part(['2026-09-03','2026-09-04'])]
    expected=merge_coverage(parts)
    for a,b,c in permutations(parts):
        for inputs in ([a,b,c],[merge_coverage([a,b]),c],[c,merge_coverage([a,b])]):
            result=merge_coverage(inputs)
            assert result['expected_keys']==expected['expected_keys']
            assert result['subject_intervals']==expected['subject_intervals']
            assert not covers_request(result,{'I1'},'2026-09-01','2026-09-08')
            assert covers_request(result,{'I1'},'2026-09-01','2026-09-04')


def test_actual_overlapping_calendar_conflict_is_still_rejected():
    a=part(['2026-09-01','2026-09-02']);b=part(['2026-09-01']);b['end']='2026-09-02'
    for inputs in ([a,b],[b,a]):
        with pytest.raises(FoundationError) as error:merge_coverage(inputs)
        assert error.value.code=='COVERAGE_CONFLICT'
