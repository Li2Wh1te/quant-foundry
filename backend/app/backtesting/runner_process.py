"""Operating-system process boundary helpers for backtest workers.

The launcher has one security-sensitive job: start a worker without a shell
and make every later signal conditional on a complete process identity.  A PID
alone is not an identity because the operating system may reuse it after a
worker exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import re
from typing import Any, Mapping, Sequence
from uuid import UUID

from .runner_protocol import EXIT_CODE_PROTOCOL, map_exit_code


HANDSHAKE_PROTOCOL = "runner_handshake@1"
DEFAULT_OUTPUT_EXCERPT_BYTES = 4096


def _canonical_uuid(value: UUID | str, field: str) -> str:
    """Validate and normalize an identity value before putting it in argv."""

    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


class ProcessIdentityMismatch(RuntimeError):
    """Raised when a signal target is not provably the intended worker."""


class WorkerHandshakeError(ValueError):
    """Raised when a worker handshake is malformed or belongs to another launch."""


@dataclass(frozen=True, slots=True)
class LaunchIdentity:
    """Persisted identity required to safely supervise one worker attempt."""

    run_id: UUID | str
    launch_id: UUID | str
    pid: int
    start_identity: str
    process_group_id: int

    @property
    def process_start_token(self) -> str:
        """Compatibility name used by the task-08 root model."""

        return self.start_identity

    @property
    def child_pid(self) -> int:
        return self.pid

    @property
    def child_process_group_id(self) -> int:
        return self.process_group_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "launch_id": str(self.launch_id),
            "child_pid": self.pid,
            "pid": self.pid,
            "child_start_identity": self.start_identity,
            "process_start_token": self.start_identity,
            "child_process_group_id": self.process_group_id,
            "process_group_id": self.process_group_id,
        }


@dataclass(frozen=True, slots=True)
class WorkerHandshake:
    """Validated handshake payload sent by a child process."""

    run_id: str
    launch_id: str
    pid: int
    start_identity: str
    process_group_id: int
    protocol_version: str = HANDSHAKE_PROTOCOL
    worker_id: str | None = None
    observed_at: datetime | None = None

    @property
    def process_start_token(self) -> str:
        return self.start_identity

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "launch_id": self.launch_id,
            "pid": self.pid,
            "worker_pid": self.pid,
            "start_identity": self.start_identity,
            "process_start_token": self.start_identity,
            "process_group_id": self.process_group_id,
            "worker_id": self.worker_id,
        }
        if self.observed_at is not None:
            payload["observed_at"] = self.observed_at.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class HandshakeValidation:
    valid: bool
    errors: tuple[str, ...] = ()
    handshake: WorkerHandshake | None = None

    def __bool__(self) -> bool:
        return self.valid


@dataclass(frozen=True, slots=True)
class ResourceLimitEvidence:
    """Honest resource-limit capability evidence persisted with a run."""

    resource: str
    requested: int | None
    supported: bool
    applied: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "requested": self.requested,
            "supported": self.supported,
            "applied": self.applied,
            "error": self.error,
        }


class StdoutCapture:
    """Bounded, streaming stdout/stderr evidence collector.

    The digest covers every consumed byte, including bytes beyond the excerpt
    and configured retention limit.  No full output is retained in memory.
    """

    def __init__(
        self,
        max_bytes: int = 1_048_576,
        *,
        excerpt_bytes: int = DEFAULT_OUTPUT_EXCERPT_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if isinstance(excerpt_bytes, bool) or not isinstance(excerpt_bytes, int) or excerpt_bytes < 0:
            raise ValueError("excerpt_bytes must be a non-negative integer")
        self.max_bytes = max_bytes
        self.excerpt_bytes = excerpt_bytes
        self._digest = hashlib.sha256()
        self._excerpt = bytearray()
        self.total_bytes = 0
        self.truncated = False

    def consume(self, chunk: bytes | bytearray | memoryview | str) -> bool:
        """Consume one chunk and return whether the cap has been exceeded."""

        if isinstance(chunk, str):
            data = chunk.encode("utf-8", "replace")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            data = bytes(chunk)
        else:
            raise TypeError("stdout chunks must be bytes or text")
        if not data:
            return self.truncated
        self._digest.update(data)
        self.total_bytes += len(data)
        if len(self._excerpt) < self.excerpt_bytes:
            self._excerpt.extend(data[: self.excerpt_bytes - len(self._excerpt)])
        # Reaching the configured byte budget is already a limit event.  Do
        # not wait for one extra chunk before asking the Supervisor to stop
        # the process group.
        if self.total_bytes >= self.max_bytes:
            self.truncated = True
        return self.truncated

    @property
    def bytes(self) -> int:
        return self.total_bytes

    @property
    def digest(self) -> str:
        return "sha256:" + self._digest.hexdigest()

    @property
    def excerpt(self) -> str:
        return bytes(self._excerpt).decode("utf-8", "replace")

    def evidence(self) -> dict[str, Any]:
        return {
            "bytes": self.total_bytes,
            "stdout_bytes": self.total_bytes,
            "digest": self.digest,
            "stdout_digest": self.digest,
            "excerpt": self.excerpt,
            "truncated": self.truncated,
            "stdout_truncated": self.truncated,
            "max_bytes": self.max_bytes,
        }


def build_worker_command(
    run_id: UUID | str,
    launch_id: UUID | str,
    *,
    python_executable: str | None = None,
    module: str = "app.backtesting.runner_worker",
    ) -> list[str]:
    """Build a shell-free command containing only public launch identities."""

    normalized_run_id = _canonical_uuid(run_id, "run_id")
    normalized_launch_id = _canonical_uuid(launch_id, "launch_id")
    if not module or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) is None:
        raise ValueError("worker module must be a non-blank import path")
    return [
        python_executable or sys.executable,
        "-m",
        module,
        "--run-id",
        normalized_run_id,
        "--launch-id",
        normalized_launch_id,
    ]


def _memory_limit_preexec(memory_limit_mib: int | None):
    """Return a child-only callback; never applies worker limits to the parent."""

    if memory_limit_mib is None:
        return None
    if isinstance(memory_limit_mib, bool) or not isinstance(memory_limit_mib, int) or memory_limit_mib <= 0:
        raise ValueError("memory_limit_mib must be a positive integer")

    def apply() -> None:
        try:
            import resource

            limit = memory_limit_mib * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, AttributeError, OSError, ValueError) as exc:
            # A configured POSIX limit that cannot be installed must fail the
            # child launch.  Returning here would let the Supervisor persist
            # a successful-looking launch while the worker ran unrestricted.
            raise RuntimeError(
                f"address-space limit could not be applied: {type(exc).__name__}: {exc}"
            ) from exc

    return apply


def memory_limit_evidence(memory_limit_mib: int | None) -> ResourceLimitEvidence:
    """Report platform capability without claiming application when unknown."""

    if memory_limit_mib is not None and (
        isinstance(memory_limit_mib, bool)
        or not isinstance(memory_limit_mib, int)
        or memory_limit_mib <= 0
    ):
        raise ValueError("memory_limit_mib must be a positive integer or None")
    supported = False
    error: str | None = None
    if memory_limit_mib is not None:
        try:
            import resource

            supported = hasattr(resource, "RLIMIT_AS") and os.name == "posix"
            if not supported:
                error = "platform does not expose POSIX RLIMIT_AS"
        except (ImportError, AttributeError) as exc:
            error = f"{type(exc).__name__}: resource limit API unavailable"
    return ResourceLimitEvidence(
        "address_space_mib",
        memory_limit_mib,
        supported,
        False,
        error,
    )


def apply_memory_limit(memory_limit_mib: int | None) -> ResourceLimitEvidence:
    """Apply the address-space limit in the current worker process only.

    The Supervisor normally applies the same limit in ``preexec_fn``.  This
    explicit helper is also used by direct worker entry points and returns an
    honest evidence object instead of silently claiming that a platform
    limit was installed.
    """

    evidence = memory_limit_evidence(memory_limit_mib)
    if memory_limit_mib is None or not evidence.supported:
        return evidence
    try:
        import resource

        limit = memory_limit_mib * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        return ResourceLimitEvidence(
            evidence.resource,
            evidence.requested,
            evidence.supported,
            False,
            f"{type(exc).__name__}: {str(exc)[:240]}",
        )
    return ResourceLimitEvidence(
        evidence.resource,
        evidence.requested,
        evidence.supported,
        True,
        None,
    )


class WorkerProcessLauncher:
    """Start and identify one worker process group."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        worker_module: str = "app.backtesting.runner_worker",
        memory_limit_mib: int | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.python_executable = python_executable
        self.worker_module = worker_module
        self.memory_limit_mib = memory_limit_mib
        self.environment = dict(environment) if environment is not None else None

    def command(self, run_id: UUID | str, launch_id: UUID | str) -> list[str]:
        return build_worker_command(
            run_id,
            launch_id,
            python_executable=self.python_executable,
            module=self.worker_module,
        )

    def resource_limit_evidence(self) -> ResourceLimitEvidence:
        """Describe launch configuration until the child confirms application.

        The parent can inspect platform capability but cannot prove that the
        child ``preexec_fn`` completed.  Only the worker's post-application
        evidence may set ``applied=True``.
        """

        base = memory_limit_evidence(self.memory_limit_mib)
        if base.supported and self.memory_limit_mib is not None:
            return ResourceLimitEvidence(
                base.resource,
                base.requested,
                base.supported,
                False,
                "awaiting_worker_confirmation",
            )
        return base

    def start(self, run_id: UUID | str, launch_id: UUID | str) -> subprocess.Popen[bytes]:
        """Start with ``shell=False`` and a dedicated POSIX process group."""

        limit_evidence = memory_limit_evidence(self.memory_limit_mib)
        preexec = (
            _memory_limit_preexec(self.memory_limit_mib)
            if os.name == "posix" and limit_evidence.supported
            else None
        )
        environment = None
        if self.environment is not None:
            # A caller-provided overlay must not accidentally remove PATH,
            # locale, or the module search path required by the worker.
            environment = os.environ.copy()
            environment.update(self.environment)
        return subprocess.Popen(
            self.command(run_id, launch_id),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=(os.name == "posix"),
            preexec_fn=preexec,
            env=environment,
        )

    launch = start


