"""Operator boundary validation happens before any database or source access."""
import pytest

from app.data_store.__main__ import main
from app.data_store.pipeline import PipelineOptions


@pytest.mark.parametrize('args',[
    ['update','--resume-sealed-only'],
    ['update','--entry','E44','--resume-sealed-only','--expect-input-identity','0'*64],
    ['update','--expect-input-identity','0'*64],
    ['update','--max-claim-batches','0'],
    ['update','--max-partition-passes','4097'],
    ['describe','--max-claim-batches','1'],
    ['update','--entry','E44','--resume-sealed-only','--max-claim-batches','2',
     '--expect-input-identity','0'*64,'--expect-source-selection','1'*64],
    ['update','--entry','E44','--entry','E50','--resume-sealed-only',
     '--expect-input-identity','0'*64,'--expect-source-selection','1'*64],
    ['retry','--entry','E44','--resume-sealed-only',
     '--expect-input-identity','0'*64,'--expect-source-selection','1'*64],
    ['update','--entry','E71','--resume-sealed-only',
     '--expect-input-identity','0'*64,'--expect-source-selection','1'*64],
])
def test_invalid_operator_boundaries_refused(args):
    with pytest.raises(SystemExit) as error:main(args)
    assert error.value.code==2


@pytest.mark.parametrize('kwargs',[
    {'maximum_claim_batches':True}, {'maximum_partition_passes':0},
    {'expected_source_selection':'1'*64}, {'resume_sealed_only':True},
    {'resume_sealed_only':True,'expected_input_identity':'0'*64,
     'expected_source_selection':'1'*64,'partitions':('2017-03.b07',)},
])
def test_direct_callers_cannot_bypass_option_validation(kwargs):
    with pytest.raises(ValueError):PipelineOptions(**kwargs)
