from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session
import structlog

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.request_logging import log_request
from app.db.session import dispose_engine, get_db_session
from app.logging.router import router as log_router


logger = structlog.get_logger(__name__)


def check_database_ready(session: Session) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging_runtime = configure_logging(app.state.settings)
    logger.info("application_started")
    try:
        yield
    finally:
        logger.info("application_stopped")
        dispose_engine()
        logging_runtime.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings if settings is not None else get_settings()
    app = FastAPI(
        title=current_settings.app_name,
        debug=current_settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = current_settings
    app.middleware("http")(log_request)
    app.include_router(log_router)

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"message": "Hello World"}

    @app.get("/readyz", include_in_schema=False)
    def read_ready(
        session: Session = Depends(get_db_session),
    ) -> dict[str, str]:
        return check_database_ready(session)

    return app


app = create_app()
