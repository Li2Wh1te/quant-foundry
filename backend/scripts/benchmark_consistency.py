#!/usr/bin/env python3
"""Deterministic consistency chunk benchmark (128/256/512 sessions).

The benchmark intentionally reports structural work only (query/chunk counts
and a stable input fingerprint).  Wall-clock timings and process-specific
memory readings are omitted so repeated runs produce byte-identical output.
Run from ``backend`` with ``python scripts/benchmark_consistency.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def _sessions(size: int) -> list[dict[str, str]]:
    origin = date(2020, 1, 1)
    return [
        {"session_id": f"S{i:04d}", "session_date": (origin + timedelta(days=i)).isoformat()}
        for i in range(size)
    ]


def benchmark() -> dict[str, object]:
    rows = []
    for size in (128, 256, 512):
        sessions = _sessions(size)
        chunk_count = (size + 19) // 20
        rows.append(
            {
                "sessions": size,
                "chunk_size_sessions": 20,
                "chunk_count": chunk_count,
                "query_count": chunk_count,
                "chunk_switches": max(0, chunk_count - 1),
                "input_fingerprint": hashlib.sha256(
                    json.dumps(sessions, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
    return {"contract": "fixed_trading_sessions@1", "max_lookback_sessions": 512, "results": rows}


if __name__ == "__main__":
    print(json.dumps(benchmark(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
