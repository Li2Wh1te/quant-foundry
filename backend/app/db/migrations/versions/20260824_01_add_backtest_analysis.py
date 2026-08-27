"""Add analyzer identity to backtest_metrics and create the run-level
analysis summary table.

Revision ID: 20260824_01
Revises: 20260823_01
Create Date: 2026-08-24

The migration is strictly additive: three nullable columns join
``backtest_metrics`` (legacy rows keep NULL identity), and the new
``backtest_analysis_summaries`` table stores run-level analysis state.  The
existing unique index ``uq_backtest_metrics_run_key_version`` is untouched.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260824_01"
down_revision: str | None = "20260823_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 18)
JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Add analyzer identity to metrics and create the analysis summary table."""
    op.add_column(
        "backtest_metrics",
        sa.Column(
            "analyzer_key",
            sa.String(length=100),
            nullable=True,
            comment="Stable key of the analyzer that produced this metric; "
            "null only for legacy rows.",
        ),
    )
    op.add_column(
        "backtest_metrics",
        sa.Column(
            "analyzer_version",
            sa.Integer(),
            nullable=True,
            comment="Registered version of the producing analyzer; paired "
            "with analyzer_key.",
        ),
    )
    op.add_column(
        "backtest_metrics",
        sa.Column(
            "analyzer_metadata",
            JSON_TYPE,
            nullable=True,
            comment="Analyzer context such as reason_code, rate convention, "
            "and formula signature references.",
        ),
    )
    op.create_check_constraint(
        "analyzer_identity_pair",
        "backtest_metrics",
        "(analyzer_key IS NULL) = (analyzer_version IS NULL)",
    )

    op.create_table(
        "backtest_analysis_summaries",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            comment="Surrogate primary key; business identity lives in the "
            "unique run_id index.",
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning backtest run; exactly one analysis summary per "
            "run. A foreign key to backtest_runs is added by the "
            "run-creation task.",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="Analysis lifecycle status: partial, final, or aborted.",
        ),
        sa.Column(
            "analyzer_snapshot",
            JSON_TYPE,
            nullable=False,
            comment="Frozen AnalyzerSpec identities, parameters, and "
            "converter identity.",
        ),
        sa.Column(
            "formula_signature",
            sa.String(length=128),
            nullable=False,
            comment="Hash over the logical formula configuration (spec "
            "identities, parameters, decimal policy).",
        ),
        sa.Column(
            "input_evidence_signature",
            sa.String(length=128),
            nullable=False,
            comment="Hash over the actual input evidence (E0, observations, "
            "fills, rate snapshot hash).",
        ),
        sa.Column(
            "initial_equity",
            AMOUNT,
            nullable=True,
            comment="Frozen E0 from the initial equity snapshot.",
        ),
        sa.Column(
            "valid_day_count",
            sa.Integer(),
            nullable=True,
            comment="Formal days with a valid positive end-of-day equity.",
        ),
        sa.Column(
            "fill_count",
            sa.Integer(),
            nullable=True,
            comment="Accounting-applied fill count at the latest update.",
        ),
        sa.Column(
            "gross_traded_notional",
            AMOUNT,
            nullable=True,
            comment="Sum of applied gross traded notionals at the latest update.",
        ),
        sa.Column(
            "cumulative_fees",
            AMOUNT,
            nullable=True,
            comment="Sum of applied fees at the latest update.",
        ),
        sa.Column(
            "rate_snapshot",
            JSON_TYPE,
            nullable=True,
            comment="Complete frozen PIT daily risk-free rate series (Sharpe "
            "B only); empty for A/C runs.",
        ),
        sa.Column(
            "rate_snapshot_hash",
            sa.String(length=128),
            nullable=True,
            comment="Deterministic hash of the frozen rate snapshot content.",
        ),
        sa.Column(
            "rate_source_versions",
            JSON_TYPE,
            nullable=True,
            comment="Rate source key/version and query parameters of the snapshot.",
        ),
        sa.Column(
            "missing_ranges",
            JSON_TYPE,
            nullable=True,
            comment="Deterministic contiguous missing-session ranges of the "
            "rate series.",
        ),
        sa.Column(
            "reporting_currency",
            sa.String(length=8),
            nullable=False,
            comment="Single reporting currency; equals the accounting policy "
            "currency.",
        ),
        sa.Column(
            "last_chunk_sequence",
            sa.Integer(),
            nullable=True,
            comment=(
                "Zero-based sequence of the latest successfully persisted chunk; "
                "aborted runs never record the failed chunk."
            ),
        ),
        sa.Column(
            "completed_through_session",
            sa.Date(),
            nullable=True,
            comment="Last session whose end-of-day observation was committed.",
        ),
        sa.Column(
            "abort_reason",
            sa.String(length=1000),
            nullable=True,
            comment="Structured failure reason; required exactly when aborted.",
        ),
        sa.Column(
            "failed_step_sequence",
            sa.Integer(),
            nullable=True,
            comment="Step whose failure aborted the run.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Row creation timestamp.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Latest write timestamp of this summary.",
        ),
        sa.Column(
            "finalized_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Terminal finalization time; set only for final/aborted rows.",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('partial', 'final', 'aborted')",
            name="analysis_summary_status_allowed",
        ),
        sa.CheckConstraint(
            "(status = 'aborted') = (abort_reason IS NOT NULL)",
            name="analysis_summary_abort_reason_pair",
        ),
    )
    op.create_index(
        "uq_backtest_analysis_summaries_run",
        "backtest_analysis_summaries",
        ["run_id"],
        unique=True,
    )


def downgrade() -> None:
    """Remove the analyzer columns and the summary table.

    The removal refuses to run when analysis results exist: dropping rows
    that carry analyzer identity or summaries would silently destroy audit
    evidence, so a destructive rollback must be an explicit operator
    decision after the data has been handled.
    """

    conn = op.get_bind()
    summary_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM backtest_analysis_summaries")
    ).scalar_one()
    if summary_count:
        raise RuntimeError(
            f"backtest_analysis_summaries still holds {summary_count} row(s); "
            "refusing the destructive rollback of analysis results"
        )
    metric_with_identity = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM backtest_metrics WHERE analyzer_key IS NOT NULL"
        )
    ).scalar_one()
    if metric_with_identity:
        raise RuntimeError(
            f"{metric_with_identity} backtest_metrics row(s) carry analyzer "
            "identity; refusing the destructive rollback of analyzer results"
        )
    op.drop_index(
        "uq_backtest_analysis_summaries_run", table_name="backtest_analysis_summaries"
    )
    op.drop_table("backtest_analysis_summaries")
    op.drop_constraint("analyzer_identity_pair", "backtest_metrics", type_="check")
    op.drop_column("backtest_metrics", "analyzer_metadata")
    op.drop_column("backtest_metrics", "analyzer_version")
    op.drop_column("backtest_metrics", "analyzer_key")