def get_process_start_identity(pid: int) -> str | None:
    """Return a stable process-start token on supported POSIX platforms."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    # Linux exposes a monotonic start tick at /proc/<pid>/stat field 22.  The
    # command name may contain spaces and closing parentheses, so split only
    # after the final ``)`` before indexing fields.
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        content = stat_path.read_text(encoding="utf-8")
        after_name = content.rsplit(")", 1)[1].split()
        # ``after_name[0]`` is state (field 3), therefore field 22 is index 19.
        return after_name[19]
    except (OSError, IndexError, UnicodeError):
        pass
    # macOS/BSD do not expose /proc.  ``lstart`` is stable for a process
    # lifetime and is normalized so it remains safe to persist as text.
    try:
        output = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return output or None


# Common spelling used by launch-identity fields in older migrations.
get_process_start_token = get_process_start_identity


def get_process_group_id(pid: int) -> int | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


def is_process_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def identity_from_process(
    process: Any,
    *,
    run_id: UUID | str,
    launch_id: UUID | str,
) -> LaunchIdentity:
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WorkerHandshakeError("worker process has no valid PID")
    # Use the compatibility alias at the call site so tests and adapters can
    # inject a platform-specific start-token provider without monkeypatching
    # the implementation name.
    start_identity = get_process_start_token(pid)
    process_group_id = get_process_group_id(pid)
    if not start_identity or process_group_id is None:
        raise WorkerHandshakeError("worker process identity could not be captured")
    return LaunchIdentity(run_id, launch_id, pid, start_identity, process_group_id)


def _identity_with_components(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    run_id: UUID | str = "unknown",
    launch_id: UUID | str = "unknown",
) -> LaunchIdentity | Mapping[str, Any] | None:
    """Accept both the object form and the low-level PID/start/PGID form."""

    if start_identity is None and process_group_id is None:
        return identity
    if isinstance(identity, bool) or not isinstance(identity, int):
        return None
    if not isinstance(start_identity, str) or not start_identity:
        return None
    if isinstance(process_group_id, bool) or not isinstance(process_group_id, int):
        return None
    return LaunchIdentity(run_id, launch_id, identity, start_identity, process_group_id)


def process_identity_matches(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    allow_exited: bool = False,
    run_id: UUID | str = "unknown",
    launch_id: UUID | str = "unknown",
) -> bool:
    """Check PID, start token and group ID before any signal operation."""

    normalized = _identity_from_any(
        _identity_with_components(
            identity,
            start_identity,
            process_group_id,
            run_id=run_id,
            launch_id=launch_id,
        )
    )
    if normalized is None:
        return False
    current_start = get_process_start_token(normalized.pid)
    current_group = get_process_group_id(normalized.pid)
    if current_start is None or current_group is None:
        return False
    if current_start != normalized.start_identity or current_group != normalized.process_group_id:
        return False
    if not allow_exited and not is_process_alive(normalized.pid):
        return False
    return True


is_same_process = process_identity_matches


def _identity_from_any(identity: LaunchIdentity | Mapping[str, Any]) -> LaunchIdentity | None:
    if isinstance(identity, LaunchIdentity):
        return identity
    if not isinstance(identity, Mapping):
        return None
    try:
        pid_value = identity.get("pid")
        if pid_value is None:
            pid_value = identity["child_pid"]
        start_value = identity.get("start_identity")
        if start_value is None:
            start_value = identity.get("child_start_identity")
        if start_value is None:
            start_value = identity["process_start_token"]
        group_value = identity.get("process_group_id")
        if group_value is None:
            group_value = identity.get("child_process_group_id")
        if group_value is None:
            group_value = identity["child_process_group_id"]
        return LaunchIdentity(
            identity["run_id"],
            identity["launch_id"],
            int(pid_value),
            str(start_value),
            int(group_value),
        )
    except (KeyError, TypeError, ValueError):
        return None


def signal_process_group(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    sig: int,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    strict: bool = False,
    run_id: UUID | str = "unknown",
    launch_id: UUID | str = "unknown",
) -> bool:
    """Signal only a group whose complete identity still matches."""

    normalized_input = _identity_with_components(
        identity,
        start_identity,
        process_group_id,
        run_id=run_id,
        launch_id=launch_id,
    )
    normalized = _identity_from_any(normalized_input)
    if normalized is None or not process_identity_matches(normalized):
        if strict:
            raise ProcessIdentityMismatch("worker PID/start identity/process group no longer match")
        return False
    try:
        if os.name == "posix":
            os.killpg(normalized.process_group_id, sig)
        else:
            os.kill(normalized.pid, sig)
    except (OSError, ProcessLookupError):
        return False
    return True


def send_graceful_termination(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    strict: bool = False,
    run_id: UUID | str = "unknown",
    launch_id: UUID | str = "unknown",
) -> bool:
    return signal_process_group(
        identity,
        signal.SIGTERM,
        start_identity,
        process_group_id,
        strict=strict,
        run_id=run_id,
        launch_id=launch_id,
    )


def send_force_kill(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    strict: bool = False,
    run_id: UUID | str = "unknown",
    launch_id: UUID | str = "unknown",
) -> bool:
    kill_signal = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
    return signal_process_group(
        identity,
        kill_signal,
        start_identity,
        process_group_id,
        strict=strict,
        run_id=run_id,
        launch_id=launch_id,
    )


terminate_process_group = send_graceful_termination
kill_process_group = send_force_kill
WorkerLauncher = WorkerProcessLauncher
ProcessManager = WorkerProcessLauncher
ProcessIdentity = LaunchIdentity


def _process_group_exists(process_group_id: int) -> bool:
    """Check group liveness after the original group leader has exited.

    This is observation only: the caller has already validated and signalled
    the original identity, and this helper never sends a signal.  It closes
    the old false-positive path where a dead leader made recovery report the
    group as collected while a descendant still held the process group.
    """

    if os.name != "posix" or isinstance(process_group_id, bool) or process_group_id <= 0:
        return False
    try:
        os.killpg(process_group_id, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_group_exit(
    identity: LaunchIdentity | Mapping[str, Any] | int,
    timeout_seconds: float | None = None,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    *,
    grace_seconds: float | None = None,
    poll_interval_seconds: float = 0.05,
    clock=time.monotonic,
) -> bool:
    """Wait for the verified worker group, not just its leader, to exit."""

    if timeout_seconds is None:
        timeout_seconds = grace_seconds
    if timeout_seconds is None:
        raise ValueError("timeout_seconds or grace_seconds is required")
    if timeout_seconds < 0 or poll_interval_seconds <= 0:
        raise ValueError("timeout and poll interval must be non-negative/positive")
    normalized = _identity_from_any(
        _identity_with_components(identity, start_identity, process_group_id)
    )
    if normalized is None:
        return False

    def still_running() -> bool:
        if process_identity_matches(normalized):
            return True
        # A reused PID is not the original worker and therefore is treated as
        # gone.  Only when the original leader is gone do we inspect the fixed
        # process group for inherited-descriptor descendants.
        if is_process_alive(normalized.pid):
            return False
        return _process_group_exists(normalized.process_group_id)

    deadline = clock() + timeout_seconds
    while clock() <= deadline:
        if not still_running():
            return True
        time.sleep(poll_interval_seconds)
    return not still_running()


def drain_output(
    process: Any,
    capture: StdoutCapture,
    *,
    timeout_seconds: float = 0,
    chunk_size: int = 64 * 1024,
) -> bool:
    """Drain only up to the configured cap, then let Supervisor terminate."""

    stream = getattr(process, "stdout", None)
    if stream is None or capture.truncated:
        return capture.truncated

    def read_size() -> int:
        remaining = capture.max_bytes - capture.bytes
        return min(chunk_size, remaining)

    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        read = getattr(stream, "read", None)
        if callable(read):
            size = read_size()
            if size > 0:
                chunk = read(size)
                if chunk:
                    capture.consume(chunk)
        return capture.truncated
    try:
        fd = fileno()
        os.set_blocking(fd, False)
    except (OSError, AttributeError, ValueError):
        return capture.truncated
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while not capture.truncated:
            size = read_size()
            if size <= 0:
                break
            wait = max(0.0, deadline - time.monotonic()) if timeout_seconds else 0.0
            events = selector.select(wait)
            if not events:
                break
            made_progress = False
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, size)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                made_progress = True
                capture.consume(chunk)
                # Stop in the same supervision tick once the cap is reached;
                # the next action is a termination request, not more reads.
                break
            if not made_progress:
                break
    finally:
        selector.close()
    return capture.truncated


def validate_handshake(
    payload: Mapping[str, Any] | None,
    *,
    expected_run_id: UUID | str,
    expected_launch_id: UUID | str,
    expected_pid: int,
    expected_start_identity: str,
    expected_process_group_id: int,
) -> HandshakeValidation:
    """Validate all launch identity fields before marking ``running``."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return HandshakeValidation(False, ("handshake must be an object",))
    if payload.get("protocol_version") != HANDSHAKE_PROTOCOL:
        errors.append("handshake protocol_version is unsupported")
    run_id = payload.get("run_id")
    launch_id = payload.get("launch_id")
    if str(run_id) != str(expected_run_id):
        errors.append("handshake run_id does not match launch")
    if str(launch_id) != str(expected_launch_id):
        errors.append("handshake launch_id does not match launch")
    pid = payload.get("pid", payload.get("worker_pid"))
    if isinstance(pid, bool) or not isinstance(pid, int) or pid != expected_pid:
        errors.append("handshake PID does not match Popen PID")
    start_identity = payload.get("start_identity", payload.get("process_start_token"))
    if not isinstance(start_identity, str) or start_identity != expected_start_identity:
        errors.append("handshake start identity does not match launch")
    group_id = payload.get("process_group_id", payload.get("child_process_group_id"))
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id != expected_process_group_id:
        errors.append("handshake process group does not match launch")
    observed_at = payload.get("observed_at")
    timestamp: datetime | None = None
    if observed_at is not None:
        if isinstance(observed_at, datetime):
            timestamp = observed_at
        elif isinstance(observed_at, str):
            try:
                timestamp = datetime.fromisoformat(observed_at)
            except ValueError:
                errors.append("handshake observed_at is not ISO-8601")
        else:
            errors.append("handshake observed_at has invalid type")
        if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
            errors.append("handshake observed_at must be timezone-aware")
    handshake = None
    if not errors:
        handshake = WorkerHandshake(
            str(run_id),
            str(launch_id),
            pid,
            start_identity,
            group_id,
            HANDSHAKE_PROTOCOL,
            payload.get("worker_id"),
            timestamp.astimezone(UTC) if timestamp else None,
        )
    return HandshakeValidation(not errors, tuple(errors), handshake)


