"""Shared test fixtures."""

from __future__ import annotations

import pytest

from researchd.persistence.transaction import init_db, make_engine, make_session_factory


@pytest.fixture()
def db_factory(tmp_path):
    """SQLite engine + session factory on a temp file, tables created."""
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()
