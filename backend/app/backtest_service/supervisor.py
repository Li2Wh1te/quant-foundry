"""S3 trusted supervisor. One bounded private worker per authoritative claim."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from .local_queue import encoded
from .process import attempt_name, container_name
from .runtime_contracts import Claim, RuntimeFault


@dataclass
class Binding:
    spec: dict
    market_routes: dict = field(default_factory=dict)
    research_routes: dict = field(default_factory=dict)
    gateway: object = None
    context: object = None
    thread: Thread | None = None
    channel: object = None
    listener: object = None
    error: BaseException | None = None


class DataRunServices:
    """D13 supplies accepted run -> D04 gateway/facts; no fallback mapper.

    builder(work, claim, stopped) returns Binding with gateway/context already
    admitted. The actual D04 serve and final-state transaction are used here.
    """

    def __init__(self, builder, result_root):
        self.builder, self.result_root = builder, Path(result_root)

    def prepare(self, claim, work, ipc_directory, stopped):
        from quantfoundry._transport import Channel

        from .data_gateway import serve

        binding = self.builder(work, claim, stopped)
        if binding.gateway is None or binding.context is None:
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "运行数据准入尚未接线")
        binding.listener = _listener(ipc_directory / "data.sock")
        binding.spec["source"]["socket_path"] = "/run/qf/ipc/data.sock"

        def run():
            try:
                peer = _accept(binding.listener, stopped)
                binding.channel = Channel(
                    peer,
                    arrow_budget=binding.gateway.batch_bytes,
                    timeout=30,
                    cancelled=stopped.is_set,
                )
                serve(
                    binding.channel,
                    binding.gateway,
                    binding.context,
                    close_context=False,
                )
            except BaseException as error:
                if not stopped.is_set() and getattr(error, "code", None) != "CANCELLED":
                    binding.error = error

        binding.thread = Thread(target=run, daemon=True)
        binding.thread.start()
        return binding

    def writer(self, claim, work):
        from quantfoundry._native import TrustedResultWriter

        return TrustedResultWriter(
            str(self.result_root), claim.run_id, work.limits.result_bytes
        )

    def commit_success(self, claim, binding, commit):
        if binding.error is not None:
            raise binding.error
        return binding.gateway.finalize(binding.context, commit)

    def close(self, binding):
        if binding.channel is not None:
            binding.channel.close()
        if binding.listener is not None:
            binding.listener.close()
        if binding.thread is not None:
            binding.thread.join(5)
            if binding.thread.is_alive():
                raise RuntimeFault("RESOURCE_LIMIT", "数据网关未在预算内关闭")
        if binding.gateway is not None:
            binding.gateway.close(binding.context)


def _listener(path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o666)
    listener.listen(1)
    listener.settimeout(0.1)
    return listener


def _accept(listener, stopped):
    import struct

    while not stopped.is_set():
        try:
            peer, _ = listener.accept()
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != 65532:
                peer.close()
                raise RuntimeFault("DATA_RESTRICTED", "IPC对端不是本worker用户")
            return peer
        except socket.timeout:
            continue
    raise RuntimeFault("CANCELLED", "worker通道已取消")


@dataclass
class _Task:
    claim: Claim
    work: object
    path: Path
    identity: os.stat_result
    listener: socket.socket
    binding: Binding
    writer: object
    started: float
    stopped: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    channel: object = None
    thread: Thread | None = None
    outcome: dict | None = None
    error: BaseException | None = None
    sequence: int = 1
    output_bytes: int = 0
    output_truncated: bool = False
    name: str | None = None
    last_heartbeat: float = 0
    heartbeat_thread: Thread | None = None


class RunnerSupervisor:
    def __init__(
        self, repository, process, services, *, lease_seconds=30, supervisor_id=None
    ):
        if not 2 <= lease_seconds <= 300:
            raise RuntimeFault("RESOURCE_LIMIT", "租约无效")
        self.repository, self.process, self.services = repository, process, services
        self.lease_seconds, self.id = lease_seconds, supervisor_id or uuid4().hex
        self.tasks = {}
        self.stopping = False
        self.peak_workers = 0
        self.started_runs = []

    def _control(self, task):
        from quantfoundry._runtime_ipc import RuntimeChannel
        from quantfoundry._transport import Frame

        channel = None
        try:
            peer = _accept(task.listener, task.stopped)
            channel = RuntimeChannel(
                peer, arrow_budget=1, timeout=30, cancelled=task.stopped.is_set
            )
            task.channel = channel
            last_id = 0
            booted = False
            while not task.stopped.is_set():
                frame = channel.receive()
                if (
                    frame.run_id != task.claim.run_id
                    or frame.status != "request"
                    or frame.payload
                    or frame.request_id <= last_id
                    or self.repository.cancelled(task.claim)
                ):
                    raise RuntimeFault("STALE_CLAIM", "旧claim或越界消息被拒绝")
                last_id = frame.request_id
                body = {}
                if frame.op == "boot" and not booted and not frame.body:
                    booted = True
                    if not self.repository.running(task.claim):
                        raise RuntimeFault("CANCELLED", "claim已取消")
                    spec = dict(
                        task.binding.spec,
                        run_id=task.claim.run_id,
                        config=task.work.config,
                        result_budget_bytes=task.work.limits.result_bytes,
                        wall_time_seconds=task.work.limits.wall_seconds,
                    )
                    if spec["data_budget_bytes"] > task.work.limits.memory_bytes // 2:
                        raise RuntimeFault(
                            "RESOURCE_LIMIT", "数据预算超过worker内存分配"
                        )
                    body = dict(
                        spec=spec,
                        strategy_sha256=hashlib.sha256(
                            task.work.strategy_source.encode()
                        ).hexdigest(),
                        market_routes=task.binding.market_routes,
                        research_routes=task.binding.research_routes,
                        output_bytes=task.work.limits.output_bytes,
                    )
                elif frame.op == "result" and booted and set(frame.body) == {"records"}:
                    records = frame.body["records"]
                    if type(records) is not list or not 1 <= len(records) <= 64:
                        raise RuntimeFault("RESULT_BUDGET_EXCEEDED", "结果信用超限")
                    task.writer.write_json(
                        encoded(
                            dict(first_sequence=str(task.sequence), records=records)
                        ).decode()
                    )
                    task.sequence += len(records)
                elif (
                    frame.op == "output"
                    and booted
                    and set(frame.body) == {"message", "truncated"}
                ):
                    message, truncated = frame.body["message"], frame.body["truncated"]
                    if (
                        type(message) is not str
                        or type(truncated) is not bool
                        or len(message.encode()) > 16384
                    ):
                        raise RuntimeFault("INVALID_CONTRACT", "标准输出消息无效")
                    task.output_bytes += len(message.encode())
                    if task.output_bytes > task.work.limits.output_bytes + 16384:
                        raise RuntimeFault("RESOURCE_LIMIT", "标准输出超过预算")
                    task.output_truncated |= truncated
                    task.writer.write_json(
                        encoded(
                            dict(
                                first_sequence=str(task.sequence),
                                records=[
                                    dict(
                                        kind="log",
                                        record=dict(
                                            time_ns="0",
                                            level="info",
                                            message=message,
                                            truncated=truncated,
                                        ),
                                    )
                                ],
                            )
                        ).decode()
                    )
                    task.sequence += 1
                elif (
                    frame.op == "notices" and booted and set(frame.body) == {"notices"}
                ):
                    task.writer.notices_json(encoded(frame.body["notices"]).decode())
                elif (
                    frame.op == "finished" and booted and set(frame.body) == {"outcome"}
                ):
                    outcome = frame.body["outcome"]
                    outcome = json.loads(task.writer.validate_outcome_json(encoded(outcome).decode()))
                    if type(outcome) is not dict or outcome.get("status") not in (
                        "succeeded",
                        "failed",
                        "cancelled",
                    ):
                        raise RuntimeFault("INVALID_CONTRACT", "worker终态无效")
                    if (
                        outcome["status"] == "succeeded"
                        and outcome.get("result_id") != task.claim.run_id
                    ):
                        raise RuntimeFault("STALE_CLAIM", "结果不属于本运行")
                    task.outcome = outcome
                    channel.send(
                        Frame(frame.run_id, frame.request_id, frame.op, "ok", {})
                    )
                    break
                else:
                    raise RuntimeFault("INVALID_CONTRACT", "未知或乱序的worker控制消息")
                channel.send(
                    Frame(frame.run_id, frame.request_id, frame.op, "ok", body)
                )
        except BaseException as error:
            task.error = error
        finally:
            if channel is not None:
                channel.close()
            task.done.set()

    def _start(self, claim):
        work = self.repository.work(claim)
        relative = attempt_name(claim)
        path = self.process.root.runner / relative
        if len(str(path / "ipc/control.sock").encode()) > 107:
            raise RuntimeFault(
                "CAPABILITY_UNAVAILABLE", "runner工作根过长，不能创建私有Unix端点"
            )
        self.process.root.check()
        path.mkdir(mode=0o700)
        identity = path.stat()
        stopped = Event()
        listener = binding = task = None
        try:
            if not self.repository.bind_attempt(
                claim, identity.st_dev, identity.st_ino
            ):
                raise RuntimeFault("STALE_CLAIM", "目录创建时claim已失效")
            code, ipc = path / "code", path / "ipc"
            code.mkdir(mode=0o755)
            ipc.mkdir(mode=0o755)
            for name, data in [
                ("strategy.py", work.strategy_source),
                ("run-id", claim.run_id),
            ]:
                fd = os.open(
                    code / name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o444,
                )
                with os.fdopen(fd, "w") as file:
                    file.write(data)
            os.chmod(code, 0o555)
            listener = _listener(ipc / "control.sock")
            binding = self.services.prepare(claim, work, ipc, stopped)
            task = _Task(
                claim,
                work,
                path,
                identity,
                listener,
                binding,
                self.services.writer(claim, work),
                time.monotonic(),
                stopped=stopped,
            )
            task.thread = Thread(target=self._control, args=(task,), daemon=True)

            def heartbeat():
                while not stopped.wait(self.lease_seconds / 4):
                    if not self.repository.heartbeat(
                        claim, time.time(), self.lease_seconds
                    ):
                        stopped.set()
                        if task.channel is not None:
                            task.channel.close()
                        break

            task.heartbeat_thread = Thread(target=heartbeat, daemon=True)
            task.heartbeat_thread.start()
            task.thread.start()
            if self.repository.cancelled(claim):
                raise RuntimeFault("STALE_CLAIM", "启动前claim已失效")
            task.name = self.process.start(claim, relative, work.limits)
            self.tasks[claim.run_id] = task
            self.started_runs.append(claim.run_id)
            self.peak_workers = max(self.peak_workers, len(self.tasks))
        except BaseException:
            stopped.set()
            if listener is not None:
                listener.close()
            if task is not None and task.channel is not None:
                task.channel.close()
            if task is not None and task.thread is not None:
                task.thread.join(5)
            if task is not None and task.heartbeat_thread is not None:
                task.heartbeat_thread.join(5)
            if binding is not None:
                self.services.close(binding)
            self._cleanup(path, identity)
            raise

    def _cleanup(self, path, identity):
        self.process.root.check()
        try:
            current = path.stat()
        except FileNotFoundError:
            return  # Another fenced recovery already removed this exact attempt.
        if path.is_symlink() or (current.st_dev, current.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        ):
            raise RuntimeFault("INVALID_CONTRACT", "拒绝清理被替换的attempt目录")
        # Workers have no writable host mount. Delete exactly the directories and
        # names created here; unexpected files/links are refused, never traversed.
        expected = {
            "code": {"strategy.py", "run-id"},
            "ipc": {"control.sock", "data.sock"},
        }
        if not set(p.name for p in path.iterdir()) <= set(expected):
            raise RuntimeFault("INVALID_CONTRACT", "attempt含未知对象；拒绝清理")
        for directory, names in expected.items():
            root = path / directory
            if (
                root.is_symlink()
                or not root.is_dir()
                or not set(p.name for p in root.iterdir()) <= names
            ):
                raise RuntimeFault("INVALID_CONTRACT", "attempt子目录被替换")
            os.chmod(root, 0o700)
            for item in root.iterdir():
                mode = item.lstat()
                if (
                    item.is_symlink()
                    or not (stat.S_ISREG(mode.st_mode) or stat.S_ISSOCK(mode.st_mode))
                    or mode.st_nlink != 1
                ):
                    raise RuntimeFault("INVALID_CONTRACT", "attempt含非法链接")
                item.unlink()
            root.rmdir()
        path.rmdir()

    def _finish(self, task, outcome):
        if outcome["status"] != "succeeded":
            task.stopped.set()
        task.listener.close()
        if task.channel is not None:
            task.channel.close()
        try:
            self.process.daemon.stop_remove(
                task.name or container_name(task.claim), task.claim
            )
        except RuntimeFault as error:
            if error.code != "STALE_CLAIM":
                raise
        if task.thread is not None:
            task.thread.join(5)
            if task.thread.is_alive():
                raise RuntimeFault("RESOURCE_LIMIT", "控制通道未释放")
        try:
            task.writer.seal(encoded(outcome).decode())
            result = task.writer.directory_name()
            publish = task.writer.retain
        except BaseException:
            result = None
            publish = None
            if outcome["status"] == "succeeded":
                outcome = self._failure(
                    RuntimeFault("RESULT_BUDGET_EXCEEDED", "必需结果无法持久化")
                )
        try:
            commit = lambda connection=None: self.repository.commit_outcome(
                task.claim, outcome, result, connection, publish
            )
            if outcome["status"] == "succeeded":
                try:
                    won = self.services.commit_success(task.claim, task.binding, commit)
                except BaseException as error:
                    outcome = self._failure(error)
                    won = self.repository.commit_outcome(task.claim, outcome, None)
            else:
                won = commit()
            if not won and self.repository.cancelled(task.claim):
                self.repository.commit_outcome(
                    task.claim, dict(status="cancelled", partial=True), None
                )
        finally:
            task.stopped.set()
            if task.heartbeat_thread is not None:
                task.heartbeat_thread.join(5)
            self.services.close(task.binding)
            self._cleanup(task.path, task.identity)
            self.tasks.pop(task.claim.run_id, None)

    @staticmethod
    def _failure(error):
        # No Docker errors/paths/DSNs/SQL are relayed to user diagnostics.
        if isinstance(error, RuntimeFault):
            diagnostic = error.diagnostic()
        else:
            code = getattr(error, "code", "STRATEGY_ERROR")
            if code not in (
                "DATA_CHANGED",
                "DATA_RESTRICTED",
                "RESOURCE_LIMIT",
                "RESULT_BUDGET_EXCEEDED",
                "CAPABILITY_UNAVAILABLE",
                "INVALID_CONTRACT",
                "CANCELLED",
                "STRATEGY_ERROR",
            ):
                code = "STRATEGY_ERROR"
            diagnostic = dict(
                code=code,
                operation="supervisor",
                message="运行或数据通道失败，请核对所示错误码",
                scope={},
            )
        return dict(status="failed", error=diagnostic, partial=True)

    def step(self):
        now = time.time()
        for claim in self.repository.expired(now):
            try:
                self.process.daemon.stop_remove(container_name(claim), claim)
            except RuntimeFault as error:
                if error.code == "STALE_CLAIM":
                    continue
                raise
            # Must also close stale private channels before releasing reservation.
            task = self.tasks.get(claim.run_id)
            if task is not None:
                self._finish(task, dict(status="cancelled", partial=True))
            else:
                identity = self.repository.attempt_identity(claim)
                path = self.process.root.runner / attempt_name(claim)
                if identity is not None and path.exists():
                    current = path.stat()
                    if (current.st_dev, current.st_ino) != identity:
                        raise RuntimeFault(
                            "INVALID_CONTRACT", "旧attempt目录身份已替换，拒绝接管"
                        )
                    self._cleanup(path, current)
            self.repository.recover_stopped(claim, now)
        for task in list(self.tasks.values()):
            now = time.time()
            if now - task.last_heartbeat > self.lease_seconds / 3:
                task.last_heartbeat = now
                if not self.repository.heartbeat(task.claim, now, self.lease_seconds):
                    self._finish(task, dict(status="cancelled", partial=True))
                    continue
            if self.stopping or self.repository.cancelled(task.claim):
                self._finish(task, dict(status="cancelled", partial=True))
                continue
            state = self.process.daemon.inspect(task.name)
            if state is None:
                self._finish(
                    task,
                    self._failure(RuntimeFault("STRATEGY_ERROR", "worker意外消失")),
                )
                continue
            state = state["State"]
            if state.get("OOMKilled"):
                self._finish(
                    task,
                    self._failure(
                        RuntimeFault("RESOURCE_LIMIT", "worker触发内存OOM上限")
                    ),
                )
            elif time.monotonic() - task.started > task.work.limits.wall_seconds:
                self._finish(
                    task,
                    self._failure(RuntimeFault("RESOURCE_LIMIT", "worker达到墙钟上限")),
                )
            elif not state["Running"]:
                if (
                    not task.done.wait(0.2)
                    or task.outcome is None
                    or state["ExitCode"] not in (0, 1)
                    or (
                        task.outcome["status"] == "succeeded" and state["ExitCode"] != 0
                    )
                ):
                    error = task.error or RuntimeFault(
                        "STRATEGY_ERROR", "worker崩溃或FFI异常退出"
                    )
                    self._finish(task, self._failure(error))
                else:
                    self._finish(task, task.outcome)
        if not self.stopping:
            # Shared repository enforces the group budget across supervisors.
            claim = self.repository.claim(self.id, time.time(), self.lease_seconds)
            if claim is not None:
                try:
                    self._start(claim)
                except BaseException as error:
                    self.repository.commit_outcome(claim, self._failure(error), None)

    def run_forever(self, *, stop_event):
        try:
            while not stop_event.is_set():
                self.step()
                stop_event.wait(0.05)
        finally:
            self.stop()

    def stop(self):
        self.stopping = True
        for task in list(self.tasks.values()):
            self._finish(task, dict(status="cancelled", partial=True))
