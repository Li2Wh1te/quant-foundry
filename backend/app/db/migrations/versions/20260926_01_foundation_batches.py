"""Expand foundation intake and multi-input relationships without rewriting history."""
from alembic import op

revision = "20260926_01"
down_revision = "20260925_01"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('\nCREATE TABLE foundation_intake_scopes (\n\tfingerprint VARCHAR(64) NOT NULL, \n\tdataset VARCHAR(80) NOT NULL, \n\tsubject VARCHAR(64) NOT NULL, \n\tvariant VARCHAR(80) NOT NULL, \n\tselector_json TEXT NOT NULL, \n\tdecoder_id UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_intake_scopes PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_intake_scopes_fingerprint UNIQUE (fingerprint), \n\tCONSTRAINT fk_foundation_intake_scopes_decoder_id_foundation_execu_5d54 FOREIGN KEY(decoder_id) REFERENCES foundation_execution_manifests (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_intake_controls (\n\tscope_id UUID NOT NULL, \n\tpaused BOOLEAN NOT NULL, \n\tCONSTRAINT pk_foundation_intake_controls PRIMARY KEY (scope_id), \n\tCONSTRAINT fk_foundation_intake_controls_scope_id_foundation_intake_scopes FOREIGN KEY(scope_id) REFERENCES foundation_intake_scopes (id) ON DELETE RESTRICT\n)\n\n')
    op.execute("\nCREATE TABLE foundation_intake_scans (\n\tscope_id UUID NOT NULL, \n\tevent_key VARCHAR(128) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcursor_id UUID, \n\tseen INTEGER NOT NULL, \n\tregistered INTEGER NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\terror_code VARCHAR(64), \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_intake_scans PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_intake_scans_scope_id UNIQUE (scope_id, event_key), \n\tCONSTRAINT ck_foundation_intake_scans_scan_status CHECK (status IN ('running','failed','completed')), \n\tCONSTRAINT ck_foundation_intake_scans_scan_counts CHECK (seen >= registered AND registered >= 0), \n\tCONSTRAINT fk_foundation_intake_scans_scope_id_foundation_intake_scopes FOREIGN KEY(scope_id) REFERENCES foundation_intake_scopes (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('CREATE INDEX ix_foundation_intake_scans_scope_id ON foundation_intake_scans (scope_id)')
    op.execute('\nCREATE TABLE foundation_intake_items (\n\tscope_id UUID NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tsource_ref_id UUID NOT NULL, \n\tfirst_scan_id UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_intake_items PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_intake_items_scope_id UNIQUE (scope_id, observation_id), \n\tCONSTRAINT fk_foundation_intake_items_scope_id_foundation_intake_scopes FOREIGN KEY(scope_id) REFERENCES foundation_intake_scopes (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_items_observation_id_tonghuashun_o_40d4 FOREIGN KEY(observation_id) REFERENCES tonghuashun_observations (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_items_source_ref_id_foundation_source_refs FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_items_first_scan_id_foundation_int_90db FOREIGN KEY(first_scan_id) REFERENCES foundation_intake_scans (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_intake_scan_entries (\n\tscan_id UUID NOT NULL, \n\titem_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_intake_scan_entries PRIMARY KEY (scan_id, item_id), \n\tCONSTRAINT fk_foundation_intake_scan_entries_scan_id_foundation_in_edd4 FOREIGN KEY(scan_id) REFERENCES foundation_intake_scans (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_scan_entries_item_id_foundation_in_f4a7 FOREIGN KEY(item_id) REFERENCES foundation_intake_items (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_intake_pointers (\n\tscope_id UUID NOT NULL, \n\tscan_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tobservation_id UUID, \n\tsource_status VARCHAR(24) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_intake_pointers PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_intake_pointers_scan_id UNIQUE (scan_id, revision), \n\tCONSTRAINT fk_foundation_intake_pointers_scope_id_foundation_intake_scopes FOREIGN KEY(scope_id) REFERENCES foundation_intake_scopes (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_pointers_scan_id_foundation_intake_scans FOREIGN KEY(scan_id) REFERENCES foundation_intake_scans (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_intake_pointers_observation_id_tonghuashu_55c1 FOREIGN KEY(observation_id) REFERENCES tonghuashun_observations (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_batches (\n\tevent_key VARCHAR(128) NOT NULL, \n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tmanifest_json TEXT NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_batches PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_batches_event_key UNIQUE (event_key)\n)\n\n')
    op.execute('CREATE INDEX ix_foundation_batches_scope_key ON foundation_batches (scope_key)')
    op.execute('\nCREATE TABLE foundation_batch_inputs (\n\tbatch_id UUID NOT NULL, \n\tsource_ref_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_batch_inputs PRIMARY KEY (batch_id, source_ref_id), \n\tCONSTRAINT fk_foundation_batch_inputs_batch_id_foundation_batches FOREIGN KEY(batch_id) REFERENCES foundation_batches (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_batch_inputs_source_ref_id_foundation_source_refs FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_batch_work (\n\tbatch_id UUID NOT NULL, \n\twork_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_batch_work PRIMARY KEY (batch_id, work_id), \n\tCONSTRAINT fk_foundation_batch_work_batch_id_foundation_batches FOREIGN KEY(batch_id) REFERENCES foundation_batches (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_batch_work_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_batch_controls (\n\tbatch_id UUID NOT NULL, \n\tpause_a BOOLEAN NOT NULL, \n\tpause_b BOOLEAN NOT NULL, \n\tallow_publish BOOLEAN NOT NULL, \n\tCONSTRAINT pk_foundation_batch_controls PRIMARY KEY (batch_id), \n\tCONSTRAINT fk_foundation_batch_controls_batch_id_foundation_batches FOREIGN KEY(batch_id) REFERENCES foundation_batches (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('\nCREATE TABLE foundation_candidate_input_sets (\n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\tentry_count INTEGER NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_candidate_input_sets PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_candidate_input_sets_input_set_count CHECK (entry_count > 0 AND entry_count <= 100), \n\tCONSTRAINT uq_foundation_candidate_input_sets_manifest_hash UNIQUE (manifest_hash)\n)\n\n')
    op.execute("\nCREATE TABLE foundation_candidate_input_entries (\n\tinput_set_id UUID NOT NULL, \n\tmanifest_id UUID NOT NULL, \n\tmanifest_hash VARCHAR(64) NOT NULL, \n\trole VARCHAR(16) NOT NULL, \n\treason VARCHAR(256) NOT NULL, \n\tCONSTRAINT pk_foundation_candidate_input_entries PRIMARY KEY (input_set_id, manifest_id), \n\tCONSTRAINT ck_foundation_candidate_input_entries_input_role CHECK (role IN ('eligible','historical')), \n\tCONSTRAINT fk_foundation_candidate_input_entries_input_set_id_foun_a50a FOREIGN KEY(input_set_id) REFERENCES foundation_candidate_input_sets (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_candidate_input_entries_manifest_id_found_c0b5 FOREIGN KEY(manifest_id) REFERENCES foundation_candidate_manifests (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("\nCREATE TABLE foundation_release_contributions (\n\trelease_id UUID NOT NULL, \n\twork_id UUID NOT NULL, \n\trole VARCHAR(16) NOT NULL, \n\tCONSTRAINT pk_foundation_release_contributions PRIMARY KEY (release_id, work_id, role), \n\tCONSTRAINT ck_foundation_release_contributions_contribution_role CHECK (role IN ('normalization','governance','inherited')), \n\tCONSTRAINT fk_foundation_release_contributions_release_id_foundati_3b87 FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_release_contributions_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("\nCREATE TABLE foundation_reconciliations (\n\tbatch_id UUID NOT NULL, \n\tevidence_hash VARCHAR(64) NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_reconciliations PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_reconciliations_batch_id UNIQUE (batch_id, evidence_hash), \n\tCONSTRAINT ck_foundation_reconciliations_reconciliation_status CHECK (status IN ('explained','unexplained','pending')), \n\tCONSTRAINT fk_foundation_reconciliations_batch_id_foundation_batches FOREIGN KEY(batch_id) REFERENCES foundation_batches (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('CREATE INDEX ix_foundation_reconciliations_batch_id ON foundation_reconciliations (batch_id)')
    op.execute("ALTER TABLE foundation_work ADD COLUMN candidate_input_set_id UUID REFERENCES foundation_candidate_input_sets(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE foundation_decisions ADD COLUMN candidate_input_set_id UUID REFERENCES foundation_candidate_input_sets(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE foundation_decisions ALTER COLUMN candidate_manifest_id DROP NOT NULL")
    op.execute("ALTER TABLE foundation_work DROP CONSTRAINT ck_foundation_work_work_input")
    op.execute("ALTER TABLE foundation_work ADD CONSTRAINT ck_foundation_work_work_input CHECK ((kind = 'A' AND source_ref_id IS NOT NULL AND candidate_manifest_id IS NULL AND candidate_input_set_id IS NULL AND policy_id IS NULL) OR (kind = 'B' AND source_ref_id IS NULL AND ((candidate_manifest_id IS NOT NULL AND candidate_input_set_id IS NULL) OR (candidate_manifest_id IS NULL AND candidate_input_set_id IS NOT NULL)) AND policy_id IS NOT NULL))")
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_scopes FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_items FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_scan_entries FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_intake_pointers FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_batches FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_batch_inputs FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_batch_work FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_candidate_input_sets FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_candidate_input_entries FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_release_contributions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_reconciliations FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("""CREATE FUNCTION foundation_guard_input_entry() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE capacity integer;
    BEGIN
      SELECT entry_count INTO capacity FROM foundation_candidate_input_sets WHERE id = NEW.input_set_id FOR UPDATE;
      IF (SELECT count(*) FROM foundation_candidate_input_entries WHERE input_set_id = NEW.input_set_id) >= capacity
        OR EXISTS (SELECT 1 FROM foundation_work WHERE candidate_input_set_id = NEW.input_set_id)
      THEN RAISE EXCEPTION 'sealed input set cannot receive entries'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_input_entry_insert BEFORE INSERT ON foundation_candidate_input_entries FOR EACH ROW EXECUTE FUNCTION foundation_guard_input_entry()")
    op.execute("\nCREATE TABLE foundation_batch_dispositions (\n\tbatch_id UUID NOT NULL, \n\tsource_ref_id UUID NOT NULL, \n\treason_code VARCHAR(48) NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_batch_dispositions PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_batch_dispositions_batch_id UNIQUE (batch_id, source_ref_id), \n\tCONSTRAINT ck_foundation_batch_dispositions_disposition_reason CHECK (reason_code IN ('SEMANTIC_DEPENDENCY_MISSING','OUT_OF_SCOPE')), \n\tCONSTRAINT fk_foundation_batch_dispositions_batch_id_foundation_batches FOREIGN KEY(batch_id) REFERENCES foundation_batches (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_batch_dispositions_source_ref_id_foundati_78fe FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_batch_dispositions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("ALTER TABLE foundation_work DROP CONSTRAINT ck_foundation_work_work_status")
    op.execute("ALTER TABLE foundation_work ADD CONSTRAINT ck_foundation_work_work_status CHECK (status IN ('queued','running','succeeded','failed','cancelled','dependency_missing','superseded','awaiting_publication'))")
    op.execute('\nCREATE TABLE foundation_work_source_pointers (\n\twork_id UUID NOT NULL, \n\tsource_ref_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tCONSTRAINT pk_foundation_work_source_pointers PRIMARY KEY (work_id, source_ref_id), \n\tCONSTRAINT fk_foundation_work_source_pointers_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_work_source_pointers_source_ref_id_founda_12bd FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_work_source_pointers FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('\nCREATE TABLE foundation_maintenance_plans (\n\tfingerprint VARCHAR(64) NOT NULL, \n\tdependency_id UUID NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_maintenance_plans PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_maintenance_plans_fingerprint UNIQUE (fingerprint), \n\tCONSTRAINT fk_foundation_maintenance_plans_dependency_id_foundatio_c165 FOREIGN KEY(dependency_id) REFERENCES foundation_dependency_manifests (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_maintenance_plans FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('\nCREATE TABLE foundation_maintenance_targets (\n\tplan_id UUID NOT NULL, \n\trelease_id UUID NOT NULL, \n\treason_json TEXT NOT NULL, \n\tCONSTRAINT pk_foundation_maintenance_targets PRIMARY KEY (plan_id, release_id), \n\tCONSTRAINT fk_foundation_maintenance_targets_plan_id_foundation_ma_49af FOREIGN KEY(plan_id) REFERENCES foundation_maintenance_plans (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_maintenance_targets_release_id_foundation_b798 FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_maintenance_targets FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("ALTER TABLE foundation_intake_scans ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'full'")
    op.execute("ALTER TABLE foundation_intake_scans ADD CONSTRAINT ck_foundation_intake_scans_scan_mode CHECK (mode IN ('full','recent'))")
    # Reconstruct only relationships already proven by immutable foreign keys;
    # no synthetic historical batch or process event is manufactured.
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT id,work_id,'governance' FROM foundation_official_releases ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT r.id,m.work_id,'normalization' FROM foundation_official_releases r JOIN foundation_work w ON w.id=r.work_id JOIN foundation_candidate_manifests m ON m.id=w.candidate_manifest_id ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT r.release_id,d.work_id,'inherited' FROM foundation_release_block_refs r JOIN foundation_block_members m ON m.block_id=r.block_id JOIN foundation_decisions d ON d.id=m.decision_id JOIN foundation_official_releases o ON o.id=r.release_id WHERE d.work_id<>o.work_id ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT r.release_id,d.work_id,'inherited' FROM foundation_release_block_refs r JOIN foundation_report_block_members m ON m.block_id=r.block_id JOIN foundation_decisions d ON d.id=m.decision_id JOIN foundation_official_releases o ON o.id=r.release_id WHERE d.work_id<>o.work_id ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT r.release_id,c.work_id,'inherited' FROM foundation_release_block_refs r JOIN foundation_block_members m ON m.block_id=r.block_id JOIN foundation_bar_official_revisions o ON o.id=m.official_id JOIN foundation_candidates c ON c.id=o.candidate_id WHERE NOT EXISTS (SELECT 1 FROM foundation_release_contributions direct WHERE direct.release_id=r.release_id AND direct.work_id=c.work_id AND direct.role='normalization') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO foundation_release_contributions (release_id,work_id,role) SELECT r.release_id,c.work_id,'inherited' FROM foundation_release_block_refs r JOIN foundation_report_block_members m ON m.block_id=r.block_id JOIN foundation_report_official_revisions o ON o.id=m.official_id JOIN foundation_candidates c ON c.id=o.candidate_id WHERE NOT EXISTS (SELECT 1 FROM foundation_release_contributions direct WHERE direct.release_id=r.release_id AND direct.work_id=c.work_id AND direct.role='normalization') ON CONFLICT DO NOTHING")
    op.execute("""CREATE FUNCTION foundation_guard_scan() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'scan evidence is retained'; END IF;
      IF (to_jsonb(NEW) - ARRAY['status','cursor_id','seen','registered','completed_at','error_code']) IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['status','cursor_id','seen','registered','completed_at','error_code'])
         OR NEW.seen < OLD.seen OR NEW.registered < OLD.registered
         OR (OLD.cursor_id IS NOT NULL AND (NEW.cursor_id IS NULL OR NEW.cursor_id < OLD.cursor_id))
         OR (OLD.status = 'completed' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD))
      THEN RAISE EXCEPTION 'scan identity, completed input and committed checkpoint are immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_scan_guard BEFORE UPDATE OR DELETE ON foundation_intake_scans FOR EACH ROW EXECUTE FUNCTION foundation_guard_scan()")


def downgrade():
    # Only an unused expansion can be removed. Once any new evidence exists,
    # keep the schema and roll back the application through compatible readers.
    tables = [
        'foundation_maintenance_targets', 'foundation_maintenance_plans',
        'foundation_work_source_pointers', 'foundation_batch_dispositions',
        'foundation_reconciliations', 'foundation_candidate_input_entries',
        'foundation_candidate_input_sets', 'foundation_batch_work',
        'foundation_batch_controls', 'foundation_batch_inputs', 'foundation_batches',
        'foundation_intake_pointers', 'foundation_intake_scan_entries',
        'foundation_intake_items', 'foundation_intake_scans',
        'foundation_intake_controls', 'foundation_intake_scopes']
    from sqlalchemy import text
    connection = op.get_bind()
    for table in tables:
        if connection.scalar(text('SELECT EXISTS (SELECT 1 FROM ' + table + ' LIMIT 1)')):
            raise RuntimeError('Foundation schema contains persisted evidence; use a compatible application rollback')
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('ALTER TABLE foundation_work DROP CONSTRAINT ck_foundation_work_work_input')
    op.execute('ALTER TABLE foundation_work DROP CONSTRAINT ck_foundation_work_work_status')
    op.execute('ALTER TABLE foundation_work DROP COLUMN candidate_input_set_id')
    op.execute('ALTER TABLE foundation_decisions DROP COLUMN candidate_input_set_id')
    op.execute('ALTER TABLE foundation_decisions ALTER COLUMN candidate_manifest_id SET NOT NULL')
    op.execute("ALTER TABLE foundation_work ADD CONSTRAINT ck_foundation_work_work_input CHECK ((kind = 'A' AND source_ref_id IS NOT NULL AND candidate_manifest_id IS NULL AND policy_id IS NULL) OR (kind = 'B' AND source_ref_id IS NULL AND candidate_manifest_id IS NOT NULL AND policy_id IS NOT NULL))")
    op.execute("ALTER TABLE foundation_work ADD CONSTRAINT ck_foundation_work_work_status CHECK (status IN ('queued','running','succeeded','failed','cancelled','dependency_missing','superseded'))")
    op.execute('DROP TABLE foundation_release_contributions')
    for table in tables:
        op.execute('DROP TABLE ' + table)
    op.execute('DROP FUNCTION foundation_guard_input_entry()')
    op.execute('DROP FUNCTION foundation_guard_scan()')
