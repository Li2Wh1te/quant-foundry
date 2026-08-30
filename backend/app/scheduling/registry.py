from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel


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
    english_name: str | None = None


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


task_registry = TaskRegistry()


def _register_application_tasks() -> None:
    """Import and register concrete application tasks after registry setup."""
    from app.data_ingestion.scheduler_tasks.etf import register_tasks as register_etf_tasks
    from app.data_ingestion.scheduler_tasks.etf_adjustment import (
        register_tasks as register_etf_adjustment_tasks,
    )
    from app.data_ingestion.scheduler_tasks.etf_daily import register_tasks as register_etf_daily_tasks
    from app.data_ingestion.scheduler_tasks.trade_calendar import register_tasks
    from app.data_ingestion.scheduler_tasks.corporate_action import register_tasks as register_corporate_action_tasks

    register_tasks(task_registry)
    register_etf_tasks(task_registry)
    register_etf_adjustment_tasks(task_registry)
    register_etf_daily_tasks(task_registry)
    register_corporate_action_tasks(task_registry)


_register_application_tasks()
