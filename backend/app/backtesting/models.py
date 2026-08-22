"""Persistent account-profile configuration for the backtesting workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, String, Uuid, func, text
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
