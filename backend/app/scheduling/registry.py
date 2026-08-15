from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
import structlog


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TaskContext:
    task_id: UUID
    run_id: UUID


class TaskHandler(Protocol):
    def __call__(
        self, context: TaskContext, parameters: BaseModel
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    name: str
    parameters_model: type[BaseModel]
    handler: TaskHandler
    parameter_version: int = 1


class TaskRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, TaskDefinition] = {}

    def register(self, definition: TaskDefinition) -> None:
        if definition.key in self._definitions:
            raise ValueError(f"task type already registered: {definition.key}")
        self._definitions[definition.key] = definition

    def get(self, key: str) -> TaskDefinition | None:
        return self._definitions.get(key)

    def require(self, key: str) -> TaskDefinition:
        definition = self.get(key)
        if definition is None:
            raise KeyError(key)
        return definition

    def list(self) -> list[TaskDefinition]:
        return sorted(self._definitions.values(), key=lambda item: item.key)


class LogMessageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1_000)


def log_message_task(
    context: TaskContext, parameters: BaseModel
) -> dict[str, Any]:
    if not isinstance(parameters, LogMessageParameters):
        raise TypeError("unexpected parameters model for system.log_message")
    logger.info(
        "scheduled_log_message",
        task_id=str(context.task_id),
        run_id=str(context.run_id),
        message=parameters.message,
    )
    return {"message": parameters.message}


task_registry = TaskRegistry()
task_registry.register(
    TaskDefinition(
        key="system.log_message",
        name="Log message",
        parameters_model=LogMessageParameters,
        handler=log_message_task,
    )
)
