"""Shared test fixtures."""

from __future__ import annotations

import pytest

from researchd.persistence.transaction import init_db, make_engine, make_session_factory


def pytest_configure(config):
    """Pin a SHORT basetemp: callers may run under a long TMPDIR (e.g. a
    reasonix session tmp), which pushes tmp_path past the 108-byte AF_UNIX
    sun_path limit for UDS-backed API tests. pytest recreates basetemp each
    run, so a fixed short path stays isolated per invocation."""
    config.option.basetemp = "/tmp/rd-pytest"


@pytest.fixture()
def db_factory(tmp_path):
    """SQLite engine + session factory on a temp file, tables created."""
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def factory(db_factory):
    """Alias for db_factory (used by integration tests)."""
    return db_factory
