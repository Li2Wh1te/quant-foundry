"""Editor concurrency and request validation without a database."""
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.router import _http_error, _usage_owner
from app.backtesting.schemas import AccountProfileUpdateRequest
from app.backtesting.service import AccountProfileService, AccountProfileVersionConflictError
from app.core.auth import AuthenticatedPrincipal


@pytest.mark.parametrize("payload", [
    {}, {"expected_version": 1}, {"name": "x", "expected_version": 0},
    {"name": "x", "expected_version": True}, {"name": "x", "expected_version": "1"},
    {"name": "x", "expected_version": None},
])
def test_invalid_editor_tokens(payload):
    with pytest.raises(ValidationError):
        AccountProfileUpdateRequest.model_validate(payload)


def test_stale_editor_never_mutates_or_appends_history():
    session = Mock()
    service = AccountProfileService(session)
    service.repository = Mock()
    record = BacktestAccountProfileRecord(id=uuid4(), name="original", version=2)
    service.repository.get.return_value = record
    with pytest.raises(AccountProfileVersionConflictError) as error:
        service.update(record.id, expected_version=1, name="stale")
    assert _http_error(error.value).status_code == 409
    assert record.name == "original" and record.version == 2
    session.add.assert_not_called()
    session.flush.assert_not_called()
    service.repository.get.assert_called_once_with(record.id, for_update=True)


def test_owner_is_authenticated_identity_not_header():
    request = Request({"type": "http", "headers": [(b"x-owner-scope", b"other")],
                       "state": {"authenticated_principal": AuthenticatedPrincipal("actual")}})
    assert _usage_owner(request) == "actual"
    assert AccountProfileUpdateRequest(name="legacy").expected_version is None
