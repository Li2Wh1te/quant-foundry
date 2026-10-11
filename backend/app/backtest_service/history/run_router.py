"""Retain legacy read URLs without importing the old queue or executor."""
from fastapi import APIRouter, Depends, Query, Request
from ..router import service, owner, fault
from ..service import RunService
from ..runtime_contracts import RuntimeFault
from .router import _cursor_signing_key
from ..listing import list_page
from .runs import read_run

router = APIRouter(prefix="/api/admin/backtest-runs", tags=["legacy-history"])


@router.get("")
@router.get("/workspace")
def history_page(request: Request, s: RunService = Depends(service), limit: int = Query(100, ge=1, le=500), cursor: str | None = None):
    try:
        return list_page(s.repository, owner(request), limit, cursor, _cursor_signing_key(request))
    except RuntimeFault as error:
        raise fault(error) from None


@router.get("/{run_id}")
def history_status(run_id: str, request: Request, s: RunService = Depends(service)):
    try:
        return read_run(s.repository, owner(request), run_id)
    except RuntimeFault as error:
        raise fault(error) from None
