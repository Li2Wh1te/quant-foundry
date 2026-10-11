"""Three S3 control objects; existing account, strategy and result rows unchanged."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261012_01"
down_revision = "20261011_01"
branch_labels = depends_on = None


def upgrade():
    js = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("s3_run_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner", "idempotency_key", name="s3_batch_owner_key"))
    op.create_table("s3_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(71), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("backtest_account_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("strategy_revision_id", sa.Uuid(), sa.ForeignKey("strategy_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("s3_run_batches.id", ondelete="RESTRICT")),
        sa.Column("batch_index", sa.Integer()),
        sa.Column("retry_of", sa.Uuid(), sa.ForeignKey("s3_runs.id", ondelete="RESTRICT")),
        sa.Column("config", js, nullable=False), sa.Column("accepted", js, nullable=False),
        sa.Column("strategy_source", sa.Text(), nullable=False), sa.Column("limits", js, nullable=False),
        sa.Column("group_policy", js, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token", sa.String(32)), sa.Column("supervisor_id", sa.String(128)),
        sa.Column("lease_until", sa.Float(), nullable=False, server_default="0"),
        sa.Column("attempt_identity", js), sa.Column("outcome", js),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner", "idempotency_key", name="s3_run_owner_key"),
        sa.CheckConstraint("status IN ('queued','starting','running','succeeded','failed','cancelled')", name="s3_run_status"),
        sa.CheckConstraint("fence >= 0", name="s3_run_fence"),
        sa.ForeignKeyConstraint(["account_id", "account_version"], ["backtest_account_profile_versions.profile_id", "backtest_account_profile_versions.version"], ondelete="RESTRICT", name="s3_run_account_version"))
    op.create_index("ix_s3_runs_queue", "s3_runs", ["status", "created_at", "id"])
    op.create_index("ix_s3_runs_owner", "s3_runs", ["owner", "created_at", "id"])
    op.create_index("ix_s3_runs_batch", "s3_runs", ["batch_id", "batch_index", "created_at"])
    op.create_table("s3_run_results",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("s3_runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("directory", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION protect_s3_run() RETURNS trigger AS $$
        BEGIN
          IF (NEW.owner,NEW.idempotency_key,NEW.request_hash,NEW.account_id,NEW.account_version,
              NEW.strategy_revision_id,NEW.batch_id,NEW.batch_index,NEW.retry_of,NEW.config,
              NEW.accepted,NEW.strategy_source,NEW.limits,NEW.group_policy,NEW.created_at)
             IS DISTINCT FROM
             (OLD.owner,OLD.idempotency_key,OLD.request_hash,OLD.account_id,OLD.account_version,
              OLD.strategy_revision_id,OLD.batch_id,OLD.batch_index,OLD.retry_of,OLD.config,
              OLD.accepted,OLD.strategy_source,OLD.limits,OLD.group_policy,OLD.created_at) THEN
            RAISE EXCEPTION 'accepted S3 configuration is immutable';
          END IF;
          IF OLD.status IN ('succeeded','failed','cancelled') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'S3 terminal state is immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER protect_s3_run BEFORE UPDATE ON s3_runs FOR EACH ROW EXECUTE FUNCTION protect_s3_run();""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM s3_runs)")).scalar():
        raise RuntimeError("S3 business runs exist; stop admission and repair forward")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER protect_s3_run ON s3_runs")
        op.execute("DROP FUNCTION protect_s3_run()")
    op.drop_table("s3_run_results")
    op.drop_table("s3_runs")
    op.drop_table("s3_run_batches")
