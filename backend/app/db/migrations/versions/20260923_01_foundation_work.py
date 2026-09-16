"""Add fenced work and typed, atomic foundation publication."""
from alembic import op

revision = "20260923_01"
down_revision = "20260922_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("\nCREATE TABLE foundation_work (\n\tfingerprint VARCHAR(64) NOT NULL, \n\tkind VARCHAR(1) NOT NULL, \n\tcontract_id UUID NOT NULL, \n\texecution_id UUID NOT NULL, \n\tdependency_id UUID NOT NULL, \n\tparameters_json TEXT NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\tsource_ref_id UUID, \n\tcandidate_manifest_id UUID, \n\tparent_release_id UUID, \n\tpolicy_id UUID, \n\texpected_head_revision INTEGER NOT NULL, \n\texpected_issue_epoch INTEGER NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tcursor INTEGER NOT NULL, \n\ttotal INTEGER, \n\tlease_epoch INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tcancelled BOOLEAN NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_work PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_work_work_kind CHECK (kind IN ('A','B')), \n\tCONSTRAINT ck_foundation_work_work_status CHECK (status IN ('queued','running','succeeded','failed','cancelled','dependency_missing','superseded')), \n\tCONSTRAINT ck_foundation_work_work_counts CHECK (cursor >= 0 AND lease_epoch >= 0 AND (total IS NULL OR total >= cursor)), \n\tCONSTRAINT ck_foundation_work_work_input CHECK ((kind = 'A' AND source_ref_id IS NOT NULL AND candidate_manifest_id IS NULL AND policy_id IS NULL) OR (kind = 'B' AND source_ref_id IS NULL AND candidate_manifest_id IS NOT NULL AND policy_id IS NOT NULL)), \n\tCONSTRAINT uq_foundation_work_fingerprint UNIQUE (fingerprint)\n)\n\n")
    op.execute('\nCREATE TABLE foundation_work_attempts (\n\twork_id UUID NOT NULL, \n\tepoch INTEGER NOT NULL, \n\toutcome VARCHAR(32) NOT NULL, \n\terror_code VARCHAR(64), \n\tdetails_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_work_attempts PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_work_attempts_work_id UNIQUE (work_id, epoch, outcome)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_work_units (\n\twork_id UUID NOT NULL, \n\tunit_key VARCHAR(128) NOT NULL, \n\tresult_hash VARCHAR(64) NOT NULL, \n\trow_count INTEGER NOT NULL, \n\tCONSTRAINT pk_foundation_work_units PRIMARY KEY (work_id, unit_key), \n\tCONSTRAINT ck_foundation_work_units_unit_count CHECK (row_count >= 0)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_work_events (\n\twork_id UUID NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tstep VARCHAR(32) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tmessage TEXT NOT NULL, \n\tdetails_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_work_events PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_work_events_work_id UNIQUE (work_id, sequence)\n)\n\n')
    op.execute("\nCREATE TABLE foundation_assessments (\n\tinput_hash VARCHAR(64) NOT NULL, \n\trule_hash VARCHAR(64) NOT NULL, \n\tscope_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tresults_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_assessments PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_assessments_input_hash UNIQUE (input_hash, rule_hash, scope_hash), \n\tCONSTRAINT ck_foundation_assessments_assessment_status CHECK (status IN ('pass','fail','unknown','not_applicable'))\n)\n\n")
    op.execute("\nCREATE TABLE foundation_candidates (\n\twork_id UUID NOT NULL, \n\tsource_ref_id UUID NOT NULL, \n\tbinding_id UUID, \n\tdependency_id UUID NOT NULL, \n\tunit_key VARCHAR(128) NOT NULL, \n\toccurrence INTEGER NOT NULL, \n\tvalues_hash VARCHAR(64) NOT NULL, \n\tassessment_id UUID NOT NULL, \n\treadiness VARCHAR(24) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_candidates PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_candidates_work_id UNIQUE (work_id, unit_key, occurrence), \n\tCONSTRAINT ck_foundation_candidates_candidate_readiness CHECK (readiness IN ('pending','ready','quarantined')), \n\tCONSTRAINT ck_foundation_candidates_candidate_binding CHECK (readiness <> 'ready' OR binding_id IS NOT NULL)\n)\n\n")
    op.execute("\nCREATE TABLE foundation_bar_candidates (\n\tcandidate_id UUID NOT NULL, \n\tinstrument_id UUID NOT NULL, \n\ttrade_date DATE NOT NULL, \n\tseries VARCHAR(128) NOT NULL, \n\topen NUMERIC(28, 10) NOT NULL, \n\thigh NUMERIC(28, 10) NOT NULL, \n\tlow NUMERIC(28, 10) NOT NULL, \n\tclose NUMERIC(28, 10) NOT NULL, \n\tvolume NUMERIC(32, 8), \n\tturnover NUMERIC(32, 8), \n\tCONSTRAINT pk_foundation_bar_candidates PRIMARY KEY (candidate_id), \n\tCONSTRAINT ck_foundation_bar_candidates_candidate_bar_values CHECK (open > 0 AND low > 0 AND high >= open AND high >= close AND low <= open AND low <= close AND open < 'Infinity' AND high < 'Infinity' AND low < 'Infinity' AND close < 'Infinity' AND (volume IS NULL OR (volume >= 0 AND volume < 'Infinity')) AND (turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity')))\n)\n\n")
    op.execute('\nCREATE TABLE foundation_candidate_manifests (\n\twork_id UUID NOT NULL, \n\tmanifest_hash VARCHAR(64) NOT NULL, \n\trow_count INTEGER NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_candidate_manifests PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_candidate_manifests_work_id UNIQUE (work_id)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_candidate_entries (\n\tmanifest_id UUID NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tcandidate_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_candidate_entries PRIMARY KEY (manifest_id, ordinal), \n\tCONSTRAINT uq_foundation_candidate_entries_manifest_id UNIQUE (manifest_id, candidate_id)\n)\n\n')
    op.execute("\nCREATE TABLE foundation_decisions (\n\tassessment_id UUID NOT NULL, \n\twork_id UUID NOT NULL, \n\ttarget_key VARCHAR(128) NOT NULL, \n\tcandidate_manifest_id UUID NOT NULL, \n\tselected_candidate_id UUID, \n\tparent_official_id UUID, \n\taction VARCHAR(16) NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_decisions PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_decisions_work_id UNIQUE (work_id, target_key), \n\tCONSTRAINT ck_foundation_decisions_decision_action CHECK ((action = 'select' AND selected_candidate_id IS NOT NULL AND parent_official_id IS NULL) OR (action = 'retain' AND selected_candidate_id IS NULL AND parent_official_id IS NOT NULL) OR (action IN ('gap','block','withdraw') AND selected_candidate_id IS NULL AND parent_official_id IS NULL))\n)\n\n")
    op.execute("\nCREATE TABLE foundation_bar_official_revisions (\n\tdecision_id UUID NOT NULL, \n\tcandidate_id UUID NOT NULL, \n\tvalues_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tinstrument_id UUID NOT NULL, \n\ttrade_date DATE NOT NULL, \n\tseries VARCHAR(128) NOT NULL, \n\topen NUMERIC(28, 10) NOT NULL, \n\thigh NUMERIC(28, 10) NOT NULL, \n\tlow NUMERIC(28, 10) NOT NULL, \n\tclose NUMERIC(28, 10) NOT NULL, \n\tvolume NUMERIC(32, 8), \n\tturnover NUMERIC(32, 8), \n\tCONSTRAINT pk_foundation_bar_official_revisions PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_bar_official_revisions_official_bar_values CHECK (open > 0 AND low > 0 AND high >= open AND high >= close AND low <= open AND low <= close AND open < 'Infinity' AND high < 'Infinity' AND low < 'Infinity' AND close < 'Infinity' AND (volume IS NULL OR (volume >= 0 AND volume < 'Infinity')) AND (turnover IS NULL OR (turnover >= 0 AND turnover < 'Infinity'))), \n\tCONSTRAINT uq_foundation_bar_official_revisions_decision_id UNIQUE (decision_id)\n)\n\n")
    op.execute("\nCREATE TABLE foundation_official_releases (\n\twork_id UUID NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\tparent_id UUID, \n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_official_releases PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_official_releases_release_status CHECK (status IN ('draft','sealed','published','abandoned')), \n\tCONSTRAINT uq_foundation_official_releases_work_id UNIQUE (work_id)\n)\n\n")
    op.execute('\nCREATE TABLE foundation_release_blocks (\n\tpartition_key VARCHAR(128) NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\trow_count INTEGER NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_release_blocks PRIMARY KEY (id)\n)\n\n')
    op.execute("\nCREATE TABLE foundation_block_members (\n\tblock_id UUID NOT NULL, \n\tinstrument_id UUID NOT NULL, \n\ttrade_date DATE NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tofficial_id UUID, \n\tdecision_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_block_members PRIMARY KEY (block_id, instrument_id, trade_date), \n\tCONSTRAINT ck_foundation_block_members_member_state CHECK ((state = 'value' AND official_id IS NOT NULL) OR (state IN ('gap','blocked','withdrawn') AND official_id IS NULL))\n)\n\n")
    op.execute('\nCREATE TABLE foundation_release_block_refs (\n\trelease_id UUID NOT NULL, \n\tpartition_key VARCHAR(128) NOT NULL, \n\tblock_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_release_block_refs PRIMARY KEY (release_id, partition_key)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_heads (\n\tscope_key VARCHAR(64) NOT NULL, \n\trelease_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tCONSTRAINT pk_foundation_heads PRIMARY KEY (scope_key), \n\tCONSTRAINT ck_foundation_heads_head_revision CHECK (revision >= 1)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_issue_scopes (\n\tscope_key VARCHAR(64) NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tCONSTRAINT pk_foundation_issue_scopes PRIMARY KEY (scope_key)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_issue_revisions (\n\tissue_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tscope_key VARCHAR(64) NOT NULL, \n\tinstrument_id UUID NOT NULL, \n\tstart DATE NOT NULL, \n\t"end" DATE NOT NULL, \n\tofficial_id UUID, \n\tstate VARCHAR(16) NOT NULL, \n\tfields_json TEXT NOT NULL, \n\treason TEXT NOT NULL, \n\tseverity VARCHAR(16) NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_issue_revisions PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_issue_revisions_issue_id UNIQUE (issue_id, revision), \n\tCONSTRAINT ck_foundation_issue_revisions_issue_interval CHECK ("end" >= start), \n\tCONSTRAINT ck_foundation_issue_revisions_issue_state CHECK (state IN (\'suspected\',\'confirmed\',\'resolved\',\'dismissed\'))\n)\n\n')
    op.execute('\nCREATE TABLE foundation_runtime_archives (\n\timage_digest VARCHAR(80) NOT NULL, \n\tarchive_hash VARCHAR(64) NOT NULL, \n\tarchive_key VARCHAR(128) NOT NULL, \n\tbyte_count INTEGER NOT NULL, \n\tverification_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_runtime_archives PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_runtime_archives_archive_size CHECK (byte_count > 0), \n\tCONSTRAINT uq_foundation_runtime_archives_image_digest UNIQUE (image_digest)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_execution_archives (\n\texecution_id UUID NOT NULL, \n\tarchive_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_execution_archives PRIMARY KEY (execution_id)\n)\n\n')
    op.execute('CREATE INDEX ix_foundation_work_scope_key ON foundation_work (scope_key)')
    op.execute('CREATE INDEX ix_foundation_work_status ON foundation_work (status)')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_parent_release_id_foundation_officia_4a93 FOREIGN KEY(parent_release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_execution_id_foundation_execution_manifests FOREIGN KEY(execution_id) REFERENCES foundation_execution_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_policy_id_foundation_definitions FOREIGN KEY(policy_id) REFERENCES foundation_definitions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_contract_id_foundation_definitions FOREIGN KEY(contract_id) REFERENCES foundation_definitions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_candidate_manifest_id_foundation_can_161e FOREIGN KEY(candidate_manifest_id) REFERENCES foundation_candidate_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_dependency_id_foundation_dependency__efc8 FOREIGN KEY(dependency_id) REFERENCES foundation_dependency_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work ADD CONSTRAINT fk_foundation_work_source_ref_id_foundation_source_refs FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work_attempts ADD CONSTRAINT fk_foundation_work_attempts_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work_units ADD CONSTRAINT fk_foundation_work_units_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_work_events ADD CONSTRAINT fk_foundation_work_events_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidates ADD CONSTRAINT fk_foundation_candidates_source_ref_id_foundation_source_refs FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidates ADD CONSTRAINT fk_foundation_candidates_binding_id_foundation_source_bindings FOREIGN KEY(binding_id) REFERENCES foundation_source_bindings (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidates ADD CONSTRAINT fk_foundation_candidates_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidates ADD CONSTRAINT fk_foundation_candidates_dependency_id_foundation_depen_96b4 FOREIGN KEY(dependency_id) REFERENCES foundation_dependency_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidates ADD CONSTRAINT fk_foundation_candidates_assessment_id_foundation_assessments FOREIGN KEY(assessment_id) REFERENCES foundation_assessments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_bar_candidates ADD CONSTRAINT fk_foundation_bar_candidates_candidate_id_foundation_candidates FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_bar_candidates ADD CONSTRAINT fk_foundation_bar_candidates_instrument_id_instruments FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidate_manifests ADD CONSTRAINT fk_foundation_candidate_manifests_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidate_entries ADD CONSTRAINT fk_foundation_candidate_entries_candidate_id_foundation_41c8 FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_candidate_entries ADD CONSTRAINT fk_foundation_candidate_entries_manifest_id_foundation__698a FOREIGN KEY(manifest_id) REFERENCES foundation_candidate_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_decisions ADD CONSTRAINT fk_foundation_decisions_candidate_manifest_id_foundatio_91b2 FOREIGN KEY(candidate_manifest_id) REFERENCES foundation_candidate_manifests (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_decisions ADD CONSTRAINT fk_foundation_decisions_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_decisions ADD CONSTRAINT fk_foundation_decisions_parent_official_id_foundation_b_7e95 FOREIGN KEY(parent_official_id) REFERENCES foundation_bar_official_revisions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_decisions ADD CONSTRAINT fk_foundation_decisions_assessment_id_foundation_assessments FOREIGN KEY(assessment_id) REFERENCES foundation_assessments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_decisions ADD CONSTRAINT fk_foundation_decisions_selected_candidate_id_foundatio_5cf7 FOREIGN KEY(selected_candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_bar_official_revisions ADD CONSTRAINT fk_foundation_bar_official_revisions_decision_id_founda_ac73 FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_bar_official_revisions ADD CONSTRAINT fk_foundation_bar_official_revisions_instrument_id_instruments FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_bar_official_revisions ADD CONSTRAINT fk_foundation_bar_official_revisions_candidate_id_found_3f55 FOREIGN KEY(candidate_id) REFERENCES foundation_candidates (id) ON DELETE RESTRICT')
    op.execute('CREATE INDEX ix_foundation_official_releases_scope_key ON foundation_official_releases (scope_key)')
    op.execute('ALTER TABLE foundation_official_releases ADD CONSTRAINT fk_foundation_official_releases_parent_id_foundation_of_2b64 FOREIGN KEY(parent_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_official_releases ADD CONSTRAINT fk_foundation_official_releases_work_id_foundation_work FOREIGN KEY(work_id) REFERENCES foundation_work (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_block_members ADD CONSTRAINT fk_foundation_block_members_block_id_foundation_release_blocks FOREIGN KEY(block_id) REFERENCES foundation_release_blocks (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_block_members ADD CONSTRAINT fk_foundation_block_members_instrument_id_instruments FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_block_members ADD CONSTRAINT fk_foundation_block_members_official_id_foundation_bar__f9eb FOREIGN KEY(official_id) REFERENCES foundation_bar_official_revisions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_block_members ADD CONSTRAINT fk_foundation_block_members_decision_id_foundation_decisions FOREIGN KEY(decision_id) REFERENCES foundation_decisions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_release_block_refs ADD CONSTRAINT fk_foundation_release_block_refs_block_id_foundation_re_e84e FOREIGN KEY(block_id) REFERENCES foundation_release_blocks (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_release_block_refs ADD CONSTRAINT fk_foundation_release_block_refs_release_id_foundation__fe96 FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_heads ADD CONSTRAINT fk_foundation_heads_release_id_foundation_official_releases FOREIGN KEY(release_id) REFERENCES foundation_official_releases (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_issue_revisions ADD CONSTRAINT fk_foundation_issue_revisions_scope_key_foundation_issue_scopes FOREIGN KEY(scope_key) REFERENCES foundation_issue_scopes (scope_key) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_issue_revisions ADD CONSTRAINT fk_foundation_issue_revisions_instrument_id_instruments FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_issue_revisions ADD CONSTRAINT fk_foundation_issue_revisions_official_id_foundation_ba_1411 FOREIGN KEY(official_id) REFERENCES foundation_bar_official_revisions (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_execution_archives ADD CONSTRAINT fk_foundation_execution_archives_archive_id_foundation__8852 FOREIGN KEY(archive_id) REFERENCES foundation_runtime_archives (id) ON DELETE RESTRICT')
    op.execute('ALTER TABLE foundation_execution_archives ADD CONSTRAINT fk_foundation_execution_archives_execution_id_foundatio_c297 FOREIGN KEY(execution_id) REFERENCES foundation_execution_manifests (id) ON DELETE RESTRICT')
    _protect()


def downgrade():
    op.drop_constraint('fk_foundation_work_parent_release_id_foundation_officia_4a93', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_execution_id_foundation_execution_manifests', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_policy_id_foundation_definitions', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_contract_id_foundation_definitions', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_candidate_manifest_id_foundation_can_161e', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_dependency_id_foundation_dependency__efc8', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_source_ref_id_foundation_source_refs', 'foundation_work', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_attempts_work_id_foundation_work', 'foundation_work_attempts', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_units_work_id_foundation_work', 'foundation_work_units', type_="foreignkey")
    op.drop_constraint('fk_foundation_work_events_work_id_foundation_work', 'foundation_work_events', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidates_source_ref_id_foundation_source_refs', 'foundation_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidates_binding_id_foundation_source_bindings', 'foundation_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidates_work_id_foundation_work', 'foundation_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidates_dependency_id_foundation_depen_96b4', 'foundation_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidates_assessment_id_foundation_assessments', 'foundation_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_bar_candidates_candidate_id_foundation_candidates', 'foundation_bar_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_bar_candidates_instrument_id_instruments', 'foundation_bar_candidates', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidate_manifests_work_id_foundation_work', 'foundation_candidate_manifests', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidate_entries_candidate_id_foundation_41c8', 'foundation_candidate_entries', type_="foreignkey")
    op.drop_constraint('fk_foundation_candidate_entries_manifest_id_foundation__698a', 'foundation_candidate_entries', type_="foreignkey")
    op.drop_constraint('fk_foundation_decisions_candidate_manifest_id_foundatio_91b2', 'foundation_decisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_decisions_work_id_foundation_work', 'foundation_decisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_decisions_parent_official_id_foundation_b_7e95', 'foundation_decisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_decisions_assessment_id_foundation_assessments', 'foundation_decisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_decisions_selected_candidate_id_foundatio_5cf7', 'foundation_decisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_bar_official_revisions_decision_id_founda_ac73', 'foundation_bar_official_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_bar_official_revisions_instrument_id_instruments', 'foundation_bar_official_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_bar_official_revisions_candidate_id_found_3f55', 'foundation_bar_official_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_official_releases_parent_id_foundation_of_2b64', 'foundation_official_releases', type_="foreignkey")
    op.drop_constraint('fk_foundation_official_releases_work_id_foundation_work', 'foundation_official_releases', type_="foreignkey")
    op.drop_constraint('fk_foundation_block_members_block_id_foundation_release_blocks', 'foundation_block_members', type_="foreignkey")
    op.drop_constraint('fk_foundation_block_members_instrument_id_instruments', 'foundation_block_members', type_="foreignkey")
    op.drop_constraint('fk_foundation_block_members_official_id_foundation_bar__f9eb', 'foundation_block_members', type_="foreignkey")
    op.drop_constraint('fk_foundation_block_members_decision_id_foundation_decisions', 'foundation_block_members', type_="foreignkey")
    op.drop_constraint('fk_foundation_release_block_refs_block_id_foundation_re_e84e', 'foundation_release_block_refs', type_="foreignkey")
    op.drop_constraint('fk_foundation_release_block_refs_release_id_foundation__fe96', 'foundation_release_block_refs', type_="foreignkey")
    op.drop_constraint('fk_foundation_heads_release_id_foundation_official_releases', 'foundation_heads', type_="foreignkey")
    op.drop_constraint('fk_foundation_issue_revisions_scope_key_foundation_issue_scopes', 'foundation_issue_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_issue_revisions_instrument_id_instruments', 'foundation_issue_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_issue_revisions_official_id_foundation_ba_1411', 'foundation_issue_revisions', type_="foreignkey")
    op.drop_constraint('fk_foundation_execution_archives_archive_id_foundation__8852', 'foundation_execution_archives', type_="foreignkey")
    op.drop_constraint('fk_foundation_execution_archives_execution_id_foundatio_c297', 'foundation_execution_archives', type_="foreignkey")
    op.drop_table('foundation_execution_archives')
    op.drop_table('foundation_runtime_archives')
    op.drop_table('foundation_issue_revisions')
    op.drop_table('foundation_issue_scopes')
    op.drop_table('foundation_heads')
    op.drop_table('foundation_release_block_refs')
    op.drop_table('foundation_block_members')
    op.drop_table('foundation_release_blocks')
    op.drop_table('foundation_official_releases')
    op.drop_table('foundation_bar_official_revisions')
    op.drop_table('foundation_decisions')
    op.drop_table('foundation_candidate_entries')
    op.drop_table('foundation_candidate_manifests')
    op.drop_table('foundation_bar_candidates')
    op.drop_table('foundation_candidates')
    op.drop_table('foundation_assessments')
    op.drop_table('foundation_work_events')
    op.drop_table('foundation_work_units')
    op.drop_table('foundation_work_attempts')
    op.drop_table('foundation_work')
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_work()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_release()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_member_insert()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_ref_insert()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_candidate_entry()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_head()")


def _protect():
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_work_attempts FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_work_units FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_work_events FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_assessments FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_candidates FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_bar_candidates FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_candidate_manifests FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_candidate_entries FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_decisions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_bar_official_revisions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_release_blocks FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_block_members FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_release_block_refs FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_issue_revisions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_runtime_archives FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_execution_archives FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("""CREATE FUNCTION foundation_guard_work() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'work evidence is retained'; END IF;
      IF (to_jsonb(NEW) - ARRAY['status','cursor','total','lease_epoch','lease_until','cancelled']) IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['status','cursor','total','lease_epoch','lease_until','cancelled'])
        OR NEW.cursor < OLD.cursor OR NEW.lease_epoch < OLD.lease_epoch
      THEN RAISE EXCEPTION 'work inputs and committed cursor are immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_work_guard BEFORE UPDATE OR DELETE ON foundation_work FOR EACH ROW EXECUTE FUNCTION foundation_guard_work()")
    op.execute("""CREATE FUNCTION foundation_guard_release() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'release evidence is retained'; END IF;
      IF (to_jsonb(NEW) - ARRAY['status','published_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','published_at'])
        OR NOT ((OLD.status = 'draft' AND NEW.status = 'sealed' AND NEW.published_at IS NULL)
          OR (OLD.status = 'sealed' AND NEW.status = 'published' AND NEW.published_at IS NOT NULL)
          OR (OLD.status IN ('draft','sealed') AND NEW.status = 'abandoned' AND NEW.published_at IS NULL))
      THEN RAISE EXCEPTION 'invalid immutable release transition'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_release_guard BEFORE UPDATE OR DELETE ON foundation_official_releases FOR EACH ROW EXECUTE FUNCTION foundation_guard_release()")
    op.execute("""CREATE FUNCTION foundation_guard_member_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM foundation_release_blocks WHERE id = NEW.block_id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM foundation_release_block_refs WHERE block_id = NEW.block_id)
      THEN RAISE EXCEPTION 'sealed block cannot receive members'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_member_insert BEFORE INSERT ON foundation_block_members FOR EACH ROW EXECUTE FUNCTION foundation_guard_member_insert()")
    op.execute("""CREATE FUNCTION foundation_guard_ref_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT status FROM foundation_official_releases WHERE id = NEW.release_id FOR UPDATE) <> 'draft'
      THEN RAISE EXCEPTION 'sealed release cannot receive references'; END IF;
      PERFORM 1 FROM foundation_release_blocks WHERE id = NEW.block_id FOR UPDATE;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_ref_insert BEFORE INSERT ON foundation_release_block_refs FOR EACH ROW EXECUTE FUNCTION foundation_guard_ref_insert()")
    op.execute("""CREATE FUNCTION foundation_guard_candidate_entry() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM foundation_work w JOIN foundation_candidate_manifests m ON m.work_id = w.id
        WHERE m.id = NEW.manifest_id AND w.status = 'succeeded')
        OR EXISTS (SELECT 1 FROM foundation_work WHERE candidate_manifest_id = NEW.manifest_id)
      THEN RAISE EXCEPTION 'sealed candidate manifest cannot receive entries'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_candidate_entry_insert BEFORE INSERT ON foundation_candidate_entries FOR EACH ROW EXECUTE FUNCTION foundation_guard_candidate_entry()")
    op.execute("""CREATE FUNCTION foundation_guard_head() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'head cannot be deleted'; END IF;
      IF TG_OP = 'UPDATE' AND (NEW.scope_key <> OLD.scope_key OR NEW.revision <> OLD.revision + 1)
      THEN RAISE EXCEPTION 'head revision must advance once'; END IF;
      IF NOT EXISTS (SELECT 1 FROM foundation_official_releases WHERE id = NEW.release_id AND status = 'published' AND scope_key = NEW.scope_key)
      THEN RAISE EXCEPTION 'head must reference a published release in its scope'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_head_guard BEFORE INSERT OR UPDATE OR DELETE ON foundation_heads FOR EACH ROW EXECUTE FUNCTION foundation_guard_head()")
