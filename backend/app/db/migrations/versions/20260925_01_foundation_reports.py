"""Add typed whole-report objects without changing existing daily releases."""
from alembic import op

revision = '20260925_01'
down_revision = '20260924_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("\nCREATE TABLE foundation_report_candidates (\n\tcandidate_id UUID NOT NULL, \n\tfund_share_id UUID NOT NULL, \n\tperiod_start DATE NOT NULL, \n\tperiod_end DATE NOT NULL, \n\treport_type VARCHAR(16) NOT NULL, \n\tscope_kind VARCHAR(24) NOT NULL, \n\tseries VARCHAR(128) NOT NULL, \n\tmember_count INTEGER NOT NULL, \n\ttransport_complete BOOLEAN NOT NULL, \n\tportfolio_complete BOOLEAN, \n\tpublic_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_foundation_report_candidates PRIMARY KEY (candidate_id), \n\tCONSTRAINT ck_foundation_report_candidates_report_range CHECK (period_start <= period_end AND member_count >= 0), \n\tCONSTRAINT ck_foundation_report_candidates_report_type CHECK (report_type IN ('quarter','annual','semiannual')), \n\tCONSTRAINT ck_foundation_report_candidates_report_scope CHECK (scope_kind IN ('provider_reported','top_n','full_portfolio')), \n\tCONSTRAINT fk_foundation_report_candidates_candidate_id_foundation_a1fd FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_candidates_fund_share_id_instruments FOREIGN KEY(fund_share_id) REFERENCES instruments (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_candidates FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("\nCREATE TABLE foundation_report_candidate_members (\n\tcandidate_id UUID NOT NULL, \n\tmember_ordinal INTEGER NOT NULL, \n\tmember_instrument_id UUID NOT NULL, \n\tbinding_id UUID NOT NULL, \n\tsource_member_code VARCHAR(64) NOT NULL, \n\thold_ratio NUMERIC(20, 10) NOT NULL, \n\tmarket_value NUMERIC(32, 8), \n\tperiod_change_ratio NUMERIC(20, 10), \n\trank INTEGER, \n\tfield_quality_json TEXT NOT NULL, \n\tCONSTRAINT pk_foundation_report_candidate_members PRIMARY KEY (candidate_id, member_ordinal), \n\tCONSTRAINT ck_foundation_report_candidate_members_holding_values CHECK (member_ordinal >= 0 AND hold_ratio >= 0 AND hold_ratio <= 1 AND (rank IS NULL OR rank > 0) AND (market_value IS NULL OR (market_value >= 0 AND market_value < 'Infinity')) AND (period_change_ratio IS NULL OR (period_change_ratio > '-Infinity' AND period_change_ratio < 'Infinity'))), \n\tCONSTRAINT fk_foundation_report_candidate_members_candidate_id_fou_4170 FOREIGN KEY(candidate_id) REFERENCES foundation_report_candidates (candidate_id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_candidate_members_member_instrumen_beb7 FOREIGN KEY(member_instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_candidate_members_binding_id_found_e228 FOREIGN KEY(binding_id) REFERENCES foundation_source_bindings (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_candidate_members FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("\nCREATE TABLE foundation_report_official_revisions (\n\tcandidate_id UUID NOT NULL, \n\tdecision_id UUID NOT NULL, \n\tvalues_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfund_share_id UUID NOT NULL, \n\tperiod_start DATE NOT NULL, \n\tperiod_end DATE NOT NULL, \n\treport_type VARCHAR(16) NOT NULL, \n\tscope_kind VARCHAR(24) NOT NULL, \n\tseries VARCHAR(128) NOT NULL, \n\tmember_count INTEGER NOT NULL, \n\ttransport_complete BOOLEAN NOT NULL, \n\tportfolio_complete BOOLEAN, \n\tpublic_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_foundation_report_official_revisions PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_report_official_revisions_report_range CHECK (period_start <= period_end AND member_count >= 0), \n\tCONSTRAINT ck_foundation_report_official_revisions_report_type CHECK (report_type IN ('quarter','annual','semiannual')), \n\tCONSTRAINT ck_foundation_report_official_revisions_report_scope CHECK (scope_kind IN ('provider_reported','top_n','full_portfolio')), \n\tCONSTRAINT fk_foundation_report_official_revisions_candidate_id_fo_26b4 FOREIGN KEY(candidate_id) REFERENCES foundation_report_candidates (candidate_id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_foundation_report_official_revisions_decision_id UNIQUE (decision_id), \n\tCONSTRAINT fk_foundation_report_official_revisions_decision_id_fou_e75c FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_official_revisions_fund_share_id_i_5509 FOREIGN KEY(fund_share_id) REFERENCES instruments (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_official_revisions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("\nCREATE TABLE foundation_report_official_members (\n\tofficial_id UUID NOT NULL, \n\tmember_ordinal INTEGER NOT NULL, \n\tmember_instrument_id UUID NOT NULL, \n\tbinding_id UUID NOT NULL, \n\tsource_member_code VARCHAR(64) NOT NULL, \n\thold_ratio NUMERIC(20, 10) NOT NULL, \n\tmarket_value NUMERIC(32, 8), \n\tperiod_change_ratio NUMERIC(20, 10), \n\trank INTEGER, \n\tfield_quality_json TEXT NOT NULL, \n\tCONSTRAINT pk_foundation_report_official_members PRIMARY KEY (official_id, member_ordinal), \n\tCONSTRAINT ck_foundation_report_official_members_holding_values CHECK (member_ordinal >= 0 AND hold_ratio >= 0 AND hold_ratio <= 1 AND (rank IS NULL OR rank > 0) AND (market_value IS NULL OR (market_value >= 0 AND market_value < 'Infinity')) AND (period_change_ratio IS NULL OR (period_change_ratio > '-Infinity' AND period_change_ratio < 'Infinity'))), \n\tCONSTRAINT fk_foundation_report_official_members_official_id_found_4a8a FOREIGN KEY(official_id) REFERENCES foundation_report_official_revisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_official_members_member_instrument_aaa2 FOREIGN KEY(member_instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_official_members_binding_id_founda_5205 FOREIGN KEY(binding_id) REFERENCES foundation_source_bindings (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_official_members FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("\nCREATE TABLE foundation_report_block_members (\n\tblock_id UUID NOT NULL, \n\ttarget_key VARCHAR(64) NOT NULL, \n\tfund_share_id UUID NOT NULL, \n\tperiod_start DATE NOT NULL, \n\tperiod_end DATE NOT NULL, \n\treport_type VARCHAR(16) NOT NULL, \n\tscope_kind VARCHAR(24) NOT NULL, \n\tseries VARCHAR(128) NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tofficial_id UUID, \n\tdecision_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_report_block_members PRIMARY KEY (block_id, target_key), \n\tCONSTRAINT ck_foundation_report_block_members_report_member_state CHECK ((state = 'value' AND official_id IS NOT NULL) OR (state IN ('gap','blocked','withdrawn') AND official_id IS NULL)), \n\tCONSTRAINT fk_foundation_report_block_members_block_id_foundation__02cd FOREIGN KEY(block_id) REFERENCES foundation_release_blocks (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_block_members_fund_share_id_instruments FOREIGN KEY(fund_share_id) REFERENCES instruments (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_block_members_official_id_foundati_dc75 FOREIGN KEY(official_id) REFERENCES foundation_report_official_revisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_block_members_decision_id_foundati_6e31 FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_block_members FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute('\nCREATE TABLE foundation_report_issue_targets (\n\tissue_revision_id UUID NOT NULL, \n\ttarget_key VARCHAR(64) NOT NULL, \n\tofficial_id UUID, \n\tCONSTRAINT pk_foundation_report_issue_targets PRIMARY KEY (issue_revision_id), \n\tCONSTRAINT fk_foundation_report_issue_targets_issue_revision_id_fo_b6ec FOREIGN KEY(issue_revision_id) REFERENCES foundation_issue_revisions (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_report_issue_targets_official_id_foundati_b4cd FOREIGN KEY(official_id) REFERENCES foundation_report_official_revisions (id) ON DELETE RESTRICT\n)\n\n')
    op.execute("CREATE TRIGGER report_immutable BEFORE UPDATE OR DELETE ON foundation_report_issue_targets FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER report_block_insert BEFORE INSERT ON foundation_report_block_members FOR EACH ROW EXECUTE FUNCTION foundation_guard_member_insert()")
    op.execute("""CREATE FUNCTION foundation_guard_report_head() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM foundation_candidates WHERE id = NEW.candidate_id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM foundation_candidate_entries WHERE candidate_id = NEW.candidate_id)
      THEN RAISE EXCEPTION 'sealed candidate cannot receive a report head'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER report_head_insert BEFORE INSERT ON foundation_report_candidates FOR EACH ROW EXECUTE FUNCTION foundation_guard_report_head()")
    op.execute("""CREATE FUNCTION foundation_guard_report_members() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'foundation_report_candidate_members' THEN
        PERFORM 1 FROM foundation_report_candidates WHERE candidate_id = NEW.candidate_id FOR UPDATE;
        IF EXISTS (SELECT 1 FROM foundation_candidate_entries WHERE candidate_id = NEW.candidate_id)
        THEN RAISE EXCEPTION 'sealed candidate cannot receive report members'; END IF;
      ELSE
        PERFORM 1 FROM foundation_report_official_revisions WHERE id = NEW.official_id FOR UPDATE;
        IF EXISTS (SELECT 1 FROM foundation_report_block_members WHERE official_id = NEW.official_id)
        THEN RAISE EXCEPTION 'sealed official report cannot receive members'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER report_members_insert BEFORE INSERT ON foundation_report_candidate_members FOR EACH ROW EXECUTE FUNCTION foundation_guard_report_members()")
    op.execute("CREATE TRIGGER report_members_insert BEFORE INSERT ON foundation_report_official_members FOR EACH ROW EXECUTE FUNCTION foundation_guard_report_members()")
    # Retained reports need a typed reference; never point the bar-only legacy
    # foreign key at a report or loosen retention into an unvalidated JSON ID.
    op.execute("ALTER TABLE foundation_decisions ADD COLUMN parent_report_id UUID REFERENCES foundation_report_official_revisions(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE foundation_decisions DROP CONSTRAINT ck_foundation_decisions_decision_action")
    op.execute("ALTER TABLE foundation_decisions ADD CONSTRAINT ck_foundation_decisions_decision_action CHECK ((action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL AND parent_report_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND ((parent_official_id IS NOT NULL AND parent_report_id IS NULL) OR (parent_official_id IS NULL AND parent_report_id IS NOT NULL))) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL AND parent_report_id IS NULL))")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('ALTER TABLE foundation_decisions DROP CONSTRAINT ck_foundation_decisions_decision_action')
    op.execute('ALTER TABLE foundation_decisions DROP COLUMN IF EXISTS parent_report_id')
    op.execute("ALTER TABLE foundation_decisions ADD CONSTRAINT ck_foundation_decisions_decision_action CHECK ((action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND parent_official_id IS NOT NULL) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL))")
    op.execute('DROP TABLE foundation_report_issue_targets')
    op.execute('DROP TABLE foundation_report_block_members')
    op.execute('DROP TABLE foundation_report_official_members')
    op.execute('DROP TABLE foundation_report_official_revisions')
    op.execute('DROP TABLE foundation_report_candidate_members')
    op.execute('DROP TABLE foundation_report_candidates')
    op.execute('DROP FUNCTION foundation_guard_report_members()')
    op.execute('DROP FUNCTION IF EXISTS foundation_guard_report_head()')
