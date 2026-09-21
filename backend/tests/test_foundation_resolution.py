"""Bounded signed contexts: no database, credentials or provider access."""
import pytest
from pydantic import ValidationError
from app.data_foundation.resolution import ResolutionCodec
from app.data_foundation.canonical import FoundationError
from app.data_foundation.service import PageRequest


def test_signature_expiry_rotation_and_kind():
    clock=[100]
    codec=ResolutionCodec('test-server-secret',clock=lambda:clock[0])
    token=codec.sign('resolution',{'expires_at':1000,'request':{'release':'fixed'}})
    assert codec.read(token,'resolution')['request']['release']=='fixed'
    for bad in [token+'x','x'*40000,token.split('.')[0]+'.invalid']:
        with pytest.raises(FoundationError):codec.read(bad,'resolution')
    with pytest.raises(FoundationError):codec.read(token,'cursor')
    with pytest.raises(FoundationError):ResolutionCodec('rotated').read(token,'resolution')
    clock[0]=1000
    with pytest.raises(FoundationError) as error:codec.read(token,'resolution')
    assert error.value.code=='RESOLUTION_EXPIRED'


def test_page_cannot_override_request():
    for extra in [{'fields':['close']},{'release':'latest'},{'page_size':1001},{'page_size':0}]:
        with pytest.raises(ValidationError):PageRequest(resolution_token='test',**extra)
