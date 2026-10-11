"""Database/API implementation of D12's fenced scheduling port.

Every operation owns a short transaction. Final success reuses the exact D04
connection, after D11 seal; no market rows or per-event progress are stored here.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import re
import time
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, or_, select, text, update

from .models import Run, RunBatch, RunResult
from .runtime_contracts import AcceptedWork, Claim, GroupBudget, RuntimeFault, TERMINAL, WorkerLimits
from .serialization import canonical_hash, encoded

R, B, P = Run.__table__, RunBatch.__table__, RunResult.__table__
MAX_WORK_BYTES = 1024 * 1024


def identity(value):
    try:
        parsed = UUID(str(value))
        if str(value) not in (str(parsed), parsed.hex):
            raise ValueError()
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise RuntimeFault("DATA_RESTRICTED", "运行不存在或无权访问") from None


def summary(row):
    outcome = row["outcome"] or {}
    return dict(run_id=str(row["id"]), engine_kind="rust", api_schema="qf.backtest.v2",
                status=row["status"], cancel_requested=row["cancel_requested"],
                progress_completed=0, progress_total=None,
                partial=outcome.get("partial", False), error=outcome.get("error"),
                batch_id=str(row["batch_id"]) if row["batch_id"] else None,
                retry_of=str(row["retry_of"]) if row["retry_of"] else None,
                account_version=row["account_version"],
                cost_configuration=row["accepted"]["account_snapshot"]["fee_schedule"],
                created_at=row["created_at"], finished_at=row["finished_at"],
                normalized_config=row["config"])


def same_config(accepted, requested):
    """Omitted calendar and its already accepted explicit value are equivalent."""
    if isinstance(accepted, list) and isinstance(requested, list):
        return len(accepted) == len(requested) and all(same_config(a, b) for a, b in zip(accepted, requested))
    if not isinstance(accepted, dict) or not isinstance(requested, dict):
        return False
    value = dict(requested)
    if "reference_calendar" not in value and "reference_calendar" in accepted:
        value["reference_calendar"] = accepted["reference_calendar"]
    return accepted == value


class DatabaseRunRepository:
    def __init__(self, engine, *, budget=None, queue_limit=1024, result_root=None):
        self.engine = engine
        self.budget = budget or GroupBudget()
        self.queue_limit = queue_limit
        self.result_root = result_root
        if type(queue_limit) is not int or not 1 <= queue_limit <= 10000:
            raise RuntimeFault("RESOURCE_LIMIT", "共享队列预算无效")

    @contextmanager
    def _transaction(self, connection=None, *, group=False):
        if connection is not None:
            if connection.engine is not self.engine or not connection.in_transaction():
                raise RuntimeFault("INVALID_CONTRACT", "终态必须使用同一数据网关事务")
            yield connection
            return
        with self.engine.connect() as c:
            # SQLite is an isolated portable fixture only. PostgreSQL row and
            # transaction locks arbitrate actual independent runner processes.
            if c.dialect.name == "sqlite":
                c.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                c.begin()
            try:
                if group:
                    if c.dialect.name == "postgresql":
                        c.execute(text("SELECT pg_advisory_xact_lock(731313,1)"))
                    policy = c.execute(select(R.c.group_policy).limit(1)).scalar_one_or_none()
                    if policy is not None and policy != vars(self.budget):
                        raise RuntimeFault("RESOURCE_LIMIT", "runner共享预算配置不一致")
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _row(self, c, run_id, owner=None, *, lock=False):
        q = select(R).where(R.c.id == identity(run_id))
        if owner is not None:
            q = q.where(R.c.owner == owner)
        if lock:
            q = q.with_for_update()
        row = c.execute(q).mappings().one_or_none()
        if row is None:
            raise RuntimeFault("DATA_RESTRICTED", "运行不存在或无权访问")
        return row

    @staticmethod
    def _valid(row, claim, now=None):
        return (row["status"] in ("starting", "running") and row["token"] == claim.token
                and row["fence"] == claim.fence and row["lease_until"] > (time.time() if now is None else now))

    @staticmethod
    def _key(key):
        if type(key) is not str or not 1 <= len(key) <= 200 or not key.strip() or any(ord(x) < 32 for x in key):
            raise RuntimeFault("INVALID_RUN_CONFIG", "需要有界的Idempotency-Key")
        return key

    @staticmethod
    def _submit_key(key):
        # Client keys cannot collide with code-owned batch/retry child keys.
        return "submit:" + canonical_hash(key)[7:]

    @staticmethod
    def _batch_configs(c, batch_id):
        return c.execute(select(R.c.config).where(R.c.batch_id == batch_id, R.c.retry_of.is_(None))
                         .order_by(R.c.batch_index).limit(256)).scalars().all()

    def existing(self, owner, key, request_hash, *, batch=False, config=None):
        self._key(key)
        key = key if batch else self._submit_key(key)
        table = B if batch else R
        with self.engine.connect() as c:
            row = c.execute(select(table).where(table.c.owner == owner, table.c.idempotency_key == key)).mappings().one_or_none()
            if row is not None and row["request_hash"] != request_hash and not same_config(
                    self._batch_configs(c, row["id"]) if batch else row["config"], config):
                raise RuntimeFault("IDEMPOTENCY_CONFLICT", "同一幂等键已绑定不同配置")
            return dict(row) if row else None

    def _values(self, work, key, request_hash, *, batch_id=None, index=None, retry_of=None):
        if (not work.owner or len(work.owner) > 128 or len(work.strategy_source.encode()) > 256 * 1024
                or "\x00" in work.strategy_source or not self.budget.fits([work.limits])
                or len(encoded(work.specification)) > MAX_WORK_BYTES):
            raise RuntimeFault("RESOURCE_LIMIT", "已受理控制配置超过预算")
        config = deepcopy(work.config)
        return dict(id=uuid4(), owner=work.owner, idempotency_key=key, request_hash=request_hash,
                    account_id=identity(config["account_id"]), account_version=work.specification["account_version"],
                    strategy_revision_id=identity(config["strategy_revision_id"]), batch_id=batch_id,
                    batch_index=index, retry_of=retry_of, config=config, accepted=deepcopy(work.specification),
                    strategy_source=work.strategy_source, limits=vars(work.limits), group_policy=vars(self.budget),
                    status="queued", cancel_requested=False, fence=0, lease_until=0)

    def submit(self, work, key, request_hash):
        key = self._submit_key(self._key(key))
        values = self._values(work, key, request_hash)
        with self._transaction(group=True) as c:
            row = c.execute(select(R).where(R.c.owner == work.owner, R.c.idempotency_key == key)).mappings().one_or_none()
            if row:
                if row["request_hash"] != request_hash and not same_config(row["config"], work.config):
                    raise RuntimeFault("IDEMPOTENCY_CONFLICT", "同一幂等键已绑定不同配置")
                return summary(row)
            self._capacity(c, 1)
            c.execute(insert(R).values(**values))
            return summary(self._row(c, values["id"], work.owner))

    def _capacity(self, c, count):
        active = c.execute(select(func.count()).select_from(R).where(R.c.status.not_in(TERMINAL))).scalar_one()
        if active + count > self.queue_limit:
            raise RuntimeFault("RESOURCE_LIMIT", "共享运行队列已满")

    def submit_batch(self, works, key, request_hash):
        self._key(key)
        if not 1 <= len(works) <= 256:
            raise RuntimeFault("RESOURCE_LIMIT", "参数批次须为1..256项")
        signature = lambda work: {k: v for k, v in work.config.items() if k != "parameters"}
        if any(w.owner != works[0].owner or signature(w) != signature(works[0])
               or w.strategy_source != works[0].strategy_source for w in works):
            raise RuntimeFault("INVALID_RUN_CONFIG", "参数批次只能改变参数")
        batch_id = uuid4()
        values = [self._values(w, "batch:"+batch_id.hex+":"+str(i), canonical_hash(w.config), batch_id=batch_id, index=i)
                  for i, w in enumerate(works)]
        with self._transaction(group=True) as c:
            row = c.execute(select(B).where(B.c.owner == works[0].owner, B.c.idempotency_key == key)).mappings().one_or_none()
            if row:
                if row["request_hash"] != request_hash and not same_config(self._batch_configs(c, row["id"]), [w.config for w in works]):
                    raise RuntimeFault("IDEMPOTENCY_CONFLICT", "同一幂等键已绑定不同批次")
                return str(row["id"])
            self._capacity(c, len(values))
            c.execute(insert(B).values(id=batch_id, owner=works[0].owner, idempotency_key=key, request_hash=request_hash))
            c.execute(insert(R), values)
            return str(batch_id)

    def status(self, owner, run_id):
        with self.engine.connect() as c:
            return summary(self._row(c, run_id, owner))

    def request_cancel(self, owner, run_id):
        with self._transaction() as c:
            row = self._row(c, run_id, owner, lock=True)
            if row["status"] not in TERMINAL:
                values = dict(cancel_requested=True)
                if row["status"] == "queued":
                    values.update(status="cancelled", outcome=dict(status="cancelled", partial=False), finished_at=datetime.now(timezone.utc))
                c.execute(update(R).where(R.c.id == row["id"]).values(**values))
            return summary(self._row(c, run_id, owner))

    def claim(self, supervisor_id, now, lease_seconds):
        with self._transaction(group=True) as c:
            active = c.execute(select(R.c.limits).where(R.c.status.in_(("starting", "running")))).scalars().all()
            row = c.execute(select(R).where(R.c.status == "queued", R.c.cancel_requested.is_(False))
                            .order_by(R.c.created_at, R.c.id).limit(1).with_for_update()).mappings().one_or_none()
            if row is None or not self.budget.fits([WorkerLimits(**x) for x in active]+[WorkerLimits(**row["limits"])]):
                return None
            token, fence = uuid4().hex, row["fence"] + 1
            c.execute(update(R).where(R.c.id == row["id"]).values(status="starting", token=token,
                      fence=fence, supervisor_id=supervisor_id, lease_until=now+lease_seconds, attempt_identity=None))
            return Claim(str(row["id"]), token, fence)

    def work(self, claim):
        with self.engine.connect() as c:
            row = self._row(c, claim.run_id)
            if not self._valid(row, claim) or row["cancel_requested"]:
                raise RuntimeFault("STALE_CLAIM", "运行认领已失效")
            return AcceptedWork(row["owner"], deepcopy(row["config"]), row["strategy_source"],
                                WorkerLimits(**row["limits"]), deepcopy(row["accepted"]))

    def _mutate_claim(self, claim, values, *, now=None, allow_cancel=False):
        with self._transaction() as c:
            row = self._row(c, claim.run_id, lock=True)
            if not self._valid(row, claim, now) or row["cancel_requested"] and not allow_cancel:
                return False
            c.execute(update(R).where(R.c.id == row["id"]).values(**values))
            return True

    def bind_attempt(self, claim, device, inode):
        return self._mutate_claim(claim, dict(attempt_identity=[device, inode]))

    def attempt_identity(self, claim):
        with self.engine.connect() as c:
            row = self._row(c, claim.run_id)
            if row["token"] != claim.token or row["fence"] != claim.fence:
                return None
            return tuple(row["attempt_identity"]) if row["attempt_identity"] is not None else None

    def heartbeat(self, claim, now, lease_seconds):
        return self._mutate_claim(claim, dict(lease_until=now+lease_seconds), now=now, allow_cancel=True)

    def running(self, claim):
        return self._mutate_claim(claim, dict(status="running"))

    def cancelled(self, claim):
        with self.engine.connect() as c:
            row = self._row(c, claim.run_id)
            return not self._valid(row, claim) or row["cancel_requested"]

    def commit_outcome(self, claim, outcome, result, connection=None, publish=None):
        from quantfoundry import _native
        outcome = __import__('json').loads(_native.TrustedResultWriter.validate_outcome_json(encoded(outcome).decode()))
        if outcome.get("status") not in TERMINAL:
            raise RuntimeFault("INVALID_CONTRACT", "终态无效")
        if outcome["status"] == "succeeded" and (connection is None or result is None or publish is None):
            raise RuntimeFault("INVALID_CONTRACT", "成功须先封存结果并使用最终依赖事务")
        if outcome["status"] == "succeeded" and outcome.get("result_id") != claim.run_id:
            raise RuntimeFault("INVALID_CONTRACT", "结果不属于本运行")
        if result is not None and (type(result) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result) is None):
            raise RuntimeFault("INVALID_CONTRACT", "结果引用无效")
        with self._transaction(connection) as c:
            row = self._row(c, claim.run_id, lock=True)
            if not self._valid(row, claim) or row["cancel_requested"] and outcome["status"] != "cancelled":
                return False
            # D11 retain seals/fsyncs before this callback. Its retain is only
            # an in-memory lifetime flip, not result I/O under D04's final guard.
            if publish is not None:
                if publish() != result:
                    raise RuntimeFault("INVALID_CONTRACT", "结果保留引用不一致")
            if result is not None:
                c.execute(insert(P).values(run_id=row["id"], directory=result))
            c.execute(update(R).where(R.c.id == row["id"]).values(status=outcome["status"],
                      outcome=outcome, lease_until=0, finished_at=datetime.now(timezone.utc)))
            return True

    def expired(self, now):
        with self.engine.connect() as c:
            rows = c.execute(select(R.c.id, R.c.token, R.c.fence).where(R.c.status.in_(("starting", "running")),
                              R.c.lease_until <= now).limit(self.budget.concurrency)).all()
            return [Claim(str(r.id), r.token, r.fence) for r in rows]

    def recover_stopped(self, claim, now):
        with self._transaction() as c:
            row = self._row(c, claim.run_id, lock=True)
            if row["token"] != claim.token or row["fence"] != claim.fence or row["lease_until"] > now or row["status"] in TERMINAL:
                return False
            values = dict(status="cancelled" if row["cancel_requested"] else "queued", token=None, lease_until=0, attempt_identity=None)
            if row["cancel_requested"]:
                values.update(outcome=dict(status="cancelled", partial=True), finished_at=datetime.now(timezone.utc))
            c.execute(update(R).where(R.c.id == row["id"]).values(**values))
            return True

    def batch_rows(self, owner, batch_id, *, offset=0, limit=100):
        with self.engine.connect() as c:
            batch_id = identity(batch_id)
            if c.execute(select(B.c.id).where(B.c.id == batch_id, B.c.owner == owner)).scalar_one_or_none() is None:
                raise RuntimeFault("DATA_RESTRICTED", "批次不存在或无权访问")
            # Attempts remain linked to the original item; latest attempt wins
            # presentation without erasing successful or failed business history.
            ranked = select(R.c.id, func.row_number().over(partition_by=R.c.batch_index,
                        order_by=(R.c.created_at.desc(), R.c.id.desc())).label("rank"))\
                        .where(R.c.batch_id == batch_id, R.c.owner == owner).subquery()
            rows = c.execute(select(R).join(ranked, ranked.c.id == R.c.id).where(ranked.c.rank == 1)
                     .order_by(R.c.batch_index).offset(offset).limit(limit+1)).mappings().all()
            return [summary(row) for row in rows[:limit]], len(rows) > limit

    def cancel_batch(self, owner, batch_id):
        rows, _ = self.batch_rows(owner, batch_id, limit=256)
        for row in rows:
            self.request_cancel(owner, row["run_id"])
        return str(identity(batch_id))

    def retry_failed(self, owner, batch_id, key):
        """New attempts only for failed items; original business results remain."""
        self._key(key)
        digest = canonical_hash(dict(batch_id=str(identity(batch_id)), operation="retry_failed"))
        prefix = "retry:" + canonical_hash(dict(owner=owner, key=key))[7:]
        with self._transaction(group=True) as c:
            bid = identity(batch_id)
            if c.execute(select(B.c.id).where(B.c.id == bid, B.c.owner == owner)).scalar_one_or_none() is None:
                raise RuntimeFault("DATA_RESTRICTED", "批次不存在或无权访问")
            previous = c.execute(select(R).where(R.c.owner == owner, R.c.idempotency_key.startswith(prefix))
                                 .order_by(R.c.batch_index).limit(256)).mappings().all()
            if previous:
                if any(r["request_hash"] != digest for r in previous):
                    raise RuntimeFault("IDEMPOTENCY_CONFLICT", "重跑幂等键已用于另一批次")
                return [str(r["id"]) for r in previous]
            ranked = select(R.c.id, func.row_number().over(partition_by=R.c.batch_index,
                        order_by=(R.c.created_at.desc(), R.c.id.desc())).label("rank"))\
                        .where(R.c.batch_id == bid, R.c.owner == owner).subquery()
            latest = c.execute(select(R).join(ranked, ranked.c.id == R.c.id).where(ranked.c.rank == 1)
                               .order_by(R.c.batch_index).limit(256).with_for_update(of=R)).mappings().all()
            failed = [r for r in latest if r["status"] == "failed"]
            self._capacity(c, len(failed))
            values = [self._values(AcceptedWork(r["owner"], r["config"], r["strategy_source"], WorkerLimits(**r["limits"]), r["accepted"]),
                       prefix+":"+str(r["batch_index"]), digest, batch_id=bid, index=r["batch_index"], retry_of=r["id"])
                      for r in failed]
            if values:
                c.execute(insert(R), values)
            return [str(r["id"]) for r in values]

    def result_pointer(self, owner, run_id):
        with self.engine.connect() as c:
            row = self._row(c, run_id, owner)
            pointer = c.execute(select(P.c.directory).where(P.c.run_id == row["id"])).scalar_one_or_none()
            return summary(row), pointer
