"""Internal HTTP API (IMPLEMENTATION.md §18).

Serves over a Unix domain socket by default (TCP fallback is 127.0.0.1 only,
and then requires a Bearer token). Every route runs inside `researchd service`,
the sole database writer.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import Settings
from ..domain.base import Actor, utcnow
from ..persistence.repositories import DecisionRepo, ProjectRepo, TaskRepo
from ..persistence.transaction import UnitOfWork, make_engine, make_session_factory
from .dependencies import require_token
from .routes import inbound, projects


def create_app(settings: Settings) -> FastAPI:
    engine = make_engine(settings.db_path)
    factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        # tighten UDS socket permissions after uvicorn binds it
        if settings.api.socket_path:
            sock = Path(settings.api.socket_path)
            try:
                os.chmod(sock, 0o600)
            except FileNotFoundError:
                pass
        yield
        engine.dispose()

    app = FastAPI(title="researchd internal API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory

    # health (no auth; no sensitive data)
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": settings.service_name, "ts": utcnow().isoformat()}

    @app.get("/readyz")
    def readyz() -> dict:
        from sqlalchemy import text

        with factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ready"}

    # every state-changing route carries its own Depends(require_token);
    # read-only routes (healthz/readyz/GET) stay unauthenticated.
    app.include_router(inbound.router)
    app.include_router(projects.router)
    return app
