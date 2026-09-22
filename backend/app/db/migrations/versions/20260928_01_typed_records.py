"""Extend shared candidates and releases with typed source-local domain records."""
from alembic import op
from sqlalchemy import text

revision = "20260928_01"
down_revision = "20260927_01"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('\nCREATE TABLE foundation_record_subjects (\n\tsource VARCHAR(32) NOT NULL, \n\tkind VARCHAR(64) NOT NULL, \n\tsource_key VARCHAR(128) NOT NULL, \n\tevidence_source_ref_id UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_record_subjects PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_record_subjects_source UNIQUE (source, kind, source_key), \n\tCONSTRAINT fk_foundation_record_subjects_evidence_source_ref_id_fo_875c FOREIGN KEY(evidence_source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_record_subjects FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('\nCREATE TABLE foundation_record_candidates (\n\tcandidate_id UUID NOT NULL, \n\tsubject_id UUID NOT NULL, \n\tbusiness_key VARCHAR(64) NOT NULL, \n\tbusiness_date DATE, \n\tschema_key VARCHAR(128) NOT NULL, \n\tbody_json TEXT NOT NULL, \n\tfield_quality_json TEXT NOT NULL, \n\tCONSTRAINT pk_foundation_record_candidates PRIMARY KEY (candidate_id), \n\tCONSTRAINT fk_foundation_record_candidates_candidate_id_foundation_b2e1 FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_record_candidates_subject_id_foundation_r_dc60 FOREIGN KEY(subject_id) REFERENCES foundation_record_subjects (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_record_candidates FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute('\nCREATE TABLE foundation_record_official_revisions (\n\tcandidate_id UUID NOT NULL, \n\tdecision_id UUID NOT NULL, \n\tvalues_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsubject_id UUID NOT NULL, \n\tbusiness_key VARCHAR(64) NOT NULL, \n\tbusiness_date DATE, \n\tschema_key VARCHAR(128) NOT NULL, \n\tbody_json TEXT NOT NULL, \n\tfield_quality_json TEXT NOT NULL, \n\tCONSTRAINT pk_foundation_record_official_revisions PRIMARY KEY (id), \n\tCONSTRAINT fk_foundation_record_official_revisions_candidate_id_fo_cb66 FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_foundation_record_official_revisions_decision_id UNIQUE (decision_id), \n\tCONSTRAINT fk_foundation_record_official_revisions_decision_id_fou_cfbe FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_record_official_revisions_subject_id_foun_7b94 FOREIGN KEY(subject_id) REFERENCES foundation_record_subjects (id) ON DELETE RESTRICT\n)\n\n')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_record_official_revisions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("\nCREATE TABLE foundation_record_block_members (\n\tblock_id UUID NOT NULL, \n\ttarget_key VARCHAR(64) NOT NULL, \n\tsubject_id UUID NOT NULL, \n\tbusiness_date DATE, \n\tstate VARCHAR(16) NOT NULL, \n\tofficial_id UUID, \n\tdecision_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_record_block_members PRIMARY KEY (block_id, target_key), \n\tCONSTRAINT ck_foundation_record_block_members_record_member_state CHECK ((state = 'value' AND official_id IS NOT NULL) OR (state IN ('gap','blocked','withdrawn') AND official_id IS NULL)), \n\tCONSTRAINT fk_foundation_record_block_members_block_id_foundation__196b FOREIGN KEY(block_id) REFERENCES foundation_release_blocks (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_record_block_members_subject_id_foundatio_3fbb FOREIGN KEY(subject_id) REFERENCES foundation_record_subjects (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_record_block_members_official_id_foundati_25d9 FOREIGN KEY(official_id) REFERENCES foundation_record_official_revisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_record_block_members_decision_id_foundati_584d FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('CREATE INDEX ix_foundation_record_block_members_subject_id ON foundation_record_block_members (subject_id)')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_record_block_members FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("\nCREATE TABLE foundation_record_issues (\n\tissue_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\ttarget_key VARCHAR(64) NOT NULL, \n\tofficial_id UUID, \n\tstate VARCHAR(16) NOT NULL, \n\tfields_json TEXT NOT NULL, \n\treason TEXT NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_record_issues PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_record_issues_issue_id UNIQUE (issue_id, revision), \n\tCONSTRAINT ck_foundation_record_issues_record_issue_state CHECK (state IN ('suspected','confirmed','resolved','dismissed')), \n\tCONSTRAINT fk_foundation_record_issues_official_id_foundation_reco_d69c FOREIGN KEY(official_id) REFERENCES foundation_record_official_revisions (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('CREATE INDEX ix_foundation_record_issues_scope_key ON foundation_record_issues (scope_key)')
    op.execute('CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_record_issues FOR EACH ROW EXECUTE FUNCTION foundation_immutable()')
    op.execute("ALTER TABLE foundation_candidates ADD COLUMN record_subject_id UUID REFERENCES foundation_record_subjects(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE foundation_candidates DROP CONSTRAINT ck_foundation_candidates_candidate_binding")
    op.execute("ALTER TABLE foundation_candidates ADD CONSTRAINT ck_foundation_candidates_candidate_binding CHECK (readiness <> 'ready' OR binding_id IS NOT NULL OR record_subject_id IS NOT NULL)")
    op.execute("ALTER TABLE foundation_candidates ADD CONSTRAINT ck_foundation_candidates_candidate_identity_kind CHECK (binding_id IS NULL OR record_subject_id IS NULL)")
    op.execute("ALTER TABLE foundation_decisions ADD COLUMN parent_record_id UUID REFERENCES foundation_record_official_revisions(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE foundation_decisions DROP CONSTRAINT ck_foundation_decisions_decision_action")
    op.execute("ALTER TABLE foundation_decisions ADD CONSTRAINT ck_foundation_decisions_decision_action CHECK ((action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL AND parent_report_id IS NULL AND parent_record_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND ((CASE WHEN parent_official_id IS NULL THEN 0 ELSE 1 END) + (CASE WHEN parent_report_id IS NULL THEN 0 ELSE 1 END) + (CASE WHEN parent_record_id IS NULL THEN 0 ELSE 1 END)) = 1) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL AND parent_report_id IS NULL AND parent_record_id IS NULL))")
    op.execute("CREATE TRIGGER record_block_insert BEFORE INSERT ON foundation_record_block_members FOR EACH ROW EXECUTE FUNCTION foundation_guard_member_insert()")
    op.execute("CREATE TRIGGER record_candidate_insert BEFORE INSERT ON foundation_record_candidates FOR EACH ROW EXECUTE FUNCTION foundation_guard_report_head()")

def downgrade():
    # Never drop persisted domain identities, candidates, restrictions or values.
    connection = op.get_bind()
    for table in ['foundation_record_subjects', 'foundation_record_candidates', 'foundation_record_official_revisions', 'foundation_record_block_members', 'foundation_record_issues']:
        if connection.scalar(text("SELECT EXISTS (SELECT 1 FROM " + table + " LIMIT 1)")):
            raise RuntimeError("Typed domain evidence exists; use a compatible application rollback")
    op.execute("ALTER TABLE foundation_decisions DROP CONSTRAINT ck_foundation_decisions_decision_action")
    op.execute("ALTER TABLE foundation_decisions DROP COLUMN parent_record_id")
    op.execute("ALTER TABLE foundation_candidates DROP CONSTRAINT ck_foundation_candidates_candidate_identity_kind")
    op.execute("ALTER TABLE foundation_candidates DROP CONSTRAINT ck_foundation_candidates_candidate_binding")
    op.execute("ALTER TABLE foundation_candidates DROP COLUMN record_subject_id")
    op.execute("ALTER TABLE foundation_candidates ADD CONSTRAINT ck_foundation_candidates_candidate_binding CHECK (readiness <> 'ready' OR binding_id IS NOT NULL)")
    op.execute("ALTER TABLE foundation_decisions ADD CONSTRAINT ck_foundation_decisions_decision_action CHECK ((action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL AND parent_report_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND ((parent_official_id IS NOT NULL AND parent_report_id IS NULL) OR (parent_official_id IS NULL AND parent_report_id IS NOT NULL))) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL AND parent_report_id IS NULL))")
    op.drop_table('foundation_record_issues')
    op.drop_table('foundation_record_block_members')
    op.drop_table('foundation_record_official_revisions')
    op.drop_table('foundation_record_candidates')
    op.drop_table('foundation_record_subjects')
