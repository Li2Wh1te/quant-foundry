"""Authenticated management endpoints; neither credentials nor vendor errors leak."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.data_sources.providers import PROVIDERS, SourceError
from app.data_sources.service import DataSourceService
from app.db.session import get_db_session
from app.scheduling.registry import task_registry


class SafeSourceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default validation response includes rejected input,
                # which can be a whole form containing a credential.
                response = JSONResponse(status_code=422, content={"detail": {
                    "message": "请求格式不正确，请检查配置字段和版本。"}})
            except SourceError as exc:
                response = JSONResponse(status_code=exc.status_code,
                    content={"detail": {"message": str(exc), "field": exc.field}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return safe_handler


router = APIRouter(prefix="/api/data-sources", tags=["data-sources"], route_class=SafeSourceRoute)


class ConfigDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    fields: dict[str, Any]


class SourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    enabled: bool = Field(strict=True)


def service(request: Request, session: Session = Depends(get_db_session)) -> DataSourceService:
    return DataSourceService(session, request.app.state.settings, task_registry)


@router.get("")
def list_sources(svc: DataSourceService = Depends(service)):
    return {"items": [svc.detail(key) for key in PROVIDERS]}


@router.get("/{key}")
def read_source(key: str, svc: DataSourceService = Depends(service)):
    return svc.detail(key)


@router.post("/{key}/test")
def test_source(key: str, payload: ConfigDraft, svc: DataSourceService = Depends(service)):
    return svc.test(key, payload.version, payload.fields)


@router.put("/{key}/config")
def save_source(key: str, payload: ConfigDraft, svc: DataSourceService = Depends(service)):
    return svc.save(key, payload.version, payload.fields)


@router.put("/{key}/state")
def set_source_state(key: str, payload: SourceState, svc: DataSourceService = Depends(service)):
    return svc.set_enabled(key, payload.version, payload.enabled)
