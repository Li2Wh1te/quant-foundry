"""Persistent account-profile configuration for the backtesting workbench."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, object_session
from sqlalchemy import event

from app.db.base import Base


class BacktestAccountProfileRecord(Base):
    """One editable account profile bound to one editable fee schedule.

    The row is the current editable catalogue entry.  ``version`` advances
    whenever the profile is edited, while each run copies the complete row and
    fee configuration into its own immutable snapshot.
    """

    __tablename__ = "backtest_account_profiles"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "status IN ('active', 'inactive', 'retired')",
            name="status_supported",
        ),
        CheckConstraint(
            "length(btrim(fee_schedule_key)) > 0",
            name="fee_schedule_key_not_blank",
        ),
        CheckConstraint(
            "fee_schedule_key <> 'zero_cost'",
            name="zero_cost_schedule_forbidden",
        ),
        CheckConstraint("version > 0", name="account_profile_version_positive"),
        CheckConstraint(
            "fee_schedule_version > 0",
            name="fee_schedule_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(fee_rules) = 'array'",
            name="fee_rules_array",
        ),
        CheckConstraint(
            "jsonb_array_length(fee_rules) > 0",
            name="fee_rules_not_empty",
        ),
        CheckConstraint(
            "jsonb_typeof(fee_schedule_metadata) = 'object'",
            name="fee_schedule_metadata_object",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="profile_metadata_object",
        ),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index(
            "ix_backtest_account_profiles_status_name",
            "status",
            "name",
        ),
        Index(
            "uq_backtest_account_profiles_name_ci",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="Stable account-profile identity used by explicit run selection.",
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="User-facing account name shown in the backtest selector.",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        comment="Account lifecycle state; only active profiles are selectable.",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Monotonic account configuration version captured by new runs.",
    )
    fee_schedule_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Stable fee-schedule key bound to this account profile.",
    )
    fee_schedule_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Monotonic fee configuration version captured by new runs.",
    )
    fee_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="Complete editable fee-rule configuration; no fee defaults are implied.",
    )
    fee_schedule_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Additional fee-schedule metadata captured by the account configuration.",
    )
    profile_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Additional account-profile metadata.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Account-profile creation timestamp.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Latest account or fee configuration update timestamp.",
    )


class BacktestAccountProfileVersionRecord(Base):
    """Append-only configuration plus independently mutable availability.

    The current catalogue row is an editing convenience. This table is the
    authority for a pinned version, including its complete fee configuration.
    """

    __tablename__ = "backtest_account_profile_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("status IN ('active', 'inactive', 'retired')", name="status_supported"),
    )
    profile_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("backtest_account_profiles.id", ondelete="RESTRICT"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


@event.listens_for(BacktestAccountProfileVersionRecord, "before_update")
def _protect_account_version(_mapper, _connection, target):
    """Permit availability edits, but never mutate a historical definition."""
    from sqlalchemy import inspect
    state = inspect(target)
    if any(state.attrs[name].history.has_changes() for name in ("profile_id", "version", "snapshot", "created_at")):
        raise ValueError("account profile version configuration is immutable")


@event.listens_for(BacktestAccountProfileVersionRecord, "before_delete")
def _prevent_account_version_delete(_mapper, _connection, _target):
    raise ValueError("account profile versions cannot be deleted")


__all__ = ["BacktestAccountProfileRecord", "BacktestAccountProfileVersionRecord"]


class BacktestRunRecord(Base):
    """Durable root row for one immutable backtest execution."""
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("run_kind IN ('backtest_run','internal_link_acceptance')", name="backtest_run_kind_supported"),
        CheckConstraint("(run_kind = 'backtest_run' AND profile = 'formal@1') OR (run_kind = 'internal_link_acceptance' AND profile = 'internal_link_acceptance@1')", name="backtest_kind_profile_match"),
        CheckConstraint("status IN ('queued','starting','running','cancel_requested','succeeded','failed','cancelled','timed_out','indeterminate')", name="backtest_status_supported"),
        CheckConstraint("terminal_status IS NULL OR terminal_status IN ('succeeded','failed','cancelled','timed_out','indeterminate')", name="backtest_terminal_supported"),
        CheckConstraint("(status IN ('succeeded','failed','cancelled','timed_out','indeterminate')) = (terminal_status IS NOT NULL)", name="backtest_terminal_status_consistent"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="backtest_progress_range"),
        CheckConstraint("length(config_hash) = 64", name="backtest_config_hash_sha256"),
        CheckConstraint("max_lookback_sessions = 512", name="backtest_lookback_fixed"),
        CheckConstraint("data_chunk_policy_key = 'fixed_trading_sessions' AND data_chunk_policy_version = 1 AND data_chunk_size_sessions = 20", name="backtest_chunk_policy_fixed"),
        CheckConstraint("(status IN ('succeeded','failed','cancelled','timed_out','indeterminate')) = (finished_at IS NOT NULL)", name="backtest_finished_at_consistent"),
        CheckConstraint("status <> 'queued' OR (launch_id IS NULL AND child_pid IS NULL AND child_start_identity IS NULL AND child_process_group_id IS NULL AND process_start_token IS NULL AND process_group_id IS NULL AND worker_handshake_at IS NULL)", name="backtest_queued_identity_clear"),
        CheckConstraint("status <> 'running' OR (launch_id IS NOT NULL AND child_pid IS NOT NULL AND child_start_identity IS NOT NULL AND child_process_group_id IS NOT NULL AND worker_handshake_at IS NOT NULL)", name="backtest_running_identity_complete"),
        CheckConstraint("stdout_bytes IS NULL OR stdout_bytes >= 0", name="backtest_stdout_bytes_non_negative"),
        CheckConstraint("status <> 'indeterminate' OR length(btrim(terminal_decision_reason)) > 0", name="backtest_indeterminate_reason_required"),
        CheckConstraint("cancel_requested_at IS NULL OR cancel_requested = TRUE", name="backtest_cancel_request_consistent"),
        CheckConstraint("termination_requested_at IS NULL OR length(btrim(termination_reason)) > 0", name="backtest_termination_reason_consistent"),
        CheckConstraint("finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at", name="backtest_finished_after_started"),
        CheckConstraint("updated_at >= created_at", name="backtest_updated_after_created"),
        Index("ix_backtest_runs_queue", "run_kind", "status", "created_at"),
        Index("uq_backtest_runs_idempotency", "idempotency_scope", "idempotency_key", unique=True),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, server_default="default")
    # ``idempotency_scope`` is the public ownership boundary.  ``tenant_id``
    # remains for compatibility with the task-08 root and is kept equal by
    # the creation repository for rows written by this service.
    idempotency_scope: Mapped[str] = mapped_column(String(128), nullable=False, server_default="default")
    run_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    profile: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    terminal_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_request_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    rerun_of_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    backtest_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    strategy_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_source_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_contract_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    initial_cash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initial_positions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    data_request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Immutable point-in-time evidence captured at admission (provider,
    # calendar/PIT snapshot and report hashes).  It is intentionally separate
    # from the request so query projections can expose evidence without
    # re-resolving a mutable provider.
    data_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # Versioned, bounded projection of the complete audit dimensions used by
    # comparison and operator views; detailed reports remain in evidence rows.
    audit_projection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # Immutable four-level admission evidence.  The binding snapshot remains
    # the source of truth; this projection makes gate status queryable without
    # reopening mutable data or strategy dependencies.
    formal_gate_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    pit_snapshot_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    pit_cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_provider_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_lookback_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="512")
    data_chunk_policy_key: Mapped[str] = mapped_column(String(64), nullable=False, server_default="fixed_trading_sessions")
    data_chunk_policy_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    data_chunk_size_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    data_admission_preflight_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_preflight_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_profile_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee_schedule_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fee_schedule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee_schedule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    analyzer_specs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    behavior_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress: Mapped[Decimal] = mapped_column(Numeric(), nullable=False, server_default="0")
    # Canonical supervisor progress fields.  ``current_date`` and the legacy
    # integer step column are retained below for rows created by task 08; all
    # new runner code writes the explicit canonical columns.
    current_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    launch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    child_start_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    child_process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_handshake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_progress_persisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    forced_termination: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    completion_marker: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    runner_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runner_exit_code_protocol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runner_exit_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runner_exit_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_start_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stdout_truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Keep the bounded excerpt and cap metadata separate from the compact
    # columns above; operators need the original diagnostic evidence without
    # turning the run root into an unbounded log sink.
    stdout_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resource_limit_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    runner_config_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    completion_marker_protocol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completion_marker_validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_integrity_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    terminal_decision_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    failure_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Bounded, desensitized failure location and traceback evidence produced
    # by the isolated Worker; terminal status remains Supervisor-owned.
    failure_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    recovery_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recovery_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_process_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Result persistence projection fields. These are maintained by the
    # writer/supervisor boundary and never recomputed by query handlers.
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    result_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    result_integrity_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def owner_scope(self) -> str:
        """Stable deployment owner scope (tenant_id remains the DB column)."""
        return self.tenant_id


__all__.append("BacktestRunRecord")


class BacktestQueueGuardRecord(Base):
    """Permanent per-kind rows that serialize queue capacity transactions."""

    __tablename__ = "backtest_queue_guards"
    __table_args__ = (
        CheckConstraint(
            "queue_kind IN ('backtest_run', 'internal_link_acceptance')",
            name="backtest_queue_guard_kind_supported",
        ),
    )

    queue_kind: Mapped[str] = mapped_column(String(40), primary_key=True)


__all__.append("BacktestQueueGuardRecord")


_IMMUTABLE_RUN_FIELDS = (
    "tenant_id", "idempotency_scope", "run_kind", "profile", "idempotency_key",
    "idempotency_request_hash", "rerun_of_run_id", "config_hash",
    "backtest_config", "strategy_revision_id", "strategy_source_hash",
    "strategy_contract_version", "parameters", "initial_cash", "initial_positions",
    "data_request", "data_provider_key", "max_lookback_sessions",
    "data_chunk_policy_key", "data_chunk_policy_version", "data_chunk_size_sessions",
    "data_admission_preflight_hash", "account_profile_id", "account_profile_version",
    "fee_schedule_key", "fee_schedule_version", "fee_schedule_snapshot",
    "analyzer_specs", "behavior_versions", "random_seed", "data_evidence",
    "formal_gate_evidence", "pit_snapshot_hash", "pit_cutoff_at",
)


@event.listens_for(BacktestRunRecord, "before_update", propagate=True)
def _reject_immutable_run_input_updates(mapper, connection, target) -> None:
    """Fail closed when a service attempts to mutate frozen run inputs."""
    state = object_session(target)
    if state is None:
        return
    from sqlalchemy import inspect
    history = inspect(target)
    changed = [name for name in _IMMUTABLE_RUN_FIELDS if history.attrs[name].history.has_changes()]
    if changed:
        raise ValueError(f"immutable backtest run fields cannot be updated: {', '.join(changed)}")
    progress_history = history.attrs.progress.history
    if progress_history.has_changes() and progress_history.deleted:
        previous = progress_history.deleted[0]
        if target.progress < previous:
            raise ValueError("backtest run progress cannot move backwards")
