"""Alembic migration environment.

Uses the application's configured database URL (``DZ_DATABASE_URL``) and the ORM
metadata, so migrations and the app never drift.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from degreezeor.config import settings
from degreezeor.core.models import Base

config = context.config
# Use the app's env-driven DSN UNLESS a concrete URL was explicitly provided (e.g. by a
# test or a one-off command). The alembic.ini default is the 'driver://' placeholder.
_existing = config.get_main_option("sqlalchemy.url")
if not _existing or _existing.startswith("driver://"):
    config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
