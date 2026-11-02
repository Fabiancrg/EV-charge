"""Alembic environment. init_db() passes its connection; the CLI uses alembic.ini / DATABASE_URL."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from ev_charge import models  # noqa: F401  (registers the tables on Base.metadata)
from ev_charge.database import DATABASE_URL, Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    if not config.get_main_option("sqlalchemy.url"):
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
