"""Worker-only container entry. No database/backend/daemon imports."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import socket
import sys
from pathlib import Path

import numpy as np

from . import _native
from ._runtime_ipc import RuntimeChannel, RuntimeClient
from .data import _error, _json, _MarketRoute


class _Output:
    def __init__(self, client, maximum):
        self.client, self.maximum, self.used, self.truncated = client, maximum, 0, False

    def write(self, text):
        data = str(text).encode("utf-8", errors="replace")
        remaining = max(0, self.maximum - self.used)
        if remaining:
            # Each response is acknowledged before another bounded message.
            for offset in range(0, min(len(data), remaining), 4096):
                part = data[offset : min(offset + 4096, remaining)].decode(
                    "utf-8", errors="replace"
                )
                self.client.call("output", dict(message=part, truncated=False))
            self.used += min(len(data), remaining)
        if len(data) > remaining and not self.truncated:
            self.truncated = True
            self.client.call(
                "output", dict(message="工作进程标准输出已截断", truncated=True)
            )
        return len(text)

    def flush(self):
        pass


def _transfer(result, client):
    summary = json.loads(result.summary_json())
    sequence = 1
    # D11 envelopes are re-indexed in this deterministic transfer order. Each
    # public partition retains its own time/order; no market events are copied.
    for kind, record_kind in [
        ("metadata", None),
        ("equity", "equity"),
        ("orders", "order"),
        ("trades", "trade"),
        ("positions", "position"),
        ("records", "record"),
        ("logs", "log"),
    ]:
        cursor = None
        while True:
            page = json.loads(
                result._native.metadata_page_json(cursor, 64)
                if kind == "metadata"
                else result.page_json(kind, cursor, 64)
            )
            records = []
            for row in page["items"]:
                row.pop("result_sequence")
                actual = record_kind or (
                    "corporate_action" if "effect" in row else "execution_model"
                )
                records.append(dict(kind=actual, record=row))
            if records:
                # Shared Rust decoder and trusted D11 writer validate credit,
                # enums and record bounds; one acknowledged batch is in flight.
                client.call("result", dict(records=records))
                sequence += len(records)
            cursor = page["next_cursor"]
            if cursor is None:
                break
    performance = summary.get("results", {}).get("performance")
    if performance is not None:
        client.call("result", dict(records=[dict(kind="summary", record=performance)]))
    return summary


def main():
    from ._worker import _run_module

    channel = None
    result = None
    original = (sys.stdout, sys.stderr)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect("/run/qf/ipc/control.sock")
        channel = RuntimeChannel(sock, arrow_budget=1, timeout=30)
        # The server binds this private endpoint to one attempt, not a token
        # supplied by user code. The opaque identity file contains no credentials.
        run_id = Path("/run/qf/code/run-id").read_text()
        client = RuntimeClient(channel, run_id)
        packet = client.call("boot", {}).body
        spec = packet["spec"]
        if spec["run_id"] != run_id or spec["source"].get("socket_path") not in (
            None,
            "/run/qf/ipc/data.sock",
        ):
            raise _error("INVALID_CONTRACT", "worker收到越界端点")
        path = Path("/run/qf/code/strategy.py")
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != packet["strategy_sha256"]:
            raise _error("INVALID_CONTRACT", "daemon工作根映射与已接受策略不一致")
        spec["config"] = json.loads(
            _native.validate_run_config_json(_json(spec["config"]))
        )
        random.seed(spec["config"]["seed"])
        np.random.seed(spec["config"]["seed"])
        sys.stdout = sys.stderr = _Output(client, packet["output_bytes"])
        module_spec = importlib.util.spec_from_file_location("_qf_user_strategy", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        routes = {
            name: _MarketRoute(row["binding"], tuple(row["fields"]))
            for name, row in packet["market_routes"].items()
        }
        result = _run_module(
            module,
            spec,
            market_routes=routes,
            research_routes=packet["research_routes"],
            _seeded=True,
        )
        summary = _transfer(result, client)
        notices = summary.get("results", {}).get("notices")
        if notices is not None:
            client.call("notices", dict(notices=notices))
        client.call("finished", dict(outcome=summary["outcome"]))
        return 0
    except BaseException as error:
        if channel is not None and not channel.closed:
            try:
                diagnostic = json.loads(_native.strategy_error_json(error))
                outcome = (
                    dict(status="cancelled", partial=True)
                    if diagnostic["code"] == "CANCELLED"
                    else dict(status="failed", error=diagnostic, partial=True)
                )
                client.call("finished", dict(outcome=outcome))
            except BaseException:
                pass
        return 1
    finally:
        sys.stdout, sys.stderr = original
        if result is not None:
            result.close()
        if channel is not None:
            channel.close()
