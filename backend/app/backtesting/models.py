"""Persistent account-profile configuration for the backtesting workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, String, Uuid, func, text, Integer, Boolean, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BacktestAccountProfileRecord(Base):
    """One editable account profile bound to one editable fee schedule.

    The record intentionally has no user-managed version column.  A future
    run-creation flow will copy the complete row into its own configuration
    snapshot; this table remains the current operator-editable catalogue.
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
    fee_schedule_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Stable fee-schedule key bound to this account profile.",
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


__all__ = ["BacktestAccountProfileRecord"]


class BacktestRunRecord(Base):
    """Durable root row for one immutable backtest execution."""
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("run_kind IN ('backtest_run','internal_link_acceptance')", name="backtest_run_kind_supported"),
        CheckConstraint("(run_kind = 'backtest_run' AND profile = 'formal@1') OR (run_kind = 'internal_link_acceptance' AND profile = 'internal_link_acceptance@1')", name="backtest_kind_profile_match"),
        CheckConstraint("status IN ('queued','starting','running','cancel_requested','terminal')", name="backtest_status_supported"),
        CheckConstraint("terminal_status IS NULL OR terminal_status IN ('succeeded','failed','cancelled','timed_out','indeterminate')", name="backtest_terminal_supported"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="backtest_progress_range"),
        CheckConstraint("length(config_hash) = 64", name="backtest_config_hash_sha256"),
        CheckConstraint("max_lookback_sessions = 512", name="backtest_lookback_fixed"),
        CheckConstraint("data_chunk_policy_key = 'fixed_trading_sessions' AND data_chunk_policy_version = 1 AND data_chunk_size_sessions = 20", name="backtest_chunk_policy_fixed"),
        Index("ix_backtest_runs_queue", "run_kind", "status", "created_at"),
        Index("uq_backtest_runs_idempotency", "tenant_id", "idempotency_key", unique=True),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, server_default="default")
    run_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    profile: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    terminal_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    backtest_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    strategy_revision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_source_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_contract_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    initial_cash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initial_positions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    data_request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
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
    progress: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False, server_default="0")
    current_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    completion_marker: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    runner_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_start_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


__all__.append("BacktestRunRecord")
