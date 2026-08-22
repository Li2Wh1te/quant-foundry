"""Add editable backtest account profiles and bound fee configurations.

Revision ID: 20260822_01
Revises: 20260819_01
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260822_01"
down_revision: str | None = "20260819_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the editable account catalogue used by explicit run selection."""
    op.create_table(
        "backtest_account_profiles",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="Stable account-profile identity used by explicit run selection.",
        ),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment="User-facing account name shown in the backtest selector.",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
            comment="Account lifecycle state; only active profiles are selectable.",
        ),
        sa.Column(
            "fee_schedule_key",
            sa.String(length=100),
            nullable=False,
            comment="Stable fee-schedule key bound to this account profile.",
        ),
        sa.Column(
            "fee_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Complete editable fee-rule configuration; no fee defaults are implied.",
        ),
        sa.Column(
            "fee_schedule_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Additional fee-schedule metadata captured by the account configuration.",
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="Additional account-profile metadata.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Account-profile creation timestamp.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Latest account or fee configuration update timestamp.",
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'retired')",
            name="status_supported",
        ),
        sa.CheckConstraint(
            "length(btrim(fee_schedule_key)) > 0",
            name="fee_schedule_key_not_blank",
        ),
        sa.CheckConstraint(
            "fee_schedule_key <> 'zero_cost'",
            name="zero_cost_schedule_forbidden",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fee_rules) = 'array'",
            name="fee_rules_array",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(fee_rules) > 0",
            name="fee_rules_not_empty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fee_schedule_metadata) = 'object'",
            name="fee_schedule_metadata_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="profile_metadata_object",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_account_profiles"),
        comment=(
            "Editable backtest account profiles. Formal runs must explicitly "
            "select one and freeze its complete configuration elsewhere."
        ),
    )
    op.create_index(
        "ix_backtest_account_profiles_status_name",
        "backtest_account_profiles",
        ["status", "name"],
    )
    op.create_index(
        "uq_backtest_account_profiles_name_ci",
        "backtest_account_profiles",
        [sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    """Drop the editable account catalogue."""
    op.drop_index(
        "uq_backtest_account_profiles_name_ci",
        table_name="backtest_account_profiles",
    )
    op.drop_index(
        "ix_backtest_account_profiles_status_name",
        table_name="backtest_account_profiles",
    )
    op.drop_table("backtest_account_profiles")
