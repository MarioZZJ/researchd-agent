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
    """Bearer token check for MUTATING endpoints.

    Enforced on every transport, including UDS. Rationale (threat model T4,
    blocker B-08): executors run as the same uid as the service, so socket
    permissions alone cannot distinguish a trusted gateway from an executor;
    the token lives in the 0600 env file and is NOT in the executor env
    whitelist. Read-only endpoints (healthz/readyz/GET) stay unauthenticated.
    """
    settings = request.app.state.settings
    expected = settings.api.token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="mutating API requires RESEARCHD_API__TOKEN to be configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[len("Bearer "):] != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def is_uds(settings) -> bool:  # noqa: ANN001
    return bool(settings.api.socket_path)
