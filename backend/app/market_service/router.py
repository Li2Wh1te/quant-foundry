"""Authenticated default D08 routes; later modules register their real routes."""
from threading import Lock
from typing import Annotated

from anyio import from_thread
from anyio.lowlevel import current_token
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.data_store.errors import DataStoreError
from .contracts import AssetType, CapabilitiesResult, InstrumentResolution
from .errors import MarketError, http_error
from .service import MarketReadService, create_market_service

router = APIRouter(prefix='/api/admin/market', tags=['admin-market'])


def _business_query(request: Request, allowed: set[str]):
    params = request.query_params.multi_items()
    if any(name not in allowed for name, _value in params) or len(params) != len({name for name, _value in params}):
        raise DataStoreError('INVALID_VALUE')


def get_market_service(request: Request, session: Session = Depends(get_db_session)) -> MarketReadService:
    # Same cancellation bridge as the existing CurrentStore route, also safe
    # when the kernel invokes it from its DuckDB watchdog thread.
    token = from_thread.run_sync(current_token)
    disconnect_lock = Lock()

    def cancelled():
        with disconnect_lock:
            return from_thread.run(request.is_disconnected, token=token)

    return create_market_service(request.app.state.settings, session.get_bind(),
        owner_scope=request.state.owner_scope, cancelled=cancelled)


@router.get('/capabilities', response_model=CapabilitiesResult)
def capabilities(request: Request, response: Response, service: MarketReadService = Depends(get_market_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, set())
        return service.capabilities()
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None


@router.get('/instruments/resolve', response_model=InstrumentResolution)
def resolve(request: Request, response: Response, symbol: Annotated[str, Query(min_length=1, max_length=64)],
            asset_type: AssetType = 'etf', service: MarketReadService = Depends(get_market_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, {'symbol', 'asset_type'})
        return service.resolve(symbol, asset_type)
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None

# D10 domain endpoints compose the sole authenticated market router/factory.
from .series_router import router as series_router
router.include_router(series_router)
from .adjustments_router import router as adjustments_router
router.include_router(adjustments_router)
