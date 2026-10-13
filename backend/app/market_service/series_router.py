"""D10 routes at the existing authenticated market router seam."""
from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, Query as Parameter, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from app.data_store.errors import DataStoreError
from .bindings import ETF_DAILY
from .contracts import Adjustment, BarPage, Period, SeriesOption
from .errors import MarketError, http_error
from .router import _business_query, get_market_service
from .service import MarketReadService
from .series import bars, series

router = APIRouter()

@router.get('/instruments/{instrument_key}/series', response_model=list[SeriesOption])
def get_series(request: Request, response: Response, instrument_key: str,
               service: MarketReadService = Depends(get_market_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, set())
        return series(service, instrument_key)
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None


@router.get('/instruments/{instrument_key}/bars', response_model=BarPage)
def get_bars(request: Request, response: Response, instrument_key: str,
             series_key: Annotated[str, Parameter(min_length=1, max_length=64)]=ETF_DAILY,
             period: Period='day', adjustment: Adjustment='none', start: date | None=None,
             end: date | None=None, as_of: date | None=None,
             limit: Annotated[int, Parameter(ge=1, le=2000)]=512,
             cursor: Annotated[str | None, Parameter(max_length=8192)]=None,
             service: MarketReadService = Depends(get_market_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, {'series_key', 'period', 'adjustment', 'start', 'end', 'as_of', 'limit', 'cursor'})
        return bars(service, instrument_key, series_key=series_key, period=period, adjustment=adjustment,
                    start=start, end=end, as_of=as_of, limit=limit, cursor=cursor)
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None
