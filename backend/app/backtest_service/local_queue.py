"""Bounded run-root scheduling reference for isolated tests and D13 port integration.

Not an HTTP, account/history or database repository. D13 owns those services.
flock arbitrates different supervisor processes; result directories are retained
business outputs. This implementation never loads or executes a user module.
"""

import fcntl
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .runtime_contracts import (
    TERMINAL,
    AcceptedWork,
    Claim,
    GroupBudget,
    RuntimeFault,
    WorkerLimits,
)

_ID = re.compile(r"[0-9a-f]{32}")
MAX_WORK_BYTES = 1024 * 1024


def encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class LocalRunRepository:
    def __init__(self, root, budget=GroupBudget(), *, queue_limit=256):
        self.root = Path(root)
        self.budget, self.queue_limit = budget, queue_limit
        if (
            type(queue_limit) is not int
            or not 1 <= queue_limit <= 1024
            or not self.root.is_dir()
            or self.root.is_symlink()
        ):
            raise RuntimeFault("INVALID_CONTRACT", "队列根或容量无效")
        self.identity = self.root.stat()
        with self._locked():
            expected = dict(budget=asdict(budget), queue_limit=queue_limit)
            policy = self._read("policy.json")
            if policy is None:
                self._write("policy.json", expected)
            elif policy != expected:
                raise RuntimeFault(
                    "RESOURCE_LIMIT", "共享supervisor必须使用同一组总预算"
                )

    def _id(self, value):
        if type(value) is not str or not _ID.fullmatch(value):
            raise RuntimeFault("INVALID_CONTRACT", "运行身份无效")
        return value

    @contextmanager
    def _locked(self):
        current = self.root.stat()
        if self.root.is_symlink() or (current.st_dev, current.st_ino) != (
            self.identity.st_dev,
            self.identity.st_ino,
        ):
            raise RuntimeFault("INVALID_CONTRACT", "队列根已被替换")
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise RuntimeFault("INVALID_CONTRACT", "队列锁不是私有常规文件")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, name, default=None):
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return default
        with os.fdopen(fd, "rb") as file:
            if (
                not stat.S_ISREG(os.fstat(file.fileno()).st_mode)
                or os.fstat(file.fileno()).st_nlink != 1
            ):
                raise RuntimeFault("INVALID_CONTRACT", "队列状态文件无效")
            data = file.read(MAX_WORK_BYTES + 1)
        if len(data) > MAX_WORK_BYTES:
            raise RuntimeFault("RESOURCE_LIMIT", "队列状态超过预算")
        return json.loads(data)

    def _write(self, name, value):
        data = encoded(value)
        if len(data) > MAX_WORK_BYTES:
            raise RuntimeFault("RESOURCE_LIMIT", "单运行控制状态超过预算")
        temporary = self.root / (".write-" + uuid4().hex)
        fd = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            target = self.root / name
            if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
                raise RuntimeFault("INVALID_CONTRACT", "队列目标已被替换")
            os.replace(temporary, target)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _row(self, run_id):
        row = self._read(self._id(run_id) + ".json")
        if row is None:
            raise RuntimeFault("DATA_RESTRICTED", "运行不存在或无权读取")
        return row

    def _active(self):
        ids = self._read("active.json", [])
        if len(ids) > self.queue_limit:
            raise RuntimeFault("RESOURCE_LIMIT", "共享队列超过预算")
        return [self._row(run_id) for run_id in ids]

    def _save_active(self, rows):
        self._write(
            "active.json",
            [row["run_id"] for row in rows if row["status"] not in TERMINAL],
        )

    def enqueue_batch(self, items):
        if not 1 <= len(items) <= 256:
            raise RuntimeFault("RESOURCE_LIMIT", "参数批次须为1..256项")
        signature = lambda w: {k: v for k, v in w.config.items() if k != "parameters"}
        first = signature(items[0])
        if any(
            signature(w) != first
            or w.owner != items[0].owner
            or w.strategy_source != items[0].strategy_source
            or w.limits != items[0].limits
            for w in items
        ):
            raise RuntimeFault("INVALID_CONTRACT", "批次只能改变独立参数")
        # Rust normalization before admission; no module import.
        from quantfoundry import _native

        work_rows = []
        for work in items:
            config = json.loads(
                _native.validate_run_config_json(encoded(work.config).decode())
            )
            if not self.budget.fits([work.limits]):
                raise RuntimeFault("RESOURCE_LIMIT", "单运行不能放入总预算")
            if (
                len(work.strategy_source.encode()) > 256 * 1024
                or "\x00" in work.strategy_source
                or work.specification.get("config") != work.config
            ):
                raise RuntimeFault("INVALID_CONTRACT", "已接受策略或配置无效")
            work_rows.append(
                dict(
                    owner=work.owner,
                    config=config,
                    strategy_source=work.strategy_source,
                    limits=asdict(work.limits),
                    specification=work.specification,
                )
            )
        with self._locked():
            active = self._active()
            if len(active) + len(items) > self.queue_limit:
                raise RuntimeFault("RESOURCE_LIMIT", "共享队列已满")
            batch_id, ids, accepted = uuid4().hex, [], []
            for index, work in enumerate(work_rows):
                run_id = uuid4().hex
                row = dict(
                    run_id=run_id,
                    status="queued",
                    cancel_requested=False,
                    fence=0,
                    token=None,
                    lease_until=0,
                    batch_id=batch_id,
                    item=index,
                    retry_of=None,
                    work=work,
                    result=None,
                    outcome=None,
                )
                if len(encoded(row)) > MAX_WORK_BYTES:
                    raise RuntimeFault("RESOURCE_LIMIT", "单运行控制状态超过预算")
                accepted.append(row)
                ids.append(run_id)
            # Validate every child before writing any of this accepted batch.
            for row in accepted:
                self._write(row["run_id"] + ".json", row)
            active.extend(accepted)
            self._write(batch_id + ".batch", dict(owner=items[0].owner, items=ids))
            self._save_active(active)
            return batch_id, ids

    def _valid(self, row, claim, now=None):
        now = time.time() if now is None else now
        return (
            row["status"] in ("starting", "running")
            and row["token"] == claim.token
            and row["fence"] == claim.fence
            and row["lease_until"] > now
        )

    def claim(self, supervisor_id, now, lease_seconds):
        with self._locked():
            rows = self._active()
            # Expired workers still consume reservations until confirmed stopped.
            grants = [
                WorkerLimits(**r["work"]["limits"])
                for r in rows
                if r["status"] in ("starting", "running")
            ]
            for row in rows:
                if row["status"] != "queued" or row["cancel_requested"]:
                    continue
                limits = WorkerLimits(**row["work"]["limits"])
                if not self.budget.fits(grants + [limits]):
                    continue
                row.update(
                    status="starting",
                    fence=row["fence"] + 1,
                    token=uuid4().hex,
                    supervisor_id=supervisor_id,
                    lease_until=now + lease_seconds,
                )
                self._write(row["run_id"] + ".json", row)
                return Claim(row["run_id"], row["token"], row["fence"])
            return None

    def work(self, claim):
        with self._locked():
            row = self._row(claim.run_id)
            if not self._valid(row, claim):
                raise RuntimeFault("STALE_CLAIM", "claim已失效")
            value = row["work"]
            return AcceptedWork(
                value["owner"],
                value["config"],
                value["strategy_source"],
                WorkerLimits(**value["limits"]),
                value["specification"],
            )

    def bind_attempt(self, claim, device, inode):
        with self._locked():
            row = self._row(claim.run_id)
            if not self._valid(row, claim):
                return False
            row["attempt_identity"] = [device, inode]
            self._write(row["run_id"] + ".json", row)
            return True

    def attempt_identity(self, claim):
        with self._locked():
            row = self._row(claim.run_id)
            if row["token"] != claim.token or row["fence"] != claim.fence:
                return None
            value = row.get("attempt_identity")
            return tuple(value) if value is not None else None

    def heartbeat(self, claim, now, lease_seconds):
        with self._locked():
            row = self._row(claim.run_id)
            if not self._valid(row, claim, now):
                return False
            row["lease_until"] = now + lease_seconds
            self._write(row["run_id"] + ".json", row)
            return True

    def running(self, claim):
        with self._locked():
            row = self._row(claim.run_id)
            if not self._valid(row, claim) or row["cancel_requested"]:
                return False
            row["status"] = "running"
            self._write(row["run_id"] + ".json", row)
            return True

    def cancelled(self, claim):
        with self._locked():
            row = self._row(claim.run_id)
            return not self._valid(row, claim) or row["cancel_requested"]

    def status(self, owner, run_id):
        with self._locked():
            row = self._row(run_id)
            if row["work"]["owner"] != owner:
                raise RuntimeFault("DATA_RESTRICTED", "无权读取运行")
            return {
                k: v
                for k, v in row.items()
                if k not in ("work", "token", "supervisor_id")
            }

    def request_cancel(self, owner, run_id):
        with self._locked():
            row = self._row(run_id)
            if row["work"]["owner"] != owner:
                raise RuntimeFault("DATA_RESTRICTED", "无权取消运行")
            if row["status"] not in TERMINAL:
                row["cancel_requested"] = True
                if row["status"] == "queued":
                    row.update(
                        status="cancelled",
                        outcome=dict(status="cancelled", partial=False),
                    )
                self._write(row["run_id"] + ".json", row)
                self._save_active(self._active())
        return self.status(owner, run_id)

    def commit_outcome(self, claim, outcome, result, connection=None, publish=None):
        if outcome.get("status") not in TERMINAL:
            raise RuntimeFault("INVALID_CONTRACT", "终态无效")
        with self._locked():
            row = self._row(claim.run_id)
            if not self._valid(row, claim):
                return False
            if row["cancel_requested"] and outcome["status"] != "cancelled":
                return False
            # Retention happens before durable pointer, within the terminal lock.
            # A crash may leave an orphan result, never a published missing result.
            if publish is not None:
                publish()
            row.update(
                status=outcome["status"], outcome=outcome, result=result, lease_until=0
            )
            self._write(row["run_id"] + ".json", row)
            self._save_active(self._active())
            return True

    def expired(self, now):
        with self._locked():
            return [
                Claim(r["run_id"], r["token"], r["fence"])
                for r in self._active()
                if r["status"] in ("starting", "running") and r["lease_until"] <= now
            ]

    def recover_stopped(self, claim, now):
        with self._locked():
            row = self._row(claim.run_id)
            if (
                row["token"] != claim.token
                or row["fence"] != claim.fence
                or row["lease_until"] > now
                or row["status"] in TERMINAL
            ):
                return False
            row.update(
                status="cancelled" if row["cancel_requested"] else "queued",
                token=None,
                lease_until=0,
                attempt_identity=None,
            )
            if row["cancel_requested"]:
                row["outcome"] = dict(status="cancelled", partial=True)
            self._write(row["run_id"] + ".json", row)
            self._save_active(self._active())
            return True

    def retry_failed(self, owner, batch_id):
        # New run for each failed item; successes and their result pointers survive.
        with self._locked():
            batch = self._read(self._id(batch_id) + ".batch")
            if batch is None or batch["owner"] != owner:
                raise RuntimeFault("DATA_RESTRICTED", "无权重跑批次")
            active = self._active()
            failed = [
                (index, self._row(run_id))
                for index, run_id in enumerate(batch["items"])
            ]
            failed = [
                (index, row) for index, row in failed if row["status"] == "failed"
            ]
            if len(active) + len(failed) > self.queue_limit:
                raise RuntimeFault("RESOURCE_LIMIT", "共享队列已满")
            replacements = []
            for index, row in failed:
                replacement = dict(
                    row,
                    run_id=uuid4().hex,
                    status="queued",
                    cancel_requested=False,
                    fence=0,
                    token=None,
                    lease_until=0,
                    retry_of=row["run_id"],
                    attempt_identity=None,
                    supervisor_id=None,
                    result=None,
                    outcome=None,
                )
                self._write(replacement["run_id"] + ".json", replacement)
                batch["items"][index] = replacement["run_id"]
                active.append(replacement)
                replacements.append(replacement["run_id"])
            self._write(batch_id + ".batch", batch)
            self._save_active(active)
            return replacements

    def batch_status(self, owner, batch_id, *, offset=0, limit=100):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 256
        ):
            raise RuntimeFault("INVALID_CONTRACT", "分页参数无效")
        with self._locked():
            batch = self._read(self._id(batch_id) + ".batch")
            if batch is None or batch["owner"] != owner:
                raise RuntimeFault("DATA_RESTRICTED", "无权读取批次")
            ids = batch["items"][offset : offset + limit]
            total = len(batch["items"])
        return dict(
            items=[self.status(owner, i) for i in ids],
            next_offset=offset + len(ids) if offset + len(ids) < total else None,
        )

    def cancel_batch(self, owner, batch_id):
        ids = [
            r["run_id"] for r in self.batch_status(owner, batch_id, limit=256)["items"]
        ]
        for run_id in ids:
            self.request_cancel(owner, run_id)
