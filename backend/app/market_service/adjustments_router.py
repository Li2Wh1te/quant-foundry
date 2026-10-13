"""Authenticated D11 domain routes, composed with the sole market factory."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from app.data_store.errors import DataStoreError
from .adjustments import factors
from .contracts import FactorPage
from .errors import MarketError, http_error
from .router import _business_query, get_market_service
from .service import MarketReadService

router = APIRouter()


@router.get('/instruments/{instrument_key}/factors', response_model=FactorPage)
def get_factors(request: Request, response: Response, instrument_key: str,
                start: date, end: date, series_key: Annotated[str, Query(min_length=1, max_length=64)]='etf-daily-local',
                as_of: date | None=None,
                limit: Annotated[int, Query(ge=1, le=2000)]=512,
                cursor: Annotated[str | None, Query(max_length=8192)]=None,
                service: MarketReadService=Depends(get_market_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        _business_query(request, {'series_key', 'start', 'end', 'as_of', 'limit', 'cursor'})
        return factors(service, instrument_key, series_key=series_key, start=start, end=end,
            as_of=as_of, limit=limit, cursor=cursor)
    except (MarketError, DataStoreError, SQLAlchemyError) as error:
        raise http_error(error) from None
