"""Add runner supervision evidence and durable queue-capacity guards.

The first backtest-runs migration created the task-08 configuration root.  This
revision is deliberately additive: it preserves that root and its result
foreign keys while adding the fields owned by the runner/supervisor boundary.
Queue guard rows are permanent coordination records; they are not an
application-level mutex and are locked by each creation transaction.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260831_02"
down_revision: str | None = "20260831_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUNNER_JSON = postgresql.JSONB(astext_type=sa.Text())


_ADDITIVE_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("idempotency_scope", sa.String(128), nullable=False, server_default="default"),
    sa.Column("current_trading_date", sa.Date(), nullable=True),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("launch_id", sa.Uuid(), nullable=True),
    sa.Column("worker_id", sa.String(128), nullable=True),
    sa.Column("child_start_identity", sa.String(128), nullable=True),
    sa.Column("child_process_group_id", sa.Integer(), nullable=True),
    sa.Column("worker_handshake_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_progress_persisted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("termination_requested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("termination_reason", sa.String(128), nullable=True),
    sa.Column("runner_exit_code_protocol", sa.String(64), nullable=True),
    sa.Column("runner_exit_category", sa.String(32), nullable=True),
    sa.Column("runner_exit_report", RUNNER_JSON, nullable=True),
    sa.Column("stdout_bytes", sa.Integer(), nullable=True),
    sa.Column("stdout_digest", sa.String(128), nullable=True),
    sa.Column("stdout_truncated", sa.Boolean(), nullable=True),
    sa.Column("resource_limit_evidence", RUNNER_JSON, nullable=True),
    sa.Column("runner_config_evidence", RUNNER_JSON, nullable=True),
    sa.Column("completion_marker_protocol", sa.String(64), nullable=True),
    sa.Column("completion_marker_validation", RUNNER_JSON, nullable=True),
    sa.Column("result_integrity_evidence", RUNNER_JSON, nullable=True),
    sa.Column("terminal_decision_reason", sa.String(256), nullable=True),
    sa.Column("failure_phase", sa.String(64), nullable=True),
    sa.Column("failure_type", sa.String(128), nullable=True),
    sa.Column("error_message", sa.String(2000), nullable=True),
    sa.Column("recovery_action", sa.String(128), nullable=True),
    sa.Column("recovery_observed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("recovery_process_state", RUNNER_JSON, nullable=True),
)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_index(bind, table: str, name: str) -> bool:
    return any(item["name"] == name for item in sa.inspect(bind).get_indexes(table))


def _has_constraint(bind, table: str, name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(item.get("name") == name for item in inspector.get_check_constraints(table))


def _drop_constraint_if_present(bind, table: str, name: str) -> None:
    # Naming conventions may materialize check constraints with a ck_<table>_ prefix.
    # Drop the inspected database name so upgrades work across dialects and revisions.
    for item in sa.inspect(bind).get_check_constraints(table):
        actual_name = item.get("name")
        if actual_name == name or actual_name == f"ck_{table}_{name}":
            op.drop_constraint(actual_name, table, type_="check")
            return


def upgrade() -> None:
    """Extend the shared root and install both logical queue guard rows."""

    bind = op.get_bind()
    if not _has_table(bind, "backtest_runs"):
        raise RuntimeError(
            "backtest_runs must be created by 20260831_01 before runner supervision"
        )

    # Existing task-08 rows use tenant_id as their idempotency scope.  Copying
    # that value is an identity-preserving migration, not a synthetic run.
    if not _has_column(bind, "backtest_runs", "idempotency_scope"):
        op.add_column("backtest_runs", _ADDITIVE_COLUMNS[0])
        op.execute(
            sa.text(
                "UPDATE backtest_runs SET idempotency_scope = tenant_id "
                "WHERE idempotency_scope IS NULL"
            )
        )

    # A nullable historical key cannot be made a valid idempotency identity;
    # fail with an explicit message instead of inventing a key.
    if bind.execute(
        sa.text("SELECT count(*) FROM backtest_runs WHERE idempotency_key IS NULL")
    ).scalar_one():
        raise RuntimeError(
            "backtest_runs contains rows without idempotency_key; refusing to invent identity"
        )
    if any(
        item["name"] == "idempotency_key" and item["nullable"]
        for item in sa.inspect(bind).get_columns("backtest_runs")
    ) and bind.dialect.name != "sqlite":
        op.alter_column(
            "backtest_runs",
            "idempotency_key",
            existing_type=sa.String(200),
            nullable=False,
        )

    for column in _ADDITIVE_COLUMNS[1:]:
        if not _has_column(bind, "backtest_runs", column.name):
            op.add_column("backtest_runs", column)

    # The original root used an integer implementation detail for current_step;
    # the supervisor contract stores a stable stage identifier.  Existing NULL
    # values are safe to convert and numeric historical values retain their text.
    current_step_type = next(
        item["type"]
        for item in sa.inspect(bind).get_columns("backtest_runs")
        if item["name"] == "current_step"
    )
    if isinstance(current_step_type, sa.Integer) and bind.dialect.name != "sqlite":
        op.alter_column(
            "backtest_runs",
            "current_step",
            existing_type=sa.Integer(),
            type_=sa.String(128),
            postgresql_using="current_step::text",
        )

    # Preserve cancellation and process evidence from the task-08 aliases.
    if _has_column(bind, "backtest_runs", "cancel_requested"):
        op.execute(
            sa.text(
                "UPDATE backtest_runs SET cancel_requested_at = updated_at "
                "WHERE cancel_requested = TRUE AND cancel_requested_at IS NULL"
            )
        )
    if _has_column(bind, "backtest_runs", "process_start_token"):
        op.execute(
            sa.text(
                "UPDATE backtest_runs SET child_start_identity = process_start_token "
                "WHERE child_start_identity IS NULL"
            )
        )
    if _has_column(bind, "backtest_runs", "process_group_id"):
        op.execute(
            sa.text(
                "UPDATE backtest_runs SET child_process_group_id = process_group_id "
                "WHERE child_process_group_id IS NULL"
            )
        )
    if _has_column(bind, "backtest_runs", "current_date"):
        # PostgreSQL's DATE input parser is strict.  Invalid historical text is
        # left NULL and therefore cannot be presented as a false trading date.
        if bind.dialect.name == "postgresql":
            op.execute(
                sa.text(
                    "UPDATE backtest_runs SET current_trading_date = "
                    "CASE WHEN \"current_date\" ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
                    "THEN \"current_date\"::date ELSE NULL END "
                    "WHERE current_trading_date IS NULL"
                )
            )

    # The old status='terminal' marker is normalized to the canonical final
    # status only when its terminal_status evidence exists.  Unknown terminal
    # rows are refused rather than guessed.
    if bind.execute(
        sa.text(
            "SELECT count(*) FROM backtest_runs "
            "WHERE status = 'terminal' AND terminal_status IS NULL"
        )
    ).scalar_one():
        raise RuntimeError(
            "backtest_runs contains terminal rows without terminal_status evidence"
        )
    op.execute(
        sa.text(
            "UPDATE backtest_runs SET status = terminal_status "
            "WHERE status = 'terminal' AND terminal_status IS NOT NULL"
        )
    )

    # Replace the legacy state constraint with the canonical nine-state enum.
    _drop_constraint_if_present(bind, "backtest_runs", "backtest_status_supported")
    _drop_constraint_if_present(bind, "backtest_runs", "backtest_terminal_status_consistent")
    _drop_constraint_if_present(bind, "backtest_runs", "backtest_cancel_request_consistent")
    _drop_constraint_if_present(bind, "backtest_runs", "backtest_termination_reason_consistent")
    op.create_check_constraint(
        "backtest_status_supported",
        "backtest_runs",
        "status IN ('queued','starting','running','cancel_requested','succeeded','failed','cancelled','timed_out','indeterminate')",
    )
    op.create_check_constraint(
        "backtest_finished_at_consistent",
        "backtest_runs",
        "(status IN ('succeeded','failed','cancelled','timed_out','indeterminate')) = (finished_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "backtest_terminal_status_consistent",
        "backtest_runs",
        "(status IN ('succeeded','failed','cancelled','timed_out','indeterminate')) = (terminal_status IS NOT NULL)",
    )
    op.create_check_constraint(
        "backtest_queued_identity_clear",
        "backtest_runs",
        "status <> 'queued' OR (launch_id IS NULL AND child_pid IS NULL AND child_start_identity IS NULL AND child_process_group_id IS NULL AND process_start_token IS NULL AND process_group_id IS NULL AND worker_handshake_at IS NULL)",
    )
    op.create_check_constraint(
        "backtest_running_identity_complete",
        "backtest_runs",
        "status <> 'running' OR (launch_id IS NOT NULL AND child_pid IS NOT NULL AND child_start_identity IS NOT NULL AND child_process_group_id IS NOT NULL AND worker_handshake_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "backtest_stdout_bytes_non_negative",
        "backtest_runs",
        "stdout_bytes IS NULL OR stdout_bytes >= 0",
    )
    op.create_check_constraint(
        "backtest_indeterminate_reason_required",
        "backtest_runs",
        "status <> 'indeterminate' OR length(btrim(terminal_decision_reason)) > 0",
    )
    op.create_check_constraint(
        "backtest_cancel_request_consistent",
        "backtest_runs",
        "cancel_requested_at IS NULL OR cancel_requested = TRUE",
    )
    op.create_check_constraint(
        "backtest_termination_reason_consistent",
        "backtest_runs",
        "termination_requested_at IS NULL OR length(btrim(termination_reason)) > 0",
    )
    op.create_check_constraint(
        "backtest_finished_after_started",
        "backtest_runs",
        "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
    )

    if _has_index(bind, "backtest_runs", "uq_backtest_runs_idempotency"):
        op.drop_index("uq_backtest_runs_idempotency", table_name="backtest_runs")
    op.create_index(
        "uq_backtest_runs_idempotency",
        "backtest_runs",
        ["idempotency_scope", "idempotency_key"],
        unique=True,
    )
    if not _has_index(bind, "backtest_runs", "ix_backtest_runs_claimable"):
        op.create_index(
            "ix_backtest_runs_claimable",
            "backtest_runs",
            ["run_kind", "status", "created_at", "id"],
        )
    if not _has_index(bind, "backtest_runs", "ix_backtest_runs_heartbeat"):
        op.create_index(
            "ix_backtest_runs_heartbeat",
            "backtest_runs",
            ["status", "last_heartbeat_at"],
        )

    if not _has_table(bind, "backtest_queue_guards"):
        op.create_table(
            "backtest_queue_guards",
            sa.Column("queue_kind", sa.String(40), primary_key=True),
            sa.CheckConstraint(
                "queue_kind IN ('backtest_run', 'internal_link_acceptance')",
                name="backtest_queue_guard_kind_supported",
            ),
        )
    # ``ON CONFLICT`` works on PostgreSQL and modern SQLite.  The fallback is
    # useful for migration unit tests that use a minimal SQL dialect.
    if bind.dialect.name in {"postgresql", "sqlite"}:
        op.execute(
            sa.text(
                "INSERT INTO backtest_queue_guards (queue_kind) VALUES "
                "('backtest_run'), ('internal_link_acceptance') "
                "ON CONFLICT (queue_kind) DO NOTHING"
            )
        )
    else:
        for kind in ("backtest_run", "internal_link_acceptance"):
            if not bind.execute(
                sa.text(
                    "SELECT 1 FROM backtest_queue_guards WHERE queue_kind = :kind"
                ),
                {"kind": kind},
            ).scalar():
                op.execute(
                    sa.text(
                        "INSERT INTO backtest_queue_guards (queue_kind) VALUES (:kind)"
                    ),
                    {"kind": kind},
                )


def downgrade() -> None:
    """Remove only runner-owned additions; never delete result rows."""

    bind = op.get_bind()
    if not _has_table(bind, "backtest_runs"):
        return

    if _has_table(bind, "backtest_queue_guards"):
        op.drop_table("backtest_queue_guards")
    for name in (
        "ix_backtest_runs_heartbeat",
        "ix_backtest_runs_claimable",
    ):
        if _has_index(bind, "backtest_runs", name):
            op.drop_index(name, table_name="backtest_runs")

    if _has_index(bind, "backtest_runs", "uq_backtest_runs_idempotency"):
        op.drop_index("uq_backtest_runs_idempotency", table_name="backtest_runs")
    op.create_index(
        "uq_backtest_runs_idempotency",
        "backtest_runs",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )

    for name in (
        "backtest_finished_after_started",
        "backtest_indeterminate_reason_required",
        "backtest_termination_reason_consistent",
        "backtest_cancel_request_consistent",
        "backtest_terminal_status_consistent",
        "backtest_stdout_bytes_non_negative",
        "backtest_running_identity_complete",
        "backtest_queued_identity_clear",
        "backtest_finished_at_consistent",
        "backtest_status_supported",
    ):
        _drop_constraint_if_present(bind, "backtest_runs", name)

    # Restore the task-08 lifecycle marker for compatibility with a downgrade
    # that is followed by the original root migration.
    op.execute(
        sa.text(
            "UPDATE backtest_runs SET terminal_status = status, status = 'terminal' "
            "WHERE status IN ('succeeded','failed','cancelled','timed_out','indeterminate')"
        )
    )
    op.create_check_constraint(
        "backtest_status_supported",
        "backtest_runs",
        "status IN ('queued','starting','running','cancel_requested','terminal')",
    )
    op.create_check_constraint(
        "backtest_terminal_status_consistent",
        "backtest_runs",
        "(status = 'terminal') = (terminal_status IS NOT NULL)",
    )

    # Convert the canonical stage identifier back to the original integer
    # representation only when every non-null value is numeric.
    if bind.dialect.name != "sqlite" and _has_column(bind, "backtest_runs", "current_step"):
        op.alter_column(
            "backtest_runs",
            "current_step",
            existing_type=sa.String(128),
            type_=sa.Integer(),
            postgresql_using="NULLIF(current_step, '')::integer",
        )

    for column in reversed(_ADDITIVE_COLUMNS[1:]):
        if _has_column(bind, "backtest_runs", column.name):
            op.drop_column("backtest_runs", column.name)
    if _has_column(bind, "backtest_runs", "idempotency_scope"):
        op.drop_column("backtest_runs", "idempotency_scope")
