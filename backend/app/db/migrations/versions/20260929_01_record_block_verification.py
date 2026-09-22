"""Retain validator receipts only for closed immutable typed-record blocks."""
from alembic import op
import sqlalchemy as sa

revision = '20260929_01'
down_revision = '20260928_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table('foundation_record_block_verifications',
        sa.Column('block_id', sa.Uuid(), sa.ForeignKey('foundation_release_blocks.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('validator_hash', sa.String(64), primary_key=True),
        sa.Column('scope_key', sa.String(64), nullable=False),
        sa.Column('schema_key', sa.String(128), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('governance_work_ids_json', sa.Text(), nullable=False),
        sa.Column('normalization_work_ids_json', sa.Text(), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False))
    op.execute('''CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE
        ON foundation_record_block_verifications FOR EACH ROW EXECUTE FUNCTION foundation_immutable()''')
    op.execute('''CREATE FUNCTION foundation_guard_record_verification() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      -- Lock the same row used by member insertion and reference sealing. A
      -- receipt can never race the insertion of another member into its block.
      PERFORM 1 FROM foundation_release_blocks WHERE id = NEW.block_id
        AND content_hash = NEW.content_hash AND row_count = NEW.row_count FOR UPDATE;
      IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM foundation_release_block_refs WHERE block_id = NEW.block_id)
      THEN RAISE EXCEPTION 'verification requires a closed matching block'; END IF;
      RETURN NEW;
    END $$''')
    op.execute('''CREATE TRIGGER record_verification_insert BEFORE INSERT
        ON foundation_record_block_verifications FOR EACH ROW EXECUTE FUNCTION foundation_guard_record_verification()''')


def downgrade():
    # Validation receipts are retained audit evidence, like the sealed blocks
    # they describe. An older compatible application can simply ignore them.
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM foundation_record_block_verifications)')):
        raise RuntimeError('Block validation receipts exist; use a compatible application rollback')
    op.drop_table('foundation_record_block_verifications')
    op.execute('DROP FUNCTION foundation_guard_record_verification()')
