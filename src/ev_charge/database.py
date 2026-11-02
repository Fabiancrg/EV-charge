"""Database engine, session factory and Alembic-driven initialisation."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import CONFIG

logger = logging.getLogger(__name__)

DATABASE_URL: str = CONFIG.database_url or "sqlite:///./ev_charge.db"

if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.endswith(":memory:"):
    Path(DATABASE_URL.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations(eng: Engine, revision: str = "head") -> None:
    """Bring the schema of a database to an Alembic revision."""
    from alembic import command
    from alembic.config import Config

    pkg_dir = Path(__file__).parent
    cfg = Config()
    cfg.set_main_option("script_location", str(pkg_dir / "migrations"))
    cfg.set_main_option("sqlalchemy.url", str(eng.url))
    with eng.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Migrations that rebuild a table (Alembic "batch" mode) must not trip over foreign keys
            # referencing it; the pragma only works outside a transaction.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # end the implicit transaction SQLAlchemy opened for the pragma
        try:
            with connection.begin():
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, revision)
        finally:
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


def init_db() -> None:
    """Bring the schema to the latest Alembic revision."""
    run_migrations(engine)
    logger.info(f"Database ready: {DATABASE_URL}")
