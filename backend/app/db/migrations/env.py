from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base
from app.data_store.tables import metadata as current_store_metadata
from app.legacy_reset.tables import metadata as legacy_maintenance_metadata


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Preserve the application's existing naming convention for historical Alembic
# operations. A metadata list would change names created by old op helpers.
for current_table in (*current_store_metadata.sorted_tables, *legacy_maintenance_metadata.sorted_tables):
    if current_table.name not in Base.metadata.tables:
        current_table.to_metadata(Base.metadata)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_settings().database_url.render_as_string(
        hide_password=False
    )
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
