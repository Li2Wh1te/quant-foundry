"""D04's sole private IPC framing. D12 supplies an already run-bound socket.

No pickle, paths, SQL, DSNs or Python callbacks are accepted. Each pull grants
one bounded Arrow IPC stream; the next pull is the acknowledgement/credit.
This module has no Arrow/database dependency and is not a security sandbox.
"""
from __future__ import annotations

import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

PROTOCOL = "qf.ipc.v1"
MAX_CONTROL_BYTES = 1024 * 1024
MAX_ARROW_BYTES = 64 * 1024 * 1024
PREFIX = struct.Struct("!4sIQ")
OPS = frozenset({"open_stream", "next", "check", "close_stream", "cancel", "close"})
STATUSES = frozenset({"request", "ok", "batch", "eof", "error"})


class TransportError(ValueError):
    def __init__(self, code="INVALID_CONTRACT", message="私有数据通道契约无效"):
        self.code, self.operation, self.message, self.scope = code, "data_ipc", message, {}
        super().__init__(message)


@dataclass(frozen=True)
class Frame:
    run_id: str
    request_id: int
    op: str
    status: str
    body: dict
    payload: bytes = b""


def _label(value):
    return (type(value) is str and 0 < len(value.encode()) <= 128
            and value.strip() == value and not any(ord(c) < 32 for c in value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportError()
        result[key] = value
    return result


def _validate(item, payload_size):
    if (type(item) is not dict or set(item) !=
            {"protocol", "run_id", "request_id", "op", "status", "body", "payload_bytes"}
            or item["protocol"] != PROTOCOL or not _label(item["run_id"])
            or type(item["request_id"]) is not int or not 0 <= item["request_id"] < 2**53
            or item["op"] not in OPS or item["status"] not in STATUSES
            or type(item["body"]) is not dict or type(item["payload_bytes"]) is not int
            or item["payload_bytes"] != payload_size
            or (payload_size and item["status"] != "batch")):
        raise TransportError()


class Channel:
    """One in-flight request, finite wall time, cooperative cancellation."""
    frame_type = Frame
    error_type = TransportError
    def __init__(self, sock: socket.socket, *, arrow_budget=MAX_ARROW_BYTES,
                 timeout=30.0, cancelled: Callable[[], bool] | None = None):
        if (type(arrow_budget) is not int or not 1 <= arrow_budget <= MAX_ARROW_BYTES
                or not 0 < timeout <= 3600):
            raise TransportError("RESOURCE_LIMIT")
        self.sock, self.arrow_budget, self.timeout = sock, arrow_budget, timeout
        self.cancelled = cancelled or (lambda: False)
        self.closed = False

    def _io(self, value, *, sending, deadline):
        position = 0
        while position < len(value):
            if self.closed or self.sock.fileno() < 0:
                raise TransportError('CANCELLED', '数据通道已断开')
            if self.cancelled():
                raise TransportError("CANCELLED", "数据通道已取消")
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TransportError("RESOURCE_LIMIT", "数据通道达到等待上限")
            try:
                ready = select.select([] if sending else [self.sock],
                                      [self.sock] if sending else [], [], min(.05, remaining))
                if not ready[1 if sending else 0]:
                    continue
                if sending:
                    count = self.sock.send(value[position:position+65536], socket.MSG_DONTWAIT)
                else:
                    count = self.sock.recv_into(value[position:position+65536],
                                                min(65536, len(value)-position), socket.MSG_DONTWAIT)
                if not count:
                    raise TransportError("CANCELLED", "数据通道已断开")
                position += count
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                raise TransportError("CANCELLED", "数据通道已断开") from None

    def send(self, frame: Frame):
        try:
            item = dict(protocol=PROTOCOL, run_id=frame.run_id, request_id=frame.request_id,
                        op=frame.op, status=frame.status, body=frame.body, payload_bytes=len(frame.payload))
            _validate(item, len(frame.payload))
            encoded = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_CONTROL_BYTES or len(frame.payload) > self.arrow_budget:
                raise TransportError("RESOURCE_LIMIT")
            deadline = time.monotonic()+self.timeout
            self._io(memoryview(PREFIX.pack(b"QFD4", len(encoded), len(frame.payload))), sending=True, deadline=deadline)
            self._io(memoryview(encoded), sending=True, deadline=deadline)
            self._io(memoryview(frame.payload), sending=True, deadline=deadline)
        except (TransportError, TypeError, ValueError, RecursionError):
            self.close()
            raise

    def receive(self) -> Frame:
        try:
            deadline = time.monotonic()+self.timeout
            prefix = bytearray(PREFIX.size)
            self._io(memoryview(prefix), sending=False, deadline=deadline)
            magic, head_len, payload_len = PREFIX.unpack(prefix)
            # Reject BEFORE allocating either attacker-provided length.
            if magic != b"QFD4" or not 0 < head_len <= MAX_CONTROL_BYTES or payload_len > self.arrow_budget:
                raise TransportError("RESOURCE_LIMIT")
            encoded = bytearray(head_len)
            self._io(memoryview(encoded), sending=False, deadline=deadline)
            item = json.loads(encoded, object_pairs_hook=_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(TransportError()))
            _validate(item, payload_len)
            payload = bytearray(payload_len)
            self._io(memoryview(payload), sending=False, deadline=deadline)
            return Frame(item["run_id"], item["request_id"], item["op"], item["status"], item["body"], bytes(payload))
        except TransportError:
            self.close()
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self.close()
            raise TransportError() from None

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()

    def is_disconnected(self):
        if self.closed or self.sock.fileno() < 0:
            return True
        poll = select.poll()
        poll.register(self.sock, select.POLLHUP | select.POLLERR | select.POLLNVAL)
        return bool(poll.poll(0))


class Client:
    def __init__(self, channel: Channel, run_id: str):
        if not _label(run_id):
            raise TransportError()
        self.channel, self.run_id, self.sequence = channel, run_id, 0

    def call(self, op: str, body: dict) -> Frame:
        self.sequence += 1
        self.channel.send(Frame(self.run_id, self.sequence, op, "request", body))
        response = self.channel.receive()
        if (response.run_id, response.request_id, response.op) != (self.run_id, self.sequence, op):
            self.channel.close()
            raise TransportError()
        allowed = {'batch', 'eof', 'error'} if op == 'next' else {'ok', 'error'}
        if response.status not in allowed:
            self.channel.close()
            raise TransportError()
        if response.status == "error":
            # Gateway errors are terminal; do not retain our endpoint while
            # waiting for the peer's already initiated shutdown or caller GC.
            self.channel.close()
            if set(response.body) != {'code', 'message', 'operation', 'scope'}:
                raise TransportError()
            if (any(type(response.body[k]) is not str for k in ('code','message','operation'))
                    or type(response.body['scope']) is not dict
                    or any(type(v) is not str for v in response.body['scope'].values())):
                raise TransportError()
            error = TransportError(response.body["code"], response.body["message"])
            error.operation, error.scope = response.body["operation"], response.body["scope"]
            raise error
        return response
