"""Alembic environment. Database URL resolution:

1. RESEARCHD_DB env var (absolute path or sqlite:/// URL)
2. --x db-url=... on the command line
3. default: .data/researchd.db relative to the repo root

SQLite gets the required PRAGMAs on every connection (WAL, FK, busy_timeout,
NORMAL synchronous).
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event

from researchd.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    env_db = os.environ.get("RESEARCHD_DB")
    if env_db:
        return env_db if env_db.startswith("sqlite") else f"sqlite:///{env_db}"
    return f"sqlite:///{(REPO_ROOT / '.data' / 'researchd.db')}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_resolve_url(), connect_args={"timeout": 10})

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        for key, value in (("journal_mode", "WAL"), ("foreign_keys", "ON"), ("busy_timeout", 5000), ("synchronous", "NORMAL")):
            cur.execute(f"PRAGMA {key} = {value}")
        cur.close()

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
