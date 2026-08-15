from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
    )


def get_db_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()
