"""D12 scheduling ports. D13 implements the shared DB/API repository.

Current claim state is separate from retained business results. No checkpoint,
market snapshot, user-controlled Docker arguments or strategy import lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

TERMINAL = frozenset(("succeeded", "failed", "cancelled"))


class RuntimeFault(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

    def diagnostic(self):
        return dict(
            code=self.code, operation="supervisor", message=self.message, scope={}
        )


@dataclass(frozen=True)
class WorkerLimits:
    cpu_millis: int = 1000
    memory_bytes: int = 512 * 1024**2
    pids: int = 32
    tmp_bytes: int = 64 * 1024**2
    file_bytes: int = 32 * 1024**2
    wall_seconds: int = 60
    output_bytes: int = 1024 * 1024
    result_bytes: int = 16 * 1024**2

    def __post_init__(self):
        values = vars(self)
        if any(type(v) is not int or v <= 0 for v in values.values()):
            raise RuntimeFault("RESOURCE_LIMIT", "worker预算须为正整数")
        if (
            not 100 <= self.cpu_millis <= 4000
            or not 128 * 1024**2 <= self.memory_bytes <= 8 * 1024**3
            or not 16 <= self.pids <= 256
            or self.wall_seconds > 3600
            or self.result_bytes > self.tmp_bytes // 2
            or self.tmp_bytes > self.memory_bytes // 2
            or self.file_bytes > self.tmp_bytes
            or self.output_bytes > 16 * 1024**2
        ):
            raise RuntimeFault("RESOURCE_LIMIT", "worker预算超过可信策略")


@dataclass(frozen=True)
class GroupBudget:
    concurrency: int = 2
    cpu_millis: int = 4000
    memory_bytes: int = 8 * 1024**3
    pids: int = 256
    tmp_bytes: int = 512 * 1024**2
    # Includes supervisor, gateway and the control DB; reserved before workers.
    reserve_cpu_millis: int = 1000
    reserve_memory_bytes: int = 1024**3
    reserve_pids: int = 64

    def __post_init__(self):
        if (
            any(type(v) is not int or v <= 0 for v in vars(self).values())
            or self.concurrency > 256
            or self.reserve_cpu_millis >= self.cpu_millis
            or self.reserve_memory_bytes >= self.memory_bytes
            or self.reserve_pids >= self.pids
        ):
            raise RuntimeFault("RESOURCE_LIMIT", "总运行预算无效")

    def fits(self, limits):
        return (
            len(limits) <= self.concurrency
            and sum(x.cpu_millis for x in limits) + self.reserve_cpu_millis
            <= self.cpu_millis
            and sum(x.memory_bytes for x in limits) + self.reserve_memory_bytes
            <= self.memory_bytes
            and sum(x.pids for x in limits) + self.reserve_pids <= self.pids
            and sum(x.tmp_bytes for x in limits) <= self.tmp_bytes
        )


@dataclass(frozen=True)
class Claim:
    run_id: str
    token: str
    fence: int


@dataclass(frozen=True)
class AcceptedWork:
    owner: str
    config: dict[str, Any]
    strategy_source: str
    limits: WorkerLimits
    # Obtained by a trusted accepted-fact provider, never the REST body.
    specification: dict[str, Any]


class RunRepository(Protocol):
    def claim(
        self, supervisor_id: str, now: float, lease_seconds: float
    ) -> Claim | None: ...
    def work(self, claim: Claim) -> AcceptedWork: ...
    def bind_attempt(self, claim: Claim, device: int, inode: int) -> bool: ...
    def attempt_identity(self, claim: Claim) -> tuple[int, int] | None: ...
    def heartbeat(self, claim: Claim, now: float, lease_seconds: float) -> bool: ...
    def running(self, claim: Claim) -> bool: ...
    def cancelled(self, claim: Claim) -> bool: ...
    def commit_outcome(
        self,
        claim: Claim,
        outcome: dict,
        result: str | None,
        connection=None,
        publish: Callable | None = None,
    ) -> bool: ...
    # Expiry fences IPC/results immediately but keeps its resource reservation.
    # Retake only AFTER Docker confirms the old deterministic container stopped.
    def expired(self, now: float) -> list[Claim]: ...
    def recover_stopped(self, claim: Claim, now: float) -> bool: ...


class RunServices(Protocol):
    def prepare(self, claim: Claim, work: AcceptedWork, ipc_directory, stopped): ...
    def writer(self, claim: Claim, work: AcceptedWork): ...
    def commit_success(self, claim: Claim, binding, commit: Callable): ...
