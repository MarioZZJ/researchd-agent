"""Transaction infrastructure: SQLite engine with required PRAGMAs, sessions, unit of work.

Only `researchd service` may write the database (IMPLEMENTATION.md §4).
Every scientifically meaningful state change runs in ONE transaction:
aggregate update + event append + optional outbox insert, then commit.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

REQUIRED_PRAGMAS = {
    "journal_mode": "WAL",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
    "synchronous": "NORMAL",
}


class OptimisticConcurrencyError(Exception):
    """Raised when an aggregate update touches a stale version."""


def make_engine(db_path: str | Path, *, echo: bool = False) -> Engine:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        connect_args={"timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        for key, value in REQUIRED_PRAGMAS.items():
            cur.execute(f"PRAGMA {key} = {value}")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def verify_pragmas(engine: Engine) -> dict[str, str]:
    """Read back actual PRAGMA values (used by researchctl doctor and tests)."""
    out: dict[str, str] = {}
    with engine.connect() as conn:
        for key in REQUIRED_PRAGMAS:
            row = conn.exec_driver_sql(f"PRAGMA {key}").fetchone()
            out[key] = str(row[0]) if row else "?"
    return out


class UnitOfWork:
    """One transaction boundary. Usage:

        with UnitOfWork(session_factory) as uow:
            task = uow.tasks.get(...)
            ...
            uow.commit()
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self._factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self._factory()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.session is not None:
            try:
                if exc_type is None and self.session.is_active:
                    self.session.commit()
            except Exception:
                self.session.rollback()
                raise
            finally:
                self.session.close()
                self.session = None

    def commit(self) -> None:
        assert self.session is not None
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def rollback(self) -> None:
        assert self.session is not None
        self.session.rollback()


@contextmanager
def session_scope(session_factory: sessionmaker[Session]):
    """Lightweight scope for read paths."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create all tables (tests / fresh installs). Production uses Alembic."""
    Base.metadata.create_all(engine)


def db_env() -> dict[str, str]:
    """Expose runtime db path for diagnostics (no secrets)."""
    return {
        "db_path": os.environ.get("RESEARCHD_DB", ".data/researchd.db"),
        "data_dir": os.environ.get("RESEARCHD_DATA_DIR", ".data"),
    }
