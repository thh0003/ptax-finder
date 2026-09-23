from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ptax.config import Settings
from ptax.db.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep the app's own loggers (e.g. ptax.migrate) alive when run in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The URL comes from Settings (env / .env) unless a caller already set it on the config.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
