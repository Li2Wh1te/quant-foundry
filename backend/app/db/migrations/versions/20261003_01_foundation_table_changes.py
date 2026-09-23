"""Capture local table changes atomically without periodic full-table copies.

The journal is append-only. Serial IDs identify occurrences, not commit order;
consumers must select unreceipted rows rather than advancing an ID watermark.
Trigger installation and schema changes commit together. No existing market
rows are copied by this migration; bootstrap compares the fixed capture later.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261003_01'
down_revision = '20261002_01'
branch_labels = None
depends_on = None

# Freeze migration metadata instead of importing a future application's ORM.
TABLES = (
    ('trading_calendar_days', 'exchange_calendar', '', 'exchange,calendar_date'),
    ('etf_codes', 'etf_directory', 'source', 'source,ts_code'),
    ('etf_code_mapping_audits', 'etf_code_mapping_audits', 'source', 'id'),
    ('etf_daily_bars', 'etf_daily', 'source', 'source,ts_code,trade_date'),
    ('etf_daily_bar_revision_audits', 'etf_daily_revision_audits', 'source', 'id'),
    ('etf_adjustment_factors', 'etf_adjustment_factors', 'source', 'source,ts_code,trade_date'),
    ('corporate_action_source_facts', 'corporate_action_source_facts', 'source', 'id'),
    ('corporate_action_facts', 'corporate_action_facts', 'source', 'event_id'),
    ('corporate_action_coverage_facts', 'corporate_action_coverage_facts', 'source', 'id'),
    ('trading_status_source_facts', 'trading_status_source_facts', 'source', 'id'),
    ('trading_status_facts', 'trading_status_facts', 'source', 'ts_code,trade_date'),
    ('trading_status_coverage_facts', 'trading_status_coverage_facts', 'source', 'id'),
    ('trading_status_fact_revision_audits', 'trading_status_revision_audits', 'previous_source', 'id'),
)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table('foundation_table_changes',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('dataset', sa.String(80), nullable=False),
        sa.Column('key_json', sa.Text(), nullable=False),
        sa.Column('before_json', sa.Text()), sa.Column('after_json', sa.Text()),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('event_key', sa.String(128), unique=True),
        sa.CheckConstraint('before_json IS NOT NULL OR after_json IS NOT NULL', name='table_change_nonempty'))
    op.create_index('ix_foundation_table_changes_dataset_id', 'foundation_table_changes', ['dataset', 'id'])
    op.create_table('foundation_table_change_visits',
        sa.Column('change_id', sa.BigInteger(), sa.ForeignKey('foundation_table_changes.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('execution_id', sa.Uuid(), sa.ForeignKey('foundation_execution_manifests.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('visited_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('visits', sa.Integer(), nullable=False),
        sa.Column('source_ref_id', sa.Uuid(), sa.ForeignKey('foundation_source_refs.id', ondelete='RESTRICT')),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.CheckConstraint('visits > 0', name='table_change_visit_positive'))
    op.alter_column('foundation_work_source_pointers', 'revision', type_=sa.BigInteger(),
                    existing_type=sa.Integer(), existing_nullable=False)
    op.execute('''CREATE FUNCTION foundation_lock_table_change() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE old_value jsonb; new_value jsonb; old_identity jsonb; new_identity jsonb;
            identity_value jsonb; column_name text;
    BEGIN
        IF TG_OP <> 'INSERT' THEN old_value := to_jsonb(OLD); END IF;
        IF TG_OP <> 'DELETE' THEN new_value := to_jsonb(NEW); END IF;
        IF TG_ARGV[1] <> '' THEN
            IF old_value ->> TG_ARGV[1] IS DISTINCT FROM 'tushare' THEN old_value := NULL; END IF;
            IF new_value ->> TG_ARGV[1] IS DISTINCT FROM 'tushare' THEN new_value := NULL; END IF;
        END IF;
        old_identity := '{}'::jsonb; new_identity := '{}'::jsonb;
        FOREACH column_name IN ARRAY string_to_array(TG_ARGV[2], ',') LOOP
            IF old_value IS NOT NULL THEN old_identity := old_identity || jsonb_build_object(column_name, old_value -> column_name); END IF;
            IF new_value IS NOT NULL THEN new_identity := new_identity || jsonb_build_object(column_name, new_value -> column_name); END IF;
        END LOOP;
        -- Publication takes the same advisory lock before checking current
        -- state. A new insert cannot appear between a deletion check and head
        -- activation. Primary-key moves lock both identities in one order.
        FOR identity_value IN SELECT value FROM (VALUES
            (CASE WHEN old_value IS NOT NULL THEN old_identity END),
            (CASE WHEN new_value IS NOT NULL THEN new_identity END)) AS keys(value)
            WHERE value IS NOT NULL ORDER BY value::text LOOP
            PERFORM pg_advisory_xact_lock(hashtextextended(TG_TABLE_NAME || '|' || identity_value::text, 0));
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$''')
    op.execute('''CREATE FUNCTION foundation_capture_table_change() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE old_value jsonb; new_value jsonb; old_identity jsonb; new_identity jsonb; identity_value jsonb; column_name text;
    BEGIN
        IF TG_OP <> 'INSERT' THEN old_value := to_jsonb(OLD); END IF;
        IF TG_OP <> 'DELETE' THEN new_value := to_jsonb(NEW); END IF;
        IF TG_ARGV[1] <> '' THEN
            IF old_value ->> TG_ARGV[1] IS DISTINCT FROM 'tushare' THEN old_value := NULL; END IF;
            IF new_value ->> TG_ARGV[1] IS DISTINCT FROM 'tushare' THEN new_value := NULL; END IF;
        END IF;
        IF old_value IS NULL AND new_value IS NULL THEN RETURN NULL; END IF;
        -- Timestamp-only collection touches do not create duplicate market
        -- evidence. Effective/known/revision timestamps remain substantive.
        IF old_value IS NOT NULL AND new_value IS NOT NULL AND
            old_value - ARRAY['updated_at','last_seen_at'] = new_value - ARRAY['updated_at','last_seen_at'] THEN
            RETURN NULL;
        END IF;
        old_identity := '{}'::jsonb; new_identity := '{}'::jsonb;
        FOREACH column_name IN ARRAY string_to_array(TG_ARGV[2], ',') LOOP
            IF old_value IS NOT NULL THEN
                old_identity := old_identity || jsonb_build_object(column_name, old_value -> column_name);
            END IF;
            IF new_value IS NOT NULL THEN
                new_identity := new_identity || jsonb_build_object(column_name, new_value -> column_name);
            END IF;
        END LOOP;
        IF old_value IS NOT NULL AND new_value IS NOT NULL AND old_identity <> new_identity THEN
            -- A primary-key move withdraws the old source-local identity and
            -- introduces the new one; a single new-key entry loses deletion.
            INSERT INTO foundation_table_changes(dataset,key_json,before_json,after_json)
            VALUES(TG_ARGV[0],old_identity::text,old_value::text,NULL);
            INSERT INTO foundation_table_changes(dataset,key_json,before_json,after_json)
            VALUES(TG_ARGV[0],new_identity::text,NULL,new_value::text);
            RETURN NULL;
        END IF;
        identity_value := CASE WHEN new_value IS NOT NULL THEN new_identity ELSE old_identity END;
        INSERT INTO foundation_table_changes(dataset,key_json,before_json,after_json)
        VALUES(TG_ARGV[0],identity_value::text,old_value::text,new_value::text);
        RETURN NULL;
    END $$''')
    op.execute('''CREATE FUNCTION foundation_reject_table_change_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Fixed table change evidence is immutable'; END $$''')
    op.execute('''CREATE TRIGGER foundation_table_changes_immutable BEFORE UPDATE OR DELETE
                  ON foundation_table_changes FOR EACH ROW EXECUTE FUNCTION foundation_reject_table_change_mutation()''')
    op.execute('''CREATE TRIGGER foundation_table_changes_no_truncate BEFORE TRUNCATE
                  ON foundation_table_changes EXECUTE FUNCTION foundation_reject_table_change_mutation()''')
    for table, dataset, source_column, keys in TABLES:
        op.execute(f"CREATE TRIGGER foundation_lock_change BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION foundation_lock_table_change('{dataset}','{source_column}','{keys}')")
        op.execute(f"CREATE TRIGGER foundation_capture_change AFTER INSERT OR UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION foundation_capture_table_change('{dataset}','{source_column}','{keys}')")
        # TRUNCATE bypasses row triggers; reject it rather than silently lose
        # removals. Ordinary DELETE retains every before-image transactionally.
        op.execute(f'CREATE TRIGGER foundation_source_no_truncate BEFORE TRUNCATE ON {table} '
                   'EXECUTE FUNCTION foundation_reject_table_change_mutation()')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT EXISTS (SELECT 1 FROM foundation_table_changes)')):
        raise RuntimeError('Fixed local changes exist; use a compatible application rollback')
    for table, *_ in TABLES:
        op.execute(f'DROP TRIGGER foundation_lock_change ON {table}')
        op.execute(f'DROP TRIGGER foundation_capture_change ON {table}')
        op.execute(f'DROP TRIGGER foundation_source_no_truncate ON {table}')
    op.drop_table('foundation_table_change_visits')
    op.drop_table('foundation_table_changes')
    op.alter_column('foundation_work_source_pointers', 'revision', type_=sa.Integer(),
                    existing_type=sa.BigInteger(), existing_nullable=False)
    op.execute('DROP FUNCTION foundation_capture_table_change()')
    op.execute('DROP FUNCTION foundation_lock_table_change()')
    op.execute('DROP FUNCTION foundation_reject_table_change_mutation()')
