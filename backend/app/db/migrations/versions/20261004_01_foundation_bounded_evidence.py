"""Retain bounded preparation/proof pages and fixed-source settlement edges.

Only new tables and small metadata guards are installed here. No historical
source, publication or proof is rewritten. Concurrent indexes have their own
resumable migration, since that operation cannot share this transaction.
"""
from alembic import op
from sqlalchemy import text

revision = '20261004_01'
down_revision = '20261003_01'
branch_labels = None
depends_on = None

TABLES = ('foundation_record_preparations', 'foundation_record_prepared_pages', 'foundation_record_validation_roots', 'foundation_record_block_page_verifications', 'foundation_backfill_probes', 'foundation_backfill_receipts', 'foundation_backfill_completions', 'foundation_table_settlement_visits', 'foundation_table_settlement_receipts', 'foundation_backfill_resume_watches')

DDL = (
    """CREATE TABLE foundation_record_preparations (
	work_id UUID NOT NULL,
	work_fingerprint VARCHAR(64) NOT NULL,
	source_hash VARCHAR(64) NOT NULL,
	row_count INTEGER NOT NULL,
	page_hashes_json TEXT NOT NULL,
	duplicate_keys_json TEXT NOT NULL,
	manifest_hash VARCHAR(64) NOT NULL,
	sealed BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_record_preparations PRIMARY KEY (work_id),
	CONSTRAINT ck_foundation_record_preparations_preparation_count CHECK (row_count >= 0),
	CONSTRAINT fk_foundation_record_preparations_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_record_prepared_pages (
	work_id UUID NOT NULL,
	ordinal INTEGER NOT NULL,
	payload_json TEXT NOT NULL,
	keys_json TEXT NOT NULL,
	row_count INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	CONSTRAINT pk_foundation_record_prepared_pages PRIMARY KEY (work_id, ordinal),
	CONSTRAINT ck_foundation_record_prepared_pages_prepared_page_bounds CHECK (ordinal >= 0 AND row_count BETWEEN 1 AND 200),
	CONSTRAINT fk_foundation_record_prepared_pages_work_id_foundation__bb05 FOREIGN KEY(work_id) REFERENCES foundation_record_preparations (work_id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_record_validation_roots (
	release_id UUID NOT NULL,
	manifest_hash VARCHAR(64) NOT NULL,
	ref_count INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_record_validation_roots PRIMARY KEY (release_id),
	CONSTRAINT ck_foundation_record_validation_roots_validation_root_count CHECK (ref_count >= 0),
	CONSTRAINT fk_foundation_record_validation_roots_release_id_founda_840f FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_record_block_page_verifications (
	block_id UUID NOT NULL,
	validator_hash VARCHAR(64) NOT NULL,
	ordinal INTEGER NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	schema_key VARCHAR(128) NOT NULL,
	after_key VARCHAR(64),
	last_key VARCHAR(64) NOT NULL,
	row_count INTEGER NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	governance_work_ids_json TEXT NOT NULL,
	normalization_work_ids_json TEXT NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_record_block_page_verifications PRIMARY KEY (block_id, validator_hash, ordinal),
	CONSTRAINT ck_foundation_record_block_page_verifications_block_page_bounds CHECK (ordinal >= 0 AND row_count BETWEEN 1 AND 200),
	CONSTRAINT fk_foundation_record_block_page_verifications_block_id__7c90 FOREIGN KEY(block_id) REFERENCES foundation_release_blocks (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_backfill_probes (
	scan_id UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	target_count INTEGER NOT NULL,
	cursor_id UUID,
	processed INTEGER NOT NULL,
	accepted INTEGER NOT NULL,
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_backfill_probes PRIMARY KEY (scan_id, scope_key),
	CONSTRAINT ck_foundation_backfill_probes_backfill_probe_counts CHECK (target_count >= processed AND processed >= accepted AND accepted >= 0),
	CONSTRAINT fk_foundation_backfill_probes_scan_id_foundation_intake_scans FOREIGN KEY(scan_id) REFERENCES foundation_intake_scans (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_backfill_receipts (
	scan_id UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	observation_id UUID NOT NULL,
	quality VARCHAR(16) NOT NULL,
	release_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_backfill_receipts PRIMARY KEY (scan_id, scope_key, observation_id, quality),
	CONSTRAINT fk_foundation_backfill_receipts_scan_id_foundation_back_2672 FOREIGN KEY(scan_id, scope_key) REFERENCES foundation_backfill_probes (scan_id, scope_key) ON DELETE RESTRICT,
	CONSTRAINT fk_foundation_backfill_receipts_scan_id_foundation_inta_6727 FOREIGN KEY(scan_id, observation_id) REFERENCES foundation_intake_scan_targets (scan_id, observation_id) ON DELETE RESTRICT,
	CONSTRAINT ck_foundation_backfill_receipts_backfill_receipt_quality CHECK (quality IN ('processed', 'accepted')),
	CONSTRAINT fk_foundation_backfill_receipts_release_id_foundation_o_856b FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_backfill_completions (
	scan_id UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	quality VARCHAR(16) NOT NULL,
	target_count INTEGER NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_backfill_completions PRIMARY KEY (scan_id, scope_key, quality),
	CONSTRAINT fk_foundation_backfill_completions_scan_id_foundation_b_6d76 FOREIGN KEY(scan_id, scope_key) REFERENCES foundation_backfill_probes (scan_id, scope_key) ON DELETE RESTRICT,
	CONSTRAINT ck_foundation_backfill_completions_backfill_completion_quality CHECK (quality IN ('processed', 'accepted') AND target_count >= 0)
)""",
    """CREATE TABLE foundation_table_settlement_visits (
	capture_ref_id UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	native_dataset VARCHAR(80) NOT NULL,
	source_ids_json TEXT NOT NULL,
	cursor_id UUID,
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_table_settlement_visits PRIMARY KEY (capture_ref_id, scope_key),
	CONSTRAINT fk_foundation_table_settlement_visits_capture_ref_id_fo_4045 FOREIGN KEY(capture_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_table_settlement_receipts (
	capture_ref_id UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	source_ref_id UUID NOT NULL,
	release_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_table_settlement_receipts PRIMARY KEY (capture_ref_id, scope_key, source_ref_id),
	CONSTRAINT fk_foundation_table_settlement_receipts_capture_ref_id__2844 FOREIGN KEY(capture_ref_id, scope_key) REFERENCES foundation_table_settlement_visits (capture_ref_id, scope_key) ON DELETE RESTRICT,
	CONSTRAINT fk_foundation_table_settlement_receipts_source_ref_id_f_9268 FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT,
	CONSTRAINT fk_foundation_table_settlement_receipts_release_id_foun_1640 FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE foundation_backfill_resume_watches (
	task_id UUID NOT NULL,
	task_version INTEGER NOT NULL,
	parameters_hash VARCHAR(64) NOT NULL,
	state VARCHAR(16) NOT NULL,
	reason VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_foundation_backfill_resume_watches PRIMARY KEY (task_id, task_version),
	CONSTRAINT ck_foundation_backfill_resume_watches_resume_watch_state CHECK (task_version > 0 AND state IN ('waiting','activated','resumed','cancelled')),
	CONSTRAINT fk_foundation_backfill_resume_watches_task_id_scheduled_tasks FOREIGN KEY(task_id) REFERENCES scheduled_tasks (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_foundation_backfill_resume_watches_state ON foundation_backfill_resume_watches (state)""",
)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    for statement in DDL:
        op.execute(statement)
    for table in ('foundation_record_prepared_pages', 'foundation_record_validation_roots',
                  'foundation_record_block_page_verifications', 'foundation_backfill_receipts',
                  'foundation_backfill_completions', 'foundation_table_settlement_receipts'):
        op.execute(f'CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    for statement in GUARDS:
        op.execute(statement)


GUARDS = (
"""CREATE FUNCTION foundation_guard_record_preparation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE hashes jsonb; duplicates jsonb; actual_count integer;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'preparation evidence is retained'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.sealed OR NEW.page_hashes_json <> '[]' OR NEW.duplicate_keys_json <> '[]' OR NEW.manifest_hash <> ''
      OR NOT EXISTS (SELECT 1 FROM foundation_work w JOIN foundation_source_refs s ON s.id=w.source_ref_id
        WHERE w.id=NEW.work_id AND w.kind='A' AND w.fingerprint=NEW.work_fingerprint AND s.content_hash=NEW.source_hash)
    THEN RAISE EXCEPTION 'preparation requires fixed unsealed input'; END IF;
    RETURN NEW;
  END IF;
  IF OLD.sealed OR NOT NEW.sealed OR
    (to_jsonb(NEW)-ARRAY['sealed','page_hashes_json','duplicate_keys_json','manifest_hash']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['sealed','page_hashes_json','duplicate_keys_json','manifest_hash'])
  THEN RAISE EXCEPTION 'preparation may only seal its fixed input once'; END IF;
  SELECT COALESCE(jsonb_agg(content_hash ORDER BY ordinal),'[]'::jsonb), COALESCE(sum(row_count),0)
    INTO hashes, actual_count FROM foundation_record_prepared_pages WHERE work_id=NEW.work_id;
  SELECT COALESCE(jsonb_agg(k ORDER BY k),'[]'::jsonb) INTO duplicates FROM (
    SELECT k FROM foundation_record_prepared_pages p, jsonb_array_elements_text(p.keys_json::jsonb) AS k
    WHERE p.work_id=NEW.work_id AND k IS NOT NULL GROUP BY k HAVING count(*) <> 1) repeated;
  IF actual_count <> NEW.row_count OR hashes <> NEW.page_hashes_json::jsonb
    OR duplicates <> NEW.duplicate_keys_json::jsonb OR length(NEW.manifest_hash) <> 64
  THEN RAISE EXCEPTION 'preparation seal requires complete pages and global duplicate evidence'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER record_preparation_guard BEFORE INSERT OR UPDATE OR DELETE ON foundation_record_preparations
FOR EACH ROW EXECUTE FUNCTION foundation_guard_record_preparation()""",
"""CREATE FUNCTION foundation_guard_prepared_page() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE header foundation_record_preparations%ROWTYPE; next_page integer; actual_keys jsonb;
BEGIN
  SELECT * INTO header FROM foundation_record_preparations WHERE work_id=NEW.work_id FOR UPDATE;
  IF NOT FOUND OR header.sealed THEN RAISE EXCEPTION 'sealed preparation cannot receive pages'; END IF;
  SELECT COALESCE(max(ordinal)+1,0) INTO next_page FROM foundation_record_prepared_pages WHERE work_id=NEW.work_id;
  SELECT COALESCE(jsonb_agg(value->'key' ORDER BY ordinal),'[]'::jsonb) INTO actual_keys
    FROM jsonb_array_elements(NEW.payload_json::jsonb) WITH ORDINALITY AS entries(value,ordinal);
  IF NEW.ordinal <> next_page OR NEW.row_count <> LEAST(200,header.row_count-NEW.ordinal*200)
    OR jsonb_array_length(NEW.payload_json::jsonb) <> NEW.row_count OR actual_keys <> NEW.keys_json::jsonb
  THEN RAISE EXCEPTION 'prepared page range or duplicate-key evidence is invalid'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER prepared_page_insert BEFORE INSERT ON foundation_record_prepared_pages
FOR EACH ROW EXECUTE FUNCTION foundation_guard_prepared_page()""",
"""CREATE FUNCTION foundation_guard_validation_root() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM foundation_official_releases WHERE id=NEW.release_id AND status='draft'
    AND manifest_hash=NEW.manifest_hash FOR UPDATE;
  IF NOT FOUND OR NEW.ref_count <> (SELECT count(*) FROM foundation_release_block_refs WHERE release_id=NEW.release_id)
  THEN RAISE EXCEPTION 'validation root requires a complete matching draft'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER validation_root_insert BEFORE INSERT ON foundation_record_validation_roots
FOR EACH ROW EXECUTE FUNCTION foundation_guard_validation_root()""",
"""CREATE OR REPLACE FUNCTION foundation_guard_ref_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT status FROM foundation_official_releases WHERE id=NEW.release_id FOR UPDATE) <> 'draft'
    OR EXISTS (SELECT 1 FROM foundation_record_validation_roots WHERE release_id=NEW.release_id)
  THEN RAISE EXCEPTION 'sealed or validating release cannot receive references'; END IF;
  PERFORM 1 FROM foundation_release_blocks WHERE id=NEW.block_id FOR UPDATE;
  RETURN NEW;
END $$""",
"""CREATE FUNCTION foundation_guard_block_page_proof() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior foundation_record_block_page_verifications%ROWTYPE;
BEGIN
  PERFORM 1 FROM foundation_release_blocks WHERE id=NEW.block_id FOR UPDATE;
  IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM foundation_release_block_refs WHERE block_id=NEW.block_id)
  THEN RAISE EXCEPTION 'page verification requires a closed block'; END IF;
  SELECT * INTO prior FROM foundation_record_block_page_verifications WHERE block_id=NEW.block_id
    AND validator_hash=NEW.validator_hash AND ordinal=NEW.ordinal;
  IF FOUND THEN
    IF (to_jsonb(prior)-'verified_at') IS DISTINCT FROM (to_jsonb(NEW)-'verified_at')
    THEN RAISE EXCEPTION 'page verification conflicts with immutable evidence'; END IF;
    RETURN NEW;
  END IF;
  IF NEW.ordinal=0 THEN
    IF NEW.after_key IS NOT NULL THEN RAISE EXCEPTION 'first proof page cannot skip members'; END IF;
  ELSE
    SELECT * INTO prior FROM foundation_record_block_page_verifications WHERE block_id=NEW.block_id
      AND validator_hash=NEW.validator_hash AND ordinal=NEW.ordinal-1;
    IF NOT FOUND OR prior.row_count<>200 OR prior.last_key IS DISTINCT FROM NEW.after_key
      OR prior.scope_key<>NEW.scope_key OR prior.schema_key<>NEW.schema_key
    THEN RAISE EXCEPTION 'proof pages must be contiguous in one scope'; END IF;
  END IF;
  IF NEW.after_key IS NOT NULL AND NEW.last_key <= NEW.after_key
  THEN RAISE EXCEPTION 'proof page key range must advance'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER block_page_proof_insert BEFORE INSERT ON foundation_record_block_page_verifications
FOR EACH ROW EXECUTE FUNCTION foundation_guard_block_page_proof()""",
"""CREATE FUNCTION foundation_guard_backfill_probe() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'settlement scope is retained'; END IF;
  IF TG_OP='INSERT' THEN
    IF NOT EXISTS (SELECT 1 FROM foundation_intake_scan_seals WHERE scan_id=NEW.scan_id AND target_count=NEW.target_count)
    THEN RAISE EXCEPTION 'settlement requires fixed capture membership'; END IF;
  ELSIF NEW.scan_id<>OLD.scan_id OR NEW.scope_key<>OLD.scope_key OR NEW.target_count<>OLD.target_count
  THEN RAISE EXCEPTION 'settlement denominator is immutable'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER backfill_probe_guard BEFORE INSERT OR UPDATE OR DELETE ON foundation_backfill_probes
FOR EACH ROW EXECUTE FUNCTION foundation_guard_backfill_probe()""",
"""CREATE FUNCTION foundation_guard_backfill_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM foundation_official_releases r
    JOIN foundation_work g ON g.id=r.work_id
    JOIN foundation_candidate_manifests m ON m.id=g.candidate_manifest_id
    JOIN foundation_work a ON a.id=m.work_id
    JOIN foundation_source_refs s ON s.id=a.source_ref_id
    JOIN foundation_intake_scan_targets t ON t.observation_id=s.observation_id AND t.scan_id=NEW.scan_id
    WHERE r.id=NEW.release_id AND r.status='published' AND r.scope_key=NEW.scope_key
      AND a.kind='A' AND a.status='succeeded' AND g.kind='B'
      AND s.source='tonghuashun' AND s.observation_id=NEW.observation_id AND s.content_hash=t.content_hash
      AND g.parameters_json NOT LIKE '%OLDER_SOURCE_RETAINED%'
      AND (NEW.quality='processed' OR NOT EXISTS (
        SELECT 1 FROM foundation_candidates c WHERE c.work_id=a.id AND c.readiness<>'ready')))
  THEN RAISE EXCEPTION 'settlement requires actual qualified published source evidence'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER backfill_receipt_insert BEFORE INSERT ON foundation_backfill_receipts
FOR EACH ROW EXECUTE FUNCTION foundation_guard_backfill_receipt()""",
"""CREATE FUNCTION foundation_guard_backfill_completion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE fixed integer;
BEGIN
  SELECT target_count INTO fixed FROM foundation_backfill_probes WHERE scan_id=NEW.scan_id AND scope_key=NEW.scope_key FOR UPDATE;
  IF NOT FOUND OR fixed<>NEW.target_count OR fixed<>(SELECT count(*) FROM foundation_backfill_receipts
    WHERE scan_id=NEW.scan_id AND scope_key=NEW.scope_key AND quality=NEW.quality)
  THEN RAISE EXCEPTION 'completion requires every fixed source publication receipt'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER backfill_completion_insert BEFORE INSERT ON foundation_backfill_completions
FOR EACH ROW EXECUTE FUNCTION foundation_guard_backfill_completion()""",
"""CREATE FUNCTION foundation_guard_table_settlement_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE members jsonb;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'table settlement scope is retained'; END IF;
  IF TG_OP='UPDATE' THEN
    IF (to_jsonb(NEW)-ARRAY['cursor_id','checked_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['cursor_id','checked_at'])
    THEN RAISE EXCEPTION 'table settlement capture is immutable'; END IF;
    RETURN NEW;
  END IF;
  SELECT jsonb_agg(chunk.value->>'source_ref_id' ORDER BY chunk.value->>'source_ref_id') INTO members
    FROM foundation_source_refs r JOIN foundation_baseline_blocks b ON b.baseline_id=r.baseline_id,
      jsonb_array_elements(b.payload_json::jsonb->0->'tables') AS groups(value),
      jsonb_array_elements(groups.value->'chunks') AS chunk(value)
    WHERE r.id=NEW.capture_ref_id AND r.source='tushare' AND r.dataset='full_local_capture_manifest'
      AND groups.value->>'dataset'=NEW.native_dataset;
  IF members IS NULL OR members<>NEW.source_ids_json::jsonb
  THEN RAISE EXCEPTION 'table settlement requires the actual fixed capture members'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER table_settlement_scope_guard BEFORE INSERT OR UPDATE OR DELETE ON foundation_table_settlement_visits
FOR EACH ROW EXECUTE FUNCTION foundation_guard_table_settlement_scope()""",
"""CREATE FUNCTION foundation_guard_table_settlement_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM foundation_table_settlement_visits v
    JOIN foundation_official_releases r ON r.id=NEW.release_id AND r.scope_key=v.scope_key
    JOIN foundation_work g ON g.id=r.work_id
    JOIN foundation_candidate_manifests m ON m.id=g.candidate_manifest_id
    JOIN foundation_work a ON a.id=m.work_id
    JOIN foundation_source_refs s ON s.id=a.source_ref_id
    WHERE v.capture_ref_id=NEW.capture_ref_id AND v.scope_key=NEW.scope_key
      AND v.source_ids_json::jsonb @> jsonb_build_array(NEW.source_ref_id::text)
      AND s.id=NEW.source_ref_id AND s.source='tushare' AND s.dataset=v.native_dataset
      AND a.kind='A' AND a.status='succeeded' AND r.status='published'
      AND g.parameters_json NOT LIKE '%OLDER_SOURCE_RETAINED%'
      AND NOT EXISTS (SELECT 1 FROM foundation_candidates c WHERE c.work_id=a.id AND c.readiness<>'ready'))
  THEN RAISE EXCEPTION 'table settlement requires qualified published fixed-source evidence'; END IF;
  RETURN NEW;
END $$""",
"""CREATE TRIGGER table_settlement_receipt_insert BEFORE INSERT ON foundation_table_settlement_receipts
FOR EACH ROW EXECUTE FUNCTION foundation_guard_table_settlement_receipt()""",
)


def downgrade():
    for table in TABLES:
        if op.get_bind().scalar(text(f'SELECT EXISTS (SELECT 1 FROM {table})')):
            raise RuntimeError('Bounded processing evidence exists; use a compatible application rollback')
    op.execute("""CREATE OR REPLACE FUNCTION foundation_guard_ref_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT status FROM foundation_official_releases WHERE id=NEW.release_id FOR UPDATE) <> 'draft'
      THEN RAISE EXCEPTION 'sealed release cannot receive references'; END IF;
      PERFORM 1 FROM foundation_release_blocks WHERE id=NEW.block_id FOR UPDATE;
      RETURN NEW;
    END $$""")
    for table in reversed(TABLES):
        op.drop_table(table)
    for suffix in ('record_preparation','prepared_page','validation_root','block_page_proof',
                   'backfill_probe','backfill_receipt','backfill_completion',
                   'table_settlement_scope','table_settlement_receipt'):
        op.execute(f'DROP FUNCTION foundation_guard_{suffix}()')
