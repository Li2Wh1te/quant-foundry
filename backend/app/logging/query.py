from __future__ import annotations

import gzip
import json
import os
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.core.logging import LOG_FILE_NAME


StatusClass = Literal["2xx", "3xx", "4xx", "5xx"]
VISIBILITY_FILE_NAME = ".visibility.json"


@dataclass(frozen=True)
class LogFilters:
    keyword: str | None = None
    level: str | None = None
    method: str | None = None
    status_class: StatusClass | None = None
    path: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class LocalLogQuery:
    def __init__(self, log_dir: Path, max_files: int) -> None:
        self.log_dir = log_dir
        self.max_files = max_files

    def search(self, filters: LogFilters, limit: int) -> dict[str, Any]:
        items: deque[dict[str, Any]] = deque(maxlen=limit)
        methods: Counter[str] = Counter()
        status_classes: Counter[str] = Counter()
        paths: Counter[str] = Counter()
        levels: Counter[str] = Counter()
        matched_count = 0
        files = self._log_files(filters.start_time)

        for entry in self._iter_entries(files):
            if not self._matches(entry, filters):
                continue
            matched_count += 1
            items.append(entry)
            self._count(entry, "method", methods)
            self._count(entry, "path", paths)
            level = entry.get("level")
            if isinstance(level, str) and level:
                levels[level.upper()] += 1
            status_code = entry.get("status_code")
            if isinstance(status_code, int) and 200 <= status_code <= 599:
                status_classes[f"{status_code // 100}xx"] += 1

        ordered_items = sorted(
            items,
            key=self._sort_key,
            reverse=True,
        )
        return {
            "items": ordered_items,
            "matched_count": matched_count,
            "truncated": matched_count > limit,
            "scanned_files": len(files),
            "facets": {
                "levels": dict(levels.most_common()),
                "methods": dict(methods.most_common()),
                "status_classes": dict(status_classes.most_common()),
                "paths": dict(paths.most_common()),
            },
        }

    def clear_visible_logs(self) -> datetime:
        visible_after = datetime.now(UTC)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        target = self.log_dir / VISIBILITY_FILE_NAME
        temporary = self.log_dir / (
            f"{VISIBILITY_FILE_NAME}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"visible_after": visible_after.isoformat()}),
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return visible_after

    def _log_files(self, start_time: datetime | None) -> list[Path]:
        if not self.log_dir.exists():
            return []
        candidates = [
            path
            for path in self.log_dir.glob(f"{LOG_FILE_NAME}*")
            if path.is_file() and not path.name.endswith(".lock")
        ]
        if start_time is not None:
            # A rotated file's mtime is close to the end of the period it contains.
            threshold = start_time.timestamp() - 86_400
            candidates = [
                path
                for path in candidates
                if path.name == LOG_FILE_NAME or path.stat().st_mtime >= threshold
            ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        selected = candidates[: self.max_files]
        selected.sort(key=lambda path: path.stat().st_mtime)
        return selected

    def _iter_entries(self, files: list[Path]) -> Iterator[dict[str, Any]]:
        visible_after = self._visible_after()
        for path in files:
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(entry, dict):
                            continue
                        timestamp = self._parse_timestamp(entry.get("timestamp"))
                        if visible_after is None or (
                            timestamp is not None and timestamp >= visible_after
                        ):
                            yield entry
            except (FileNotFoundError, OSError):
                # A concurrent rollover can replace a file between listing and opening.
                continue

    def _visible_after(self) -> datetime | None:
        try:
            data = json.loads(
                (self.log_dir / VISIBILITY_FILE_NAME).read_text(encoding="utf-8")
            )
            return self._parse_timestamp(data.get("visible_after"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
            return None

    @classmethod
    def _matches(cls, entry: dict[str, Any], filters: LogFilters) -> bool:
        timestamp = cls._parse_timestamp(entry.get("timestamp"))
        if filters.start_time and (timestamp is None or timestamp < filters.start_time):
            return False
        if filters.end_time and (timestamp is None or timestamp > filters.end_time):
            return False
        if filters.level and str(entry.get("level", "")).upper() != filters.level:
            return False
        if filters.method and str(entry.get("method", "")).upper() != filters.method:
            return False
        if filters.path and entry.get("path") != filters.path:
            return False
        if filters.status_class:
            status_code = entry.get("status_code")
            expected_class = int(filters.status_class[0])
            if not isinstance(status_code, int) or status_code // 100 != expected_class:
                return False
        if filters.keyword:
            serialized = json.dumps(entry, ensure_ascii=False).casefold()
            if filters.keyword.casefold() not in serialized:
                return False
        return True

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    @staticmethod
    def _count(entry: dict[str, Any], key: str, counter: Counter[str]) -> None:
        value = entry.get(key)
        if isinstance(value, str) and value:
            counter[value] += 1

    @staticmethod
    def _sort_key(entry: dict[str, Any]) -> tuple[str, int]:
        sequence = entry.get("sequence")
        return (
            str(entry.get("timestamp", "")),
            sequence if isinstance(sequence, int) else 0,
        )
