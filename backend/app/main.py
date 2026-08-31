from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session
import structlog

from app.core.auth import require_api_token
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.request_logging import log_request
from app.data_ingestion.router import router as data_ingestion_router
from app.backtesting.router import router as backtesting_router
from app.backtesting.run_router import router as backtest_run_router, internal_router as internal_backtest_run_router
from app.backtesting.result_router import (
    router as backtest_result_router,
    legacy_router as backtest_result_legacy_router,
)
from app.core.version import get_release_version
from app.db.session import dispose_engine, get_db_session
from app.logging.router import router as log_router
from app.scheduling.router import router as scheduling_router
from app.scheduling.runtime import SchedulerRuntime
from app.strategies.router import router as strategies_router


logger = structlog.get_logger(__name__)


def check_database_ready(session: Session) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging_runtime = configure_logging(app.state.settings)
    scheduler_runtime = SchedulerRuntime(app.state.settings)
    app.state.scheduler_runtime = scheduler_runtime
    try:
        scheduler_runtime.start()
        logger.info("application_started")
        yield
    finally:
        logger.info("application_stopped")
        scheduler_runtime.stop()
        dispose_engine()
        logging_runtime.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings if settings is not None else get_settings()
    app = FastAPI(
        title=current_settings.app_name,
        version=get_release_version(),
        debug=current_settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = current_settings
    app.middleware("http")(log_request)

    protected_router = APIRouter(dependencies=[Depends(require_api_token)])
    protected_router.include_router(log_router)
    protected_router.include_router(scheduling_router)
    protected_router.include_router(data_ingestion_router)
    protected_router.include_router(strategies_router)
    protected_router.include_router(backtesting_router)
    protected_router.include_router(backtest_run_router)
    protected_router.include_router(internal_backtest_run_router)
    protected_router.include_router(backtest_result_router)
    protected_router.include_router(backtest_result_legacy_router)

    @protected_router.get(
        "/api/auth/verify",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def verify_api_token() -> Response:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @protected_router.get("/api")
    def read_root() -> dict[str, str]:
        return {"message": "Hello World"}

    @protected_router.get("/api/system/version")
    def read_system_version() -> dict[str, str]:
        return {"version": app.version}

    app.include_router(protected_router)

    @app.get("/readyz", include_in_schema=False)
    def read_ready(
        session: Session = Depends(get_db_session),
    ) -> dict[str, str]:
        return check_database_ready(session)

    return app


app = create_app()
