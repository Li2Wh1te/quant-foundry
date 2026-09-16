"""Freeze local source evidence and protect its dependency closure."""
from alembic import op

revision = "20260922_01"
down_revision = "20260921_01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('\nCREATE TABLE foundation_artifacts (\n\tcontent_hash VARCHAR(64) NOT NULL, \n\tpayload BYTEA NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_artifacts PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_artifacts_artifact_size CHECK (length(payload) <= 16777216), \n\tCONSTRAINT uq_foundation_artifacts_content_hash UNIQUE (content_hash)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_baselines (\n\tevent_key VARCHAR(128) NOT NULL, \n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tsource VARCHAR(32) NOT NULL, \n\tdataset VARCHAR(128) NOT NULL, \n\tscope_json TEXT NOT NULL, \n\tobserved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\trow_count INTEGER NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_baselines PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_baselines_source UNIQUE (source, dataset, event_key), \n\tCONSTRAINT ck_foundation_baselines_baseline_count CHECK (row_count >= 0), \n\tCONSTRAINT uq_foundation_baselines_manifest_hash UNIQUE (manifest_hash)\n)\n\n')
    op.execute("\nCREATE TABLE foundation_definitions (\n\tkind VARCHAR(24) NOT NULL, \n\tname VARCHAR(128) NOT NULL, \n\tversion VARCHAR(128) NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tdefinition_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_definitions PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_definitions_kind UNIQUE (kind, name, version), \n\tCONSTRAINT ck_foundation_definitions_definition_kind CHECK (kind IN ('contract','series','policy','support'))\n)\n\n")
    op.execute('\nCREATE TABLE foundation_dependency_manifests (\n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_dependency_manifests PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_dependency_manifests_manifest_hash UNIQUE (manifest_hash)\n)\n\n')
    op.execute('\nCREATE TABLE foundation_baseline_blocks (\n\tbaseline_id UUID NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tpayload_json TEXT NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\trow_count INTEGER NOT NULL, \n\tCONSTRAINT pk_foundation_baseline_blocks PRIMARY KEY (baseline_id, ordinal), \n\tCONSTRAINT ck_foundation_baseline_blocks_block_count CHECK (row_count >= 0 AND ordinal >= 0), \n\tCONSTRAINT fk_foundation_baseline_blocks_baseline_id_foundation_baselines FOREIGN KEY(baseline_id) REFERENCES foundation_baselines (id) ON DELETE RESTRICT\n)\n\n')
    op.execute("\nCREATE TABLE foundation_execution_manifests (\n\tmanifest_hash VARCHAR(64) NOT NULL, \n\tmanifest_json TEXT NOT NULL, \n\tartifact_id UUID, \n\treplay_status VARCHAR(32) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_execution_manifests PRIMARY KEY (id), \n\tCONSTRAINT ck_foundation_execution_manifests_execution_replay CHECK (replay_status IN ('ready','dependency_missing')), \n\tCONSTRAINT uq_foundation_execution_manifests_manifest_hash UNIQUE (manifest_hash), \n\tCONSTRAINT fk_foundation_execution_manifests_artifact_id_foundatio_c11c FOREIGN KEY(artifact_id) REFERENCES foundation_artifacts (id) ON DELETE RESTRICT\n)\n\n")
    op.execute("\nCREATE TABLE foundation_source_refs (\n\tsource VARCHAR(32) NOT NULL, \n\tdataset VARCHAR(128) NOT NULL, \n\tsubject VARCHAR(64) NOT NULL, \n\tvariant VARCHAR(80) NOT NULL, \n\trepresentation VARCHAR(32) NOT NULL, \n\tlocator_hash VARCHAR(64) NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tobserved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tobservation_id UUID, \n\tbaseline_id UUID, \n\tdecoder_id UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_source_refs PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_source_refs_source UNIQUE (source, representation, locator_hash), \n\tCONSTRAINT ck_foundation_source_refs_source_locator CHECK ((representation = 'ths_observation' AND observation_id IS NOT NULL AND baseline_id IS NULL) OR (representation = 'local_table_baseline' AND baseline_id IS NOT NULL AND observation_id IS NULL)), \n\tCONSTRAINT fk_foundation_source_refs_observation_id_tonghuashun_ob_bd6c FOREIGN KEY(observation_id) REFERENCES tonghuashun_observations (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_source_refs_baseline_id_foundation_baselines FOREIGN KEY(baseline_id) REFERENCES foundation_baselines (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_source_refs_decoder_id_foundation_executi_9836 FOREIGN KEY(decoder_id) REFERENCES foundation_execution_manifests (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('\nCREATE TABLE foundation_observation_dependencies (\n\tsource_ref_id UUID NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tCONSTRAINT pk_foundation_observation_dependencies PRIMARY KEY (source_ref_id, observation_id), \n\tCONSTRAINT fk_foundation_observation_dependencies_source_ref_id_fo_16d5 FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_observation_dependencies_observation_id_t_7ccc FOREIGN KEY(observation_id) REFERENCES tonghuashun_observations (id) ON DELETE RESTRICT\n)\n\n')
    op.execute("\nCREATE TABLE foundation_source_bindings (\n\tsource_ref_id UUID NOT NULL, \n\tsource VARCHAR(32) NOT NULL, \n\tsubject VARCHAR(64) NOT NULL, \n\tinstrument_id UUID NOT NULL, \n\tvalid_from DATE NOT NULL, \n\tvalid_to DATE NOT NULL, \n\tknown_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tbinding_version VARCHAR(128) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tevidence_json TEXT NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_foundation_source_bindings PRIMARY KEY (id), \n\tCONSTRAINT uq_foundation_source_bindings_source_ref_id UNIQUE (source_ref_id, subject, binding_version), \n\tCONSTRAINT ck_foundation_source_bindings_binding_interval CHECK (valid_to > valid_from), \n\tCONSTRAINT ck_foundation_source_bindings_binding_status CHECK (status IN ('resolved','unresolved')), \n\tCONSTRAINT fk_foundation_source_bindings_source_ref_id_foundation__5df8 FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_source_bindings_instrument_id_instruments FOREIGN KEY(instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT\n)\n\n")
    op.execute('\nCREATE TABLE foundation_dependency_entries (\n\tmanifest_id UUID NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tsource_ref_id UUID, \n\tbinding_id UUID, \n\texecution_id UUID, \n\tdefinition_id UUID, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tpurpose VARCHAR(128) NOT NULL, \n\tCONSTRAINT pk_foundation_dependency_entries PRIMARY KEY (manifest_id, ordinal), \n\tCONSTRAINT ck_foundation_dependency_entries_one_dependency CHECK ((CASE WHEN source_ref_id IS NULL THEN 0 ELSE 1 END + CASE WHEN binding_id IS NULL THEN 0 ELSE 1 END + CASE WHEN execution_id IS NULL THEN 0 ELSE 1 END + CASE WHEN definition_id IS NULL THEN 0 ELSE 1 END) = 1), \n\tCONSTRAINT fk_foundation_dependency_entries_manifest_id_foundation_8780 FOREIGN KEY(manifest_id) REFERENCES foundation_dependency_manifests (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_dependency_entries_source_ref_id_foundati_633c FOREIGN KEY(source_ref_id) REFERENCES foundation_source_refs (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_dependency_entries_binding_id_foundation__a181 FOREIGN KEY(binding_id) REFERENCES foundation_source_bindings (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_dependency_entries_execution_id_foundatio_1ba3 FOREIGN KEY(execution_id) REFERENCES foundation_execution_manifests (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_foundation_dependency_entries_definition_id_foundati_5acf FOREIGN KEY(definition_id) REFERENCES foundation_definitions (id) ON DELETE RESTRICT\n)\n\n')
    _protect()


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS foundation_observation_guard ON tonghuashun_observations")
    op.drop_table('foundation_dependency_entries')
    op.drop_table('foundation_source_bindings')
    op.drop_table('foundation_observation_dependencies')
    op.drop_table('foundation_source_refs')
    op.drop_table('foundation_execution_manifests')
    op.drop_table('foundation_baseline_blocks')
    op.drop_table('foundation_dependency_manifests')
    op.drop_table('foundation_definitions')
    op.drop_table('foundation_baselines')
    op.drop_table('foundation_artifacts')
    op.execute("DROP FUNCTION IF EXISTS foundation_immutable()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_observation()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_block_insert()")
    op.execute("DROP FUNCTION IF EXISTS foundation_guard_binding()")


def _protect():
    op.execute("""CREATE FUNCTION foundation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'foundation evidence is immutable' USING ERRCODE = '23514'; END $$""")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_artifacts FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_baselines FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_definitions FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_dependency_manifests FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_baseline_blocks FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_execution_manifests FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_source_refs FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_observation_dependencies FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_source_bindings FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("CREATE TRIGGER foundation_immutable BEFORE UPDATE OR DELETE ON foundation_dependency_entries FOR EACH ROW EXECUTE FUNCTION foundation_immutable()")
    op.execute("""CREATE FUNCTION foundation_guard_observation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM foundation_observation_dependencies WHERE observation_id = OLD.id)
      THEN RAISE EXCEPTION 'referenced observation is immutable' USING ERRCODE = '23514'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_observation_guard BEFORE UPDATE ON tonghuashun_observations FOR EACH ROW EXECUTE FUNCTION foundation_guard_observation()")
    op.execute("""CREATE FUNCTION foundation_guard_block_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM foundation_baselines WHERE id = NEW.baseline_id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM foundation_source_refs WHERE baseline_id = NEW.baseline_id)
      THEN RAISE EXCEPTION 'sealed baseline cannot receive blocks' USING ERRCODE = '23514'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_block_insert BEFORE INSERT ON foundation_baseline_blocks FOR EACH ROW EXECUTE FUNCTION foundation_guard_block_insert()")
    op.execute("""CREATE FUNCTION foundation_guard_binding() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended(NEW.source || ':' || NEW.subject || ':' || NEW.binding_version, 0));
      IF EXISTS (SELECT 1 FROM foundation_source_bindings WHERE source = NEW.source AND subject = NEW.subject
        AND binding_version = NEW.binding_version AND valid_from < NEW.valid_to AND valid_to > NEW.valid_from)
      THEN RAISE EXCEPTION 'overlapping binding range' USING ERRCODE = '23514'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER foundation_binding_insert BEFORE INSERT ON foundation_source_bindings FOR EACH ROW EXECUTE FUNCTION foundation_guard_binding()")
