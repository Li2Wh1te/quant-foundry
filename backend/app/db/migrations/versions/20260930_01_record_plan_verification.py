"""Seal full typed-record governance derivations for bounded page execution."""
from alembic import op
import sqlalchemy as sa

revision = '20260930_01'
down_revision = '20260929_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table('foundation_record_plan_verifications',
        sa.Column('work_id', sa.Uuid(), sa.ForeignKey('foundation_work.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('validator_hash', sa.String(64), primary_key=True),
        sa.Column('work_fingerprint', sa.String(64), nullable=False),
        sa.Column('parameters_hash', sa.String(64), nullable=False),
        sa.Column('action_count', sa.Integer(), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False))
    op.execute('''CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE
        ON foundation_record_plan_verifications FOR EACH ROW EXECUTE FUNCTION foundation_immutable()''')
    op.execute('''CREATE FUNCTION foundation_guard_record_plan_verification() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM foundation_work WHERE id=NEW.work_id AND kind='B'
        AND candidate_manifest_id IS NOT NULL AND candidate_input_set_id IS NULL
        AND fingerprint=NEW.work_fingerprint
        AND parameters_json::jsonb->>'domain'='typed-record-v1'
        AND jsonb_array_length(parameters_json::jsonb->'actions')=NEW.action_count FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'plan verification requires matching immutable governance work'; END IF;
      RETURN NEW;
    END $$''')
    op.execute('''CREATE TRIGGER record_plan_verification_insert BEFORE INSERT
        ON foundation_record_plan_verifications FOR EACH ROW EXECUTE FUNCTION foundation_guard_record_plan_verification()''')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM foundation_record_plan_verifications)')):
        raise RuntimeError('Governance validation receipts exist; use a compatible application rollback')
    op.drop_table('foundation_record_plan_verifications')
    op.execute('DROP FUNCTION foundation_guard_record_plan_verification()')