def build_handshake(
    *,
    run_id: UUID | str,
    launch_id: UUID | str,
    pid: int | None = None,
    start_identity: str | None = None,
    process_group_id: int | None = None,
    worker_id: str | None = None,
    observed_at: datetime | None = None,
) -> WorkerHandshake:
    """Build a child handshake from the child's own OS identity."""

    actual_pid = os.getpid() if pid is None else pid
    actual_start = get_process_start_token(actual_pid) if start_identity is None else start_identity
    actual_group = get_process_group_id(actual_pid) if process_group_id is None else process_group_id
    if not actual_start or actual_group is None:
        raise WorkerHandshakeError("unable to capture worker process identity")
    return WorkerHandshake(
        str(run_id),
        str(launch_id),
        actual_pid,
        actual_start,
        actual_group,
        HANDSHAKE_PROTOCOL,
        worker_id,
        observed_at or datetime.now(UTC),
    )


__all__ = [
    "DEFAULT_OUTPUT_EXCERPT_BYTES",
    "HANDSHAKE_PROTOCOL",
    "HandshakeValidation",
    "LaunchIdentity",
    "ProcessIdentityMismatch",
    "ProcessIdentity",
    "ProcessManager",
    "ResourceLimitEvidence",
    "StdoutCapture",
    "WorkerHandshake",
    "WorkerHandshakeError",
    "WorkerProcessLauncher",
    "WorkerLauncher",
    "build_handshake",
    "build_worker_command",
    "drain_output",
    "apply_memory_limit",
    "get_process_group_id",
    "get_process_start_identity",
    "get_process_start_token",
    "identity_from_process",
    "is_process_alive",
    "is_same_process",
    "kill_process_group",
    "memory_limit_evidence",
    "process_identity_matches",
    "send_force_kill",
    "send_graceful_termination",
    "signal_process_group",
    "terminate_process_group",
    "validate_handshake",
    "wait_for_group_exit",
]
