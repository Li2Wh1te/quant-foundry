"""Create durable backtest run roots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision='20260831_01'; down_revision='20260830_06'; branch_labels=None; depends_on=None

RESULT_TABLES = (
    'backtest_steps', 'backtest_decisions', 'backtest_orders',
    'backtest_order_updates', 'backtest_fills', 'backtest_positions',
    'backtest_equity_curve', 'backtest_metrics', 'backtest_data_preflight',
    'backtest_data_chunks', 'backtest_analysis_summaries',
)

def upgrade():
    # Fail closed when pre-existing result rows have no owning root.
    bind = op.get_bind()
    for table in ('backtest_steps','backtest_decisions','backtest_orders','backtest_fills','backtest_positions','backtest_equity_curve','backtest_metrics','backtest_data_chunks'):
        if bind.dialect.name != 'sqlite':
            op.execute(sa.text(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} r LEFT JOIN backtest_runs b ON b.id=r.run_id WHERE b.id IS NULL) THEN RAISE EXCEPTION 'orphan backtest result rows in {table}'; END IF; END $$;"))
    op.create_table('backtest_runs',
      sa.Column('id', sa.Uuid(), primary_key=True),
      sa.Column('tenant_id', sa.String(128), nullable=False, server_default='default'),
      sa.Column('run_kind', sa.String(40), nullable=False), sa.Column('profile', sa.String(80), nullable=False),
      sa.Column('status', sa.String(24), nullable=False, server_default='queued'),
      sa.Column('terminal_status', sa.String(24)), sa.Column('idempotency_key', sa.String(200)),
      sa.Column('config_hash', sa.String(64), nullable=False), sa.Column('backtest_config', postgresql.JSONB(), nullable=False),
      sa.Column('strategy_revision_id', sa.String(128)), sa.Column('strategy_source_hash', sa.String(128)),
      sa.Column('strategy_contract_version', sa.String(64)), sa.Column('parameters', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
      sa.Column('initial_cash', sa.String(64)), sa.Column('initial_positions', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
      sa.Column('data_request', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")), sa.Column('data_provider_key', sa.String(128)),
      sa.Column('max_lookback_sessions', sa.Integer(), nullable=False, server_default='512'),
      sa.Column('data_chunk_policy_key', sa.String(64), nullable=False, server_default='fixed_trading_sessions'),
      sa.Column('data_chunk_policy_version', sa.Integer(), nullable=False, server_default='1'), sa.Column('data_chunk_size_sessions', sa.Integer(), nullable=False, server_default='20'),
      sa.Column('data_admission_preflight_hash', sa.String(64)), sa.Column('data_preflight_hash', sa.String(64)),
      sa.Column('account_profile_id', sa.String(128)), sa.Column('account_profile_version', sa.String(64)),
      sa.Column('fee_schedule_key', sa.String(128)), sa.Column('fee_schedule_version', sa.String(64)),
      sa.Column('fee_schedule_snapshot', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
      sa.Column('analyzer_specs', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")), sa.Column('behavior_versions', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
      sa.Column('progress', sa.Numeric(6,5), nullable=False, server_default='0'), sa.Column('current_date', sa.String(32)),
      sa.Column('current_step', sa.Integer()), sa.Column('checkpoint', postgresql.JSONB(), nullable=False),
      sa.Column('completion_marker', postgresql.JSONB()), sa.Column('runner_exit_code', sa.Integer()), sa.Column('child_pid', sa.Integer()),
      sa.Column('process_start_token', sa.String(128)), sa.Column('process_group_id', sa.Integer()),
      sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
      sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
      sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
      sa.CheckConstraint("run_kind IN ('backtest_run','internal_link_acceptance')", name='backtest_run_kind_supported'),
      sa.CheckConstraint("(run_kind = 'backtest_run' AND profile = 'formal@1') OR (run_kind = 'internal_link_acceptance' AND profile = 'internal_link_acceptance@1')", name='backtest_kind_profile_match'),
      sa.CheckConstraint("status IN ('queued','starting','running','cancel_requested','terminal')", name='backtest_status_supported'),
      sa.CheckConstraint("terminal_status IS NULL OR terminal_status IN ('succeeded','failed','cancelled','timed_out','indeterminate')", name='backtest_terminal_supported'),
      sa.CheckConstraint('progress >= 0 AND progress <= 1', name='backtest_progress_range'),
      sa.CheckConstraint('length(config_hash) = 64', name='backtest_config_hash_sha256'),
      sa.CheckConstraint('max_lookback_sessions = 512', name='backtest_lookback_fixed'),
      sa.CheckConstraint("data_chunk_policy_key = 'fixed_trading_sessions' AND data_chunk_policy_version = 1 AND data_chunk_size_sessions = 20", name='backtest_chunk_policy_fixed'))
    op.create_index('ix_backtest_runs_queue','backtest_runs',['run_kind','status','created_at'])
    op.create_index('uq_backtest_runs_idempotency','backtest_runs',['tenant_id','idempotency_key'], unique=True)
    for table in RESULT_TABLES:
        op.execute(sa.text(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = '{table}') THEN IF EXISTS (SELECT 1 FROM {table} r LEFT JOIN backtest_runs b ON b.id = r.run_id WHERE b.id IS NULL) THEN RAISE EXCEPTION 'orphan run_id rows found in {table}'; END IF; END IF; END $$;"))
        op.create_foreign_key(f"fk_{table}_run_id", table, 'backtest_runs', ['run_id'], ['id'], ondelete='RESTRICT')

def downgrade():
    for table in reversed(RESULT_TABLES):
        op.drop_constraint(f"fk_{table}_run_id", table, type_='foreignkey')
    op.drop_table('backtest_runs')
