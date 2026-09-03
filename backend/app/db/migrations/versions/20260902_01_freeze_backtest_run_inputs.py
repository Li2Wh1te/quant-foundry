"""Freeze the complete run input snapshot and version account configurations."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260902_01"
down_revision: str | None = "20260901_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add version identities and reject mutation of frozen run inputs."""

    bind = op.get_bind()

    if not _has_column(bind, "backtest_account_profiles", "version"):
        op.add_column(
            "backtest_account_profiles",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
    if not _has_column(bind, "backtest_account_profiles", "fee_schedule_version"):
        op.add_column(
            "backtest_account_profiles",
            sa.Column(
                "fee_schedule_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )
    existing_checks = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(
            "backtest_account_profiles"
        )
    }
    if bind.dialect.name != "sqlite":
        if "account_profile_version_positive" not in existing_checks:
            op.create_check_constraint(
                "account_profile_version_positive",
                "backtest_account_profiles",
                "version > 0",
            )
        if "fee_schedule_version_positive" not in existing_checks:
            op.create_check_constraint(
                "fee_schedule_version_positive",
                "backtest_account_profiles",
                "fee_schedule_version > 0",
            )

    if not _has_column(bind, "backtest_runs", "random_seed"):
        op.add_column("backtest_runs", sa.Column("random_seed", sa.Integer(), nullable=True))

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION reject_backtest_run_input_update()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF ROW(
                        NEW.tenant_id,
                        NEW.idempotency_scope,
                        NEW.run_kind,
                        NEW.profile,
                        NEW.idempotency_key,
                        NEW.idempotency_request_hash,
                        NEW.rerun_of_run_id,
                        NEW.config_hash,
                        NEW.backtest_config,
                        NEW.strategy_revision_id,
                        NEW.strategy_source_hash,
                        NEW.strategy_contract_version,
                        NEW.parameters,
                        NEW.initial_cash,
                        NEW.initial_positions,
                        NEW.data_request,
                        NEW.data_provider_key,
                        NEW.max_lookback_sessions,
                        NEW.data_chunk_policy_key,
                        NEW.data_chunk_policy_version,
                        NEW.data_chunk_size_sessions,
                        NEW.data_admission_preflight_hash,
                        NEW.account_profile_id,
                        NEW.account_profile_version,
                        NEW.fee_schedule_key,
                        NEW.fee_schedule_version,
                        NEW.fee_schedule_snapshot,
                        NEW.analyzer_specs,
                        NEW.behavior_versions,
                        NEW.random_seed,
                        NEW.data_evidence,
                        NEW.pit_snapshot_hash,
                        NEW.pit_cutoff_at
                    ) IS DISTINCT FROM ROW(
                        OLD.tenant_id,
                        OLD.idempotency_scope,
                        OLD.run_kind,
                        OLD.profile,
                        OLD.idempotency_key,
                        OLD.idempotency_request_hash,
                        OLD.rerun_of_run_id,
                        OLD.config_hash,
                        OLD.backtest_config,
                        OLD.strategy_revision_id,
                        OLD.strategy_source_hash,
                        OLD.strategy_contract_version,
                        OLD.parameters,
                        OLD.initial_cash,
                        OLD.initial_positions,
                        OLD.data_request,
                        OLD.data_provider_key,
                        OLD.max_lookback_sessions,
                        OLD.data_chunk_policy_key,
                        OLD.data_chunk_policy_version,
                        OLD.data_chunk_size_sessions,
                        OLD.data_admission_preflight_hash,
                        OLD.account_profile_id,
                        OLD.account_profile_version,
                        OLD.fee_schedule_key,
                        OLD.fee_schedule_version,
                        OLD.fee_schedule_snapshot,
                        OLD.analyzer_specs,
                        OLD.behavior_versions,
                        OLD.random_seed,
                        OLD.data_evidence,
                        OLD.pit_snapshot_hash,
                        OLD.pit_cutoff_at
                    ) THEN
                        RAISE EXCEPTION
                            'frozen backtest run inputs cannot be updated';
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
                DROP TRIGGER IF EXISTS backtest_run_inputs_immutable
                ON backtest_runs;
                CREATE TRIGGER backtest_run_inputs_immutable
                BEFORE UPDATE ON backtest_runs
                FOR EACH ROW
                EXECUTE FUNCTION reject_backtest_run_input_update();
                """
            )
        )


def downgrade() -> None:
    """Remove only this migration's additive columns and trigger."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS backtest_run_inputs_immutable "
                "ON backtest_runs"
            )
        )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS reject_backtest_run_input_update()"
            )
        )
    if _has_column(bind, "backtest_runs", "random_seed"):
        op.drop_column("backtest_runs", "random_seed")
    checks = {
        item.get("name")
        for item in sa.inspect(bind).get_check_constraints(
            "backtest_account_profiles"
        )
    }
    if bind.dialect.name != "sqlite" and "fee_schedule_version_positive" in checks:
        op.drop_constraint(
            "fee_schedule_version_positive",
            "backtest_account_profiles",
            type_="check",
        )
    if bind.dialect.name != "sqlite" and "account_profile_version_positive" in checks:
        op.drop_constraint(
            "account_profile_version_positive",
            "backtest_account_profiles",
            type_="check",
        )
    if _has_column(bind, "backtest_account_profiles", "fee_schedule_version"):
        op.drop_column("backtest_account_profiles", "fee_schedule_version")
    if _has_column(bind, "backtest_account_profiles", "version"):
        op.drop_column("backtest_account_profiles", "version")
