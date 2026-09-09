"""Authenticated read-only overview. Source probes are deliberately not run."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.overview.schemas import OperationsOverview
from app.overview.service import OverviewService
from app.scheduling.registry import task_registry


router = APIRouter(prefix="/api/admin", tags=["admin-overview"])


@router.get("/overview", response_model=OperationsOverview)
def read_overview(request: Request, response: Response,
                  session: Annotated[Session, Depends(get_db_session)]) -> OperationsOverview:
    # Expose only presence, never the credential or third-party endpoint. A
    # configured token is not evidence that the source is currently reachable.
    token = request.app.state.settings.tushare_token
    configured = token is not None and bool(token.get_secret_value().strip())
    response.headers["Cache-Control"] = "no-store"
    # This dependency supplies a fresh session. Pin all counts and bounded
    # previews to one PostgreSQL snapshot while scheduler workers keep writing.
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    return OverviewService(session, task_registry).read(tushare_configured=configured)
