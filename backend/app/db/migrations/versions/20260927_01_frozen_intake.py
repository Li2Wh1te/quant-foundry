"""Retain exact local-version membership for restartable full intake."""
from alembic import op
import sqlalchemy as sa

revision = '20260927_01'
down_revision = '20260926_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(op.f('ck_foundation_intake_scans_scan_mode'), 'foundation_intake_scans', type_='check')
    op.create_check_constraint('scan_mode', 'foundation_intake_scans', "mode IN ('full','recent','frozen')")
    op.create_table(
        'foundation_intake_scan_targets',
        sa.Column('scan_id', sa.Uuid(), sa.ForeignKey('foundation_intake_scans.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('observation_id', sa.Uuid(), sa.ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('content_hash', sa.String(64), nullable=False),
    )
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_scan_targets '
               'FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.create_table(
        'foundation_intake_scan_failures',
        sa.Column('scan_id', sa.Uuid(), sa.ForeignKey('foundation_intake_scans.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('observation_id', sa.Uuid(), sa.ForeignKey('tonghuashun_observations.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('error_code', sa.String(64), nullable=False),
    )
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_scan_failures '
               'FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.create_table(
        'foundation_intake_scan_seals',
        sa.Column('scan_id', sa.Uuid(), sa.ForeignKey('foundation_intake_scans.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('target_count', sa.Integer(), nullable=False),
        sa.CheckConstraint('target_count >= 0', name='target_count'),
    )
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_scan_seals '
               'FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("""CREATE FUNCTION foundation_guard_scan_target() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM foundation_intake_scans WHERE id = NEW.scan_id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM foundation_intake_scan_seals WHERE scan_id = NEW.scan_id)
      THEN RAISE EXCEPTION 'frozen scan membership is sealed'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER foundation_scan_target_insert BEFORE INSERT ON foundation_intake_scan_targets '
               'FOR EACH ROW EXECUTE FUNCTION foundation_guard_scan_target()')
    op.execute("""CREATE FUNCTION foundation_guard_frozen_observation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM foundation_intake_scan_targets WHERE observation_id = OLD.id)
      THEN RAISE EXCEPTION 'frozen observation is immutable' USING ERRCODE = '23514'; END IF;
      RETURN NEW;
    END $$""")
    op.create_index('ix_foundation_intake_scan_targets_observation_id', 'foundation_intake_scan_targets', ['observation_id'])
    op.execute('CREATE TRIGGER foundation_frozen_observation_guard BEFORE UPDATE ON tonghuashun_observations '
               'FOR EACH ROW EXECUTE FUNCTION foundation_guard_frozen_observation()')
    op.create_table(
        'foundation_intake_campaigns',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_key', sa.String(128), nullable=False, unique=True),
        sa.Column('decoder_id', sa.Uuid(), sa.ForeignKey('foundation_execution_manifests.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('datasets_json', sa.Text(), nullable=False),
    )
    op.create_table(
        'foundation_intake_campaign_scans',
        sa.Column('campaign_id', sa.Uuid(), sa.ForeignKey('foundation_intake_campaigns.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('dataset', sa.String(80), primary_key=True),
        sa.Column('scan_id', sa.Uuid(), sa.ForeignKey('foundation_intake_scans.id', ondelete='RESTRICT'), nullable=False, unique=True),
    )
    for table in ('foundation_intake_campaigns', 'foundation_intake_campaign_scans'):
        op.execute(f'CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')


def downgrade():
    # Never discard a full-run denominator or rewrite old scan modes on rollback.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM foundation_intake_scans WHERE mode = 'frozen')")):
        raise RuntimeError('Frozen input evidence exists; use a compatible application rollback')
    if connection.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM foundation_intake_campaigns)')):
        raise RuntimeError('Intake campaign evidence exists; use a compatible application rollback')
    op.drop_table('foundation_intake_campaign_scans')
    op.drop_table('foundation_intake_campaigns')
    op.execute('DROP TRIGGER foundation_frozen_observation_guard ON tonghuashun_observations')
    op.execute('DROP FUNCTION foundation_guard_frozen_observation()')
    op.drop_table('foundation_intake_scan_failures')
    op.drop_table('foundation_intake_scan_seals')
    op.drop_table('foundation_intake_scan_targets')
    op.execute('DROP FUNCTION foundation_guard_scan_target()')
    op.drop_constraint(op.f('ck_foundation_intake_scans_scan_mode'), 'foundation_intake_scans', type_='check')
    op.create_check_constraint('scan_mode', 'foundation_intake_scans', "mode IN ('full','recent')")
