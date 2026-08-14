from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.db.session import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings if settings is not None else get_settings()
    app = FastAPI(
        title=current_settings.app_name,
        debug=current_settings.debug,
        lifespan=lifespan,
    )

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"message": "Hello World"}

    return app


app = create_app()
