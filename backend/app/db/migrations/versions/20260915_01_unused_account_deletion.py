"""Allow explicit removal of unused accounts while protecting referenced history."""
from alembic import op

revision = "20260915_01"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


def _replace(delete_guard: str):
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE OR REPLACE FUNCTION protect_backtest_account_version() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        """ + delete_guard + """
      END IF;
      IF NEW.profile_id IS DISTINCT FROM OLD.profile_id OR NEW.version IS DISTINCT FROM OLD.version
         OR NEW.snapshot IS DISTINCT FROM OLD.snapshot OR NEW.created_at IS DISTINCT FROM OLD.created_at
      THEN RAISE EXCEPTION 'account version configuration is immutable'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")


def upgrade():
    # Require both the explicit service path and absence of references across
    # all versions/owners. Ordinary version deletion remains prohibited.
    _replace("""
      IF current_setting('qf.deleting_account', true) IS DISTINCT FROM OLD.profile_id::text
         OR EXISTS (SELECT 1 FROM backtest_runs WHERE lower(btrim(account_profile_id)) = OLD.profile_id::text)
      THEN RAISE EXCEPTION 'account versions cannot be deleted'; END IF;
      RETURN OLD;
    """)


def downgrade():
    _replace("RAISE EXCEPTION 'account versions cannot be deleted';")
