"""API dependencies: session, auth, UoW."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from ..persistence.transaction import UnitOfWork


def get_uow(request: Request) -> UnitOfWork:
    factory = request.app.state.session_factory
    uow = UnitOfWork(factory)
    uow.__enter__()
    try:
        yield uow
        if uow.session is not None and uow.session.is_active:
            uow.commit()
    except Exception:
        uow.rollback()
        raise
    finally:
        if uow.session is not None:
            uow.session.close()


def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer token check. Only enforced when the app listens on TCP; UDS is
    local-only by construction (IMPLEMENTATION.md §22: API must not be remotely
    reachable without auth)."""
    settings = request.app.state.settings
    if settings.api.socket_path:
        return  # UDS transport: socket permissions are the boundary
    expected = settings.api.token
    if not expected:
        raise HTTPException(status_code=503, detail="TCP transport requires RESEARCHD_API__TOKEN")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[len("Bearer "):] != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def is_uds(settings) -> bool:  # noqa: ANN001
    return bool(settings.api.socket_path)
