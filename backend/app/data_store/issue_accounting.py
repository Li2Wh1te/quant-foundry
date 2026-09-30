"""Transactional physical issue totals; logical evidence remains in each issue.

The inventory has one derived row per dataset. Normal writes never recount the
full issue catalog. Installation takes the writer-excluding table lock before
counting existing records and enabling triggers in the same transaction.
"""
from sqlalchemy import text

DDL = """
CREATE TABLE data_store_issue_totals (
    dataset varchar(128) PRIMARY KEY REFERENCES data_store_datasets(name) ON DELETE RESTRICT,
    physical_records bigint NOT NULL CHECK (physical_records >= 0),
    affected_objects bigint NOT NULL CHECK (affected_objects >= 0)
);
CREATE FUNCTION data_store_count_issue_records() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_count bigint := 0;
    new_count bigint := 0;
BEGIN
    IF TG_OP IN ('DELETE','UPDATE') THEN
        old_count := CASE WHEN OLD.target_json::jsonb->>'scope_version'='object-key-set-v1'
            THEN greatest(1,jsonb_array_length(OLD.target_json::jsonb->'members')) ELSE 1 END;
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        new_count := CASE WHEN NEW.target_json::jsonb->>'scope_version'='object-key-set-v1'
            THEN greatest(1,jsonb_array_length(NEW.target_json::jsonb->'members')) ELSE 1 END;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        DELETE FROM data_store_issue_totals;
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.dataset = NEW.dataset THEN
        UPDATE data_store_issue_totals SET affected_objects=affected_objects+new_count-old_count
        WHERE dataset=NEW.dataset;
        IF NOT FOUND THEN RAISE EXCEPTION 'Current issue inventory is missing'; END IF;
        RETURN NULL;
    END IF;
    IF TG_OP IN ('DELETE','UPDATE') THEN
        UPDATE data_store_issue_totals SET physical_records=physical_records-1,affected_objects=affected_objects-old_count
        WHERE dataset=OLD.dataset;
        IF NOT FOUND THEN RAISE EXCEPTION 'Current issue inventory is missing'; END IF;
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        INSERT INTO data_store_issue_totals(dataset,physical_records,affected_objects) VALUES(NEW.dataset,1,new_count)
        ON CONFLICT(dataset) DO UPDATE SET physical_records=data_store_issue_totals.physical_records+1,
            affected_objects=data_store_issue_totals.affected_objects+new_count;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER data_store_issue_records_row AFTER INSERT OR DELETE OR UPDATE
    ON data_store_issues FOR EACH ROW EXECUTE FUNCTION data_store_count_issue_records();
CREATE TRIGGER data_store_issue_records_truncate AFTER TRUNCATE
    ON data_store_issues FOR EACH STATEMENT EXECUTE FUNCTION data_store_count_issue_records();
CREATE INDEX ix_data_store_issues_scope_page ON data_store_issues(dataset,scope_key,issue_key);
"""

def install(connection):
    """Build an exact cache without changing issue rows or source evidence."""
    connection.execute(text('LOCK TABLE data_store_issues IN SHARE ROW EXCLUSIVE MODE'))
    connection.execute(text(DDL))
    from .issue_sets import COUNT_SQL
    connection.execute(text('INSERT INTO data_store_issue_totals(dataset,physical_records,affected_objects) '
                            'SELECT dataset,count(*),sum('+COUNT_SQL+') FROM data_store_issues GROUP BY dataset'))


def check_enabled(connection):
    """A disabled cache producer must never turn real restrictions into zero."""
    from .errors import DataStoreError
    enabled=connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_trigger "
        "WHERE tgrelid='data_store_issues'::regclass AND tgname='data_store_issue_records_row' "
        "AND tgenabled IN ('O','A'))")).scalar_one()
    if not enabled:raise DataStoreError('CATALOG_MISMATCH')
