"""Public error-envelope compatibility, independent of private proof sources.

This boundary test uses the real serializer and FastAPI exception handler.
Injected public error metadata is not evidence of native request admission.
The separately audited fixed-R01 nine-test source remains unchanged.
"""
from fastapi import FastAPI
import pytest
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.data_store import router
from app.data_store.errors import DataStoreError
from tests.test_data_store_api import ASGIClient


RESTRICTED_MESSAGE = '当前数据存在未解决的问题，未返回可用数据。'


def wire_error(operation):
    app = FastAPI()

    @app.get('/denial')
    def denial():
        try:
            operation()
        except (DataStoreError, SQLAlchemyError) as error:
            raise router._http_error(error) from error

    return ASGIClient(app).get('/denial')


def refuse(reason=None):
    error = DataStoreError('DATA_RESTRICTED')
    if reason is not None:
        error.restriction_reason = reason
    raise error


@pytest.mark.parametrize('reason', ['REQUEST_EVIDENCE_UNPROVEN', 'CURRENT_INPUT_PENDING'])
def test_supplied_restriction_reason_keeps_complete_original_error_envelope(reason):
    response = wire_error(lambda: refuse(reason))
    assert response.status_code == 409
    assert response.json() == {'detail': {
        'code': 'DATA_RESTRICTED', 'message': RESTRICTED_MESSAGE, 'reason': reason}}


@pytest.mark.parametrize('reason', [None, ''])
def test_absent_reason_keeps_original_two_field_envelope(reason):
    response = wire_error(lambda: refuse(reason))
    assert response.status_code == 409
    assert response.json() == {'detail': {
        'code': 'DATA_RESTRICTED', 'message': RESTRICTED_MESSAGE}}


@pytest.mark.parametrize('state,status,code,message', [
    ('55P03', 503, 'LOCK_TIMEOUT', '数据正在提交或处理，请稍后重试。'),
    ('57014', 422, 'QUERY_TIMEOUT', '操作达到时间上限，未返回不完整结果。'),
    ('08006', 503, 'CATALOG_UNAVAILABLE', '当前目录暂不可访问。'),
    (None, 503, 'CATALOG_UNAVAILABLE', '当前目录暂不可访问。'),
])
def test_sql_error_normalization_keeps_public_envelope_without_database_details(
        state, status, code, message):
    def fail():
        if state is None:
            raise SQLAlchemyError('private database payload')
        original = RuntimeError('private database payload')
        original.sqlstate = state
        raise DBAPIError('private SQL statement', {'password': 'private credential'}, original)

    response = wire_error(fail)
    assert response.status_code == status
    assert response.json() == {'detail': {'code': code, 'message': message}}
