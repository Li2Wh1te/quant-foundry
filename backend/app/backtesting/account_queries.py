"""Read models for the account catalogue and historical formal-run usage."""

from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.backtesting.models import BacktestAccountProfileRecord as Account, BacktestRunRecord as Run
from app.backtesting.run_repository import FORMAL_KIND
from app.strategies.models import Strategy, StrategyRevision


class AccountProfileQueries:
    """Aggregate in PostgreSQL without loading a capped selector into memory."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _scope(owner_scope: str):
        # Failed/cancelled submissions still reference a configuration. Internal
        # acceptance runs and another owner's submissions must never count.
        return Run.run_kind == FORMAL_KIND, Run.idempotency_scope == owner_scope

    def page(self, *, status: str | None, keyword: str | None, limit: int, offset: int):
        """Apply identical literal-substring filters to records and total."""
        filters = []
        if status is not None:
            filters.append(Account.status == status)
        if keyword and keyword.strip():
            value = keyword.strip().lower()
            filters.append(or_(
                func.lower(Account.name).contains(value, autoescape=True),
                func.lower(Account.fee_schedule_key).contains(value, autoescape=True),
            ))
        total = self.session.scalar(select(func.count()).select_from(Account).where(*filters))
        rows = self.session.scalars(select(Account).where(*filters).order_by(
            func.lower(Account.name), Account.id,
        ).limit(limit).offset(offset))
        return list(rows), total

    def overview(self, *, owner_scope: str):
        """Catalogue totals are global; usage totals follow run visibility."""
        counts = self.session.execute(select(
            func.count().label("total_accounts"),
            func.count().filter(Account.status == "active").label("active_accounts"),
            func.count().filter(Account.status == "inactive").label("inactive_accounts"),
            func.count().filter(Account.status == "retired").label("retired_accounts"),
            func.coalesce(func.sum(func.jsonb_array_length(Account.fee_rules)), 0).label("total_fee_rules"),
        )).mappings().one()
        references = self.session.execute(select(
            func.count(func.distinct(func.nullif(Run.strategy_revision_id, ""))).label("related_strategy_versions"),
            func.count(func.distinct(Run.account_profile_id)).label("used_accounts"),
        ).select_from(Run).join(Account, cast(Account.id, String) == Run.account_profile_id)
            .where(*self._scope(owner_scope))).mappings().one()
        return dict(counts) | dict(references)

    def usage(self, profile_id: UUID, *, owner_scope: str, limit: int, offset: int):
        """Retain historical textual bindings, including unresolved revisions."""
        partition = [Run.account_profile_version, Run.strategy_revision_id]
        # Window functions select the latest complete run row per group, with
        # the UUID resolving timestamp ties deterministically. UUID casts apply
        # only to trusted model IDs, so malformed legacy bindings remain safe.
        ranked = select(
            Run.account_profile_version, Run.strategy_revision_id,
            Run.id.label("latest_run_id"), Run.created_at.label("latest_run_at"),
            Run.status.label("latest_run_status"),
            func.count().over(partition_by=partition).label("run_count"),
            func.row_number().over(partition_by=partition, order_by=[Run.created_at.desc(), Run.id.desc()]).label("position"),
        ).where(*self._scope(owner_scope), Run.account_profile_id == str(profile_id)).subquery()
        groups = select(ranked).where(ranked.c.position == 1).subquery()
        counts = self.session.execute(select(
            func.count().label("total"),
            func.coalesce(func.sum(groups.c.run_count), 0).label("total_runs"),
            func.count(func.distinct(func.nullif(groups.c.strategy_revision_id, ""))).label("related_strategy_versions"),
        )).mappings().one()
        items = self.session.execute(select(
            groups.c.account_profile_version, groups.c.strategy_revision_id,
            groups.c.run_count, groups.c.latest_run_id, groups.c.latest_run_at, groups.c.latest_run_status,
            Strategy.id.label("strategy_id"), Strategy.name.label("strategy_name"),
            StrategyRevision.revision_number,
        ).select_from(groups).outerjoin(StrategyRevision, cast(StrategyRevision.id, String) == groups.c.strategy_revision_id)
            .outerjoin(Strategy, Strategy.id == StrategyRevision.strategy_id)
            .order_by(groups.c.latest_run_at.desc(), groups.c.latest_run_id.desc())
            .limit(limit).offset(offset)).mappings().all()
        return dict(counts) | dict(items=[dict(row) for row in items], limit=limit, offset=offset)
