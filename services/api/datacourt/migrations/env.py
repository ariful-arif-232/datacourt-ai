from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from datacourt.config import get_settings
from datacourt.db.models import Base
from datacourt.db.session import is_pooled_url

# Serialises concurrent `alembic upgrade` runs (e.g. two worker runs starting together). A
# transaction-scoped lock also works through a transaction-mode connection pooler.
MIGRATION_LOCK_ID = 7_310_442_901

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = get_settings().database_url
    connect_args: dict = {"connect_timeout": 30}
    if is_pooled_url(url):
        connect_args["prepare_threshold"] = None
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK_ID})
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
