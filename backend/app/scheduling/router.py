from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import task_registry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.runtime import SchedulerDisabledError, SchedulerRuntime
from app.scheduling.schemas import (
    TaskCreate,
    TaskResponse,
    TaskRunResponse,
    TaskState,
    TaskTypeResponse,
    TaskUpdate,
)
from app.scheduling.service import (
    InvalidTaskParametersError,
    SchedulerService,
    TaskConflictError,
    TaskNotFoundError,
    UnknownTaskTypeError,
)


router = APIRouter(prefix="/api/admin", tags=["admin-scheduler"])


def get_scheduler_runtime(request: Request) -> SchedulerRuntime:
    return request.app.state.scheduler_runtime


@router.get("/task-types", response_model=list[TaskTypeResponse])
def list_task_types() -> list[TaskTypeResponse]:
    return [
        TaskTypeResponse(
            key=definition.key,
            name=definition.name,
            english_name=definition.english_name,
            parameter_version=definition.parameter_version,
            parameter_schema=definition.parameters_model.model_json_schema(),
        )
        for definition in task_registry.list()
    ]


@router.get("/tasks", response_model=list[TaskResponse])
def list_tasks(
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskResponse]:
    repository = SchedulerRepository(session)
    tasks = repository.list_tasks(
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    latest_runs = repository.list_latest_runs_for_tasks(
        [task.id for task in tasks]
    )
    return [
        _task_response(task, runtime, latest_run=latest_runs.get(task.id))
        for task in tasks
    ]


@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskResponse:
    try:
        task = SchedulerService(session, task_registry).create_task(payload)
        session.commit()
        session.refresh(task)
        runtime.sync_task(task.id)
        return _task_response(task, runtime)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskResponse:
    task = SchedulerRepository(session).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(task, runtime)


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskResponse:
    try:
        task = SchedulerService(session, task_registry).update_task(task_id, payload)
        session.commit()
        session.refresh(task)
        runtime.sync_task(task.id)
        return _task_response(task, runtime)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/pause", response_model=TaskResponse)
def pause_task(
    task_id: UUID,
    version: Annotated[int, Query(ge=1)],
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskResponse:
    return _change_state(task_id, version, TaskState.PAUSED, session, runtime)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
def resume_task(
    task_id: UUID,
    version: Annotated[int, Query(ge=1)],
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskResponse:
    return _change_state(task_id, version, TaskState.ACTIVE, session, runtime)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_task(
    task_id: UUID,
    version: Annotated[int, Query(ge=1)],
    session: Annotated[Session, Depends(get_db_session)],
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> Response:
    try:
        task = SchedulerService(session, task_registry).archive_task(
            task_id, expected_version=version
        )
        session.commit()
        runtime.sync_task(task.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/tasks/{task_id}/run",
    response_model=TaskRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_task_now(
    task_id: UUID,
    runtime: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
) -> TaskRunResponse:
    try:
        return TaskRunResponse.model_validate(runtime.enqueue_manual_run(task_id))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/task-runs", response_model=list[TaskRunResponse])
def list_task_runs(
    session: Annotated[Session, Depends(get_db_session)],
    task_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskRunResponse]:
    runs = SchedulerRepository(session).list_runs(
        task_id=task_id,
        limit=limit,
        offset=offset,
    )
    return [TaskRunResponse.model_validate(run) for run in runs]


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRunResponse])
def list_task_runs_for_task(
    task_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskRunResponse]:
    repository = SchedulerRepository(session)
    if repository.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    runs = repository.list_runs(task_id=task_id, limit=limit, offset=offset)
    return [TaskRunResponse.model_validate(run) for run in runs]


@router.get("/task-runs/{run_id}", response_model=TaskRunResponse)
def get_task_run(
    run_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
) -> TaskRunResponse:
    run = SchedulerRepository(session).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Task run not found")
    return TaskRunResponse.model_validate(run)


def _change_state(
    task_id: UUID,
    version: int,
    target: TaskState,
    session: Session,
    runtime: SchedulerRuntime,
) -> TaskResponse:
    try:
        task = SchedulerService(session, task_registry).change_state(
            task_id,
            expected_version=version,
            target=target,
        )
        session.commit()
        session.refresh(task)
        runtime.sync_task(task.id)
        return _task_response(task, runtime)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


def _task_response(
    task: ScheduledTask,
    runtime: SchedulerRuntime,
    *,
    latest_run: TaskRun | None = None,
) -> TaskResponse:
    response = TaskResponse.model_validate(task)
    return response.model_copy(
        update={
            "next_run_at": runtime.next_run_at(task.id),
            "latest_run": (
                TaskRunResponse.model_validate(latest_run)
                if latest_run is not None
                else None
            ),
        }
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TaskNotFoundError):
        return HTTPException(status_code=404, detail="Task not found")
    if isinstance(exc, SchedulerDisabledError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TaskConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, UnknownTaskTypeError):
        return HTTPException(status_code=422, detail=f"Unknown task type: {exc}")
    if isinstance(exc, InvalidTaskParametersError):
        return HTTPException(status_code=422, detail=exc.errors)
    if isinstance(exc, (ValueError, ValidationError, KeyError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Scheduler operation failed")
