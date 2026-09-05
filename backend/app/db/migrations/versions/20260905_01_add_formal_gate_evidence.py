"""Persist the immutable four-level formal admission gate projection."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260905_01"
down_revision: str | None = "20260904_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add the queryable formal admission evidence projection."""

    bind = op.get_bind()
    if not _has_column(bind, "backtest_runs", "formal_gate_evidence"):
        op.add_column(
            "backtest_runs",
            sa.Column(
                "formal_gate_evidence",
                JSON_TYPE,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # Keep this projection immutable at the database boundary too.  The
    # existing run-input trigger predates this column; a focused trigger avoids
    # rewriting that historical function while preserving its guarantees.
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION reject_formal_gate_evidence_update()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF NEW.formal_gate_evidence IS DISTINCT FROM OLD.formal_gate_evidence THEN
                        RAISE EXCEPTION
                            'formal admission gate evidence cannot be updated';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                """
            )
        )
        op.execute(
            sa.text(
                """
                DROP TRIGGER IF EXISTS formal_gate_evidence_immutable
                ON backtest_runs;
                CREATE TRIGGER formal_gate_evidence_immutable
                BEFORE UPDATE ON backtest_runs
                FOR EACH ROW
                EXECUTE FUNCTION reject_formal_gate_evidence_update();
                """
            )
        )


def downgrade() -> None:
    """Remove the formal gate projection and its focused immutability trigger."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS formal_gate_evidence_immutable "
                "ON backtest_runs"
            )
        )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS reject_formal_gate_evidence_update()"
            )
        )
    if _has_column(bind, "backtest_runs", "formal_gate_evidence"):
        op.drop_column("backtest_runs", "formal_gate_evidence")
