"""Bounded, desensitized failure evidence for isolated backtest workers."""

from __future__ import annotations

from pathlib import Path
import re
import traceback
from typing import Any


MAX_FAILURE_DETAIL_CHARS = 8_000

# Exception text can contain connection strings, bearer tokens, or secret-like
# key/value pairs.  Failure evidence is operator diagnostics, not a second
# secret channel, so redact those values before persisting or logging them.
_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)(://[^\s/@:]+:)[^\s/@]+@"),
        r"\1<redacted>@",
    ),
    (
        re.compile(r"(?i)(/(?:Users|home|private|tmp|var|opt)/)[^\s:]+"),
        r"\1<redacted>",
    ),
)


def _redact(value: str) -> str:
    """Remove common secret and host-path forms from diagnostic text."""

    result = value
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return a bounded cause/context chain without following cycles."""

    chain: list[BaseException] = []
    pending: BaseException | None = exc
    visited: set[int] = set()
    while pending is not None and id(pending) not in visited and len(chain) < 16:
        visited.add(id(pending))
        chain.append(pending)
        pending = pending.__cause__ or pending.__context__
    return tuple(chain)


def _source_line(exc: BaseException, chain: tuple[BaseException, ...]) -> int | None:
    """Find an explicit or traceback-derived source line."""

    for value in (getattr(exc, "source_line", None), getattr(exc, "line", None)):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    for current in reversed(chain):
        frames = traceback.extract_tb(current.__traceback__)
        for frame in reversed(frames):
            if frame.filename == "published_strategy" or "strategy" in Path(frame.filename).name.lower():
                return frame.lineno
    for current in reversed(chain):
        frames = traceback.extract_tb(current.__traceback__)
        if frames:
            return frames[-1].lineno
    return None


def _technical_detail(chain: tuple[BaseException, ...]) -> str:
    """Render stack metadata while omitting source lines and absolute paths."""

    lines: list[str] = []
    for current in chain:
        lines.append(f"{type(current).__name__}: {_redact(str(current))}")
        for frame in traceback.extract_tb(current.__traceback__):
            # Keep filename basename and line/function identity, but not the
            # source line itself: source text may contain credentials or data.
            lines.append(
                f'  File "{Path(frame.filename).name}", line {frame.lineno}, in {frame.name}'
            )
    return _redact("\n".join(lines))[:MAX_FAILURE_DETAIL_CHARS]


def build_failure_evidence(
    exc: BaseException,
    *,
    default_phase: str = "runner_worker",
) -> dict[str, Any]:
    """Build stable, bounded failure evidence for a run root.

    ``PhaseExecutionError`` carries the business phase and step while its
    cause carries the useful strategy exception.  This helper combines both
    without serializing arbitrary exception objects or raw source text.
    """

    if not isinstance(exc, BaseException):
        raise TypeError("exc must be an exception")
    chain = _exception_chain(exc)
    root = chain[-1] if chain else exc
    phase = getattr(exc, "phase_key", None) or getattr(exc, "failure_phase", None)
    if not isinstance(phase, str) or not phase.strip():
        phase = default_phase
    step = getattr(exc, "step_sequence", None)
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        step = getattr(root, "step_sequence", None)
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        step = None
    error_type = getattr(exc, "error_type", None)
    if not isinstance(error_type, str) or not error_type.strip():
        error_type = type(root).__name__
    message = _redact(str(root))[:1_800]
    technical_detail = _technical_detail(chain)
    return {
        "failure_phase": phase,
        "failure_step": step,
        "error_type": error_type,
        "message": message,
        "source_line": _source_line(exc, chain),
        "technical_detail": technical_detail,
        "traceback": technical_detail,
        "desensitized": True,
    }


__all__ = ["MAX_FAILURE_DETAIL_CHARS", "build_failure_evidence"]
