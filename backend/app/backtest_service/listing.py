"""One bounded list for new runs and unchanged legacy history."""
from datetime import datetime
from contextlib import nullcontext
from uuid import UUID
from sqlalchemy import String, and_, cast, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.strategies.models import StrategyRevision
from .cursors import decode, encode
from .history.runs import legacy_summary
from .history.storage_models import BacktestRunRecord
from .models import Run
from .repository import summary
from .runtime_contracts import RuntimeFault
from .serialization import encoded


def list_page(repository, owner, limit, cursor, key, *, strategy_id=None, session=None,
              strategy_revision_id=None, status=None, created_after=None, created_before=None):
    old, new = BacktestRunRecord.__table__, Run.__table__
    queries = []
    for table, engine, acl, revision in (
        (old, "legacy_python", [old.c.idempotency_scope == owner, old.c.run_kind == "backtest_run", old.c.profile == "formal@1"], old.c.strategy_revision_id),
        (new, "rust", [new.c.owner == owner], cast(new.c.strategy_revision_id, String)),
    ):
        q = select(table.c.id, table.c.created_at, literal(engine).label("engine_kind")).where(*acl)
        if strategy_revision_id is not None:
            q = q.where(revision == str(strategy_revision_id))
        if status is not None:
            q = q.where(table.c.status == status)
        if created_after is not None:
            q = q.where(table.c.created_at >= created_after)
        if created_before is not None:
            q = q.where(table.c.created_at <= created_before)
        if strategy_id is not None:
            q = q.join(StrategyRevision, cast(StrategyRevision.id, String) == revision).where(StrategyRevision.strategy_id == strategy_id)
        queries.append(q)
    both = union_all(*queries).subquery()
    scope = dict(owner=owner, limit=limit, strategy_id=str(strategy_id) if strategy_id else None,
                 revision=str(strategy_revision_id) if strategy_revision_id else None, status=status,
                 after=created_after.isoformat() if created_after else None,
                 before=created_before.isoformat() if created_before else None)
    q = select(both)
    if cursor:
        value = decode(cursor, scope, key)
        try:
            when, rid = datetime.fromisoformat(value[0]), UUID(value[1])
        except (ValueError, TypeError, IndexError):
            raise RuntimeFault("INVALID_CONTRACT", "游标边界无效") from None
        q = q.where(or_(both.c.created_at < when, and_(both.c.created_at == when, both.c.id < rid)))
    with (nullcontext(session) if session is not None else Session(repository.engine)) as session:
        rows = session.execute(q.order_by(both.c.created_at.desc(), both.c.id.desc()).limit(limit+1)).all()
        selected, items = rows[:limit], []
        # Two bounded set reads, independent of full history size.
        new_rows = {r.id: r for r in session.scalars(select(Run).where(Run.id.in_([r.id for r in selected if r.engine_kind == "rust"]))).all()}
        old_rows = {r.id: r for r in session.scalars(select(BacktestRunRecord).where(BacktestRunRecord.id.in_([r.id for r in selected if r.engine_kind != "rust"]))).all()}
        accepted, size = [], 16*1024  # Signed cursor and page envelope share the 1MiB budget.
        for row in selected:
            if row.engine_kind == "rust":
                root = new_rows[row.id]
                item = summary({c.name: getattr(root, c.name) for c in new.columns})
            else:
                item = legacy_summary(old_rows[row.id])
            # Lists carry status and small configuration, never whole results
            # or the old retained input snapshot. Those have bounded read URLs.
            for name in ("result_summary", "result_counts", "backtest_config"):
                item.pop(name, None)
            item_bytes = len(encoded(item))
            if size+item_bytes > 1024*1024:
                if not items:
                    raise RuntimeFault("RESOURCE_LIMIT", "运行摘要超过控制预算")
                break
            size += item_bytes+1
            items.append(item); accepted.append(row)
        selected = accepted
    more = len(rows) > len(selected)
    boundary = [selected[-1].created_at.isoformat(), str(selected[-1].id)] if selected else None
    return dict(items=items, has_more=more, next_cursor=encode(boundary, scope, key) if more else None)
