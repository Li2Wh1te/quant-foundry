"""D09 endpoints compose the sole authenticated market factory."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from app.data_store.errors import DataStoreError
from . import catalog
from .bindings import ETF_CATALOG
from .contracts import CalendarResult, InstrumentDetail, InstrumentPage, MarketOverview
from .errors import MarketError, http_error
from .router import _business_query, get_market_service
from .service import MarketReadService

router = APIRouter(prefix='/api/admin/market', tags=['admin-market'])


def _get(request, response, allowed, action):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, allowed)
        return action()
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None


@router.get('/instruments', response_model=InstrumentPage)
def instruments(request: Request, response: Response, catalog_key: str = ETF_CATALOG,
                q: Annotated[str, Query(max_length=128)] = '', exchange: str = '', list_status: str = '',
                limit: Annotated[int, Query(ge=1, le=200)] = 50,
                cursor: Annotated[str | None, Query(min_length=1, max_length=8192)] = None,
                service: MarketReadService = Depends(get_market_service)):
    return _get(request, response, {'catalog_key', 'q', 'exchange', 'list_status', 'limit', 'cursor'},
        lambda: catalog.instruments(service, catalog_key=catalog_key, q=q, exchange=exchange,
            list_status=list_status, limit=limit, cursor=cursor))


@router.get('/instruments/{instrument_key}', response_model=InstrumentDetail)
def instrument(request: Request, response: Response, instrument_key: str,
               service: MarketReadService = Depends(get_market_service)):
    return _get(request, response, set(), lambda: catalog.instrument(service, instrument_key))


@router.get('/overview', response_model=MarketOverview)
def overview(request: Request, response: Response, catalog_key: str = ETF_CATALOG,
             service: MarketReadService = Depends(get_market_service)):
    return _get(request, response, {'catalog_key'}, lambda: catalog.overview(service, catalog_key))


@router.get('/calendar', response_model=CalendarResult)
def calendar(request: Request, response: Response, exchange: str, start: date, end: date,
             service: MarketReadService = Depends(get_market_service)):
    return _get(request, response, {'exchange', 'start', 'end'},
        lambda: catalog.calendar(service, exchange=exchange, start=start, end=end))


@router.get('/calendar/{exchange}/{day}', response_model=CalendarResult)
def calendar_day(request: Request, response: Response, exchange: str, day: date,
                 service: MarketReadService = Depends(get_market_service)):
    return _get(request, response, set(), lambda: catalog.calendar(service, exchange=exchange, start=day, end=day))
