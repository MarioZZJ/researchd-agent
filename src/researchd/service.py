"""researchd service: the long-lived process and sole database writer.

Runs the FastAPI internal API over a Unix domain socket (TCP fallback with
bearer token) plus the scheduler loop (IMPLEMENTATION.md §14).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog
import uvicorn

from .config import Settings

logger = structlog.get_logger("researchd.service")


async def run_service(settings: Settings) -> None:
    settings.ensure_dirs()
    loop = asyncio.get_running_loop()

    from .api.app import create_app
    from .persistence.locking import DataDirLock, DataDirLockedError

    # exclusive writer lock: no other service/migrate may touch this data dir
    lock = DataDirLock(settings.data_dir)
    try:
        lock.acquire()
    except DataDirLockedError as exc:
        logger.error("service_start_failed", error=str(exc))
        sys.exit(1)

    app = create_app(settings)

    # UDS transport preferred (IMPLEMENTATION.md §18); TCP fallback is
    # localhost-only and requires a bearer token.
    socket_path = Path(settings.api.socket_path)
    if settings.api.socket_path:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        if socket_path.exists():
            socket_path.unlink()

    if settings.api.socket_path:
        config = uvicorn.Config(
            app,
            uds=str(socket_path),
            log_level=settings.log_level,
            access_log=False,
        )
    else:
        config = uvicorn.Config(
            app,
            host=settings.api.tcp_host,
            port=settings.api.tcp_port,
            log_level=settings.log_level,
            access_log=False,
        )
    server = uvicorn.Server(config)

    if settings.api.socket_path:

        async def _tighten_socket() -> None:
            """uvicorn binds the UDS with the process umask; tighten it to
            0600 right after bind (owner only)."""
            for _ in range(100):
                if socket_path.exists():
                    try:
                        os.chmod(socket_path, 0o600)
                    except OSError:
                        pass
                    return
                await asyncio.sleep(0.1)

        loop.create_task(_tighten_socket())

    from .executors.fake import FakeExecutor
    from .scheduler.loop import SchedulerLoop

    executor = _build_executor(settings)
    delivery_port = _build_delivery_port(settings)
    scheduler = SchedulerLoop(
        settings,
        app.state.session_factory,
        executor,
        delivery_port,
        max_parallel=settings.scheduler.max_parallel,
    )

    def _shutdown(_sig, _frame) -> None:  # noqa: ANN001
        scheduler.stop()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _shutdown(s, None))

    scheduler_task = asyncio.create_task(scheduler.run())
    logger.info("service_started", socket=str(socket_path), db=settings.db_path)
    try:
        await server.serve()
    finally:
        scheduler.stop()
        await scheduler_task
        await executor.close()
        lock.release()
        logger.info("service_stopped")


def _build_executor(settings: Settings):
    """Executor adapter factory. `fake` is the default; reasonix/codex are
    registered by Phase 4/5 behind the same interface."""
    from .executors.fake import FakeExecutor

    kind = settings.scheduler.executor
    if kind == "fake":
        return FakeExecutor(workspace_root=Path(settings.data_dir))
    if kind == "reasonix":
        from .executors.reasonix import ReasonixAdapter

        return ReasonixAdapter(settings=settings)
    if kind == "codex":
        from .executors.codex import CodexAdapter

        return CodexAdapter(settings=settings)
    raise ValueError(f"unknown executor {kind!r}")


def _build_delivery_port(settings: Settings):
    """Delivery port factory: fake | cc_connect. cc_connect fails closed when
    the target is not fully configured; the token stays in settings and is
    never written to logs, the DB, artifacts, or the executor env."""
    if settings.scheduler.delivery == "fake":
        from .executors.fake import FakeDeliveryPort

        return FakeDeliveryPort()
    if settings.scheduler.delivery == "cc_connect":
        from .integrations.cc_connect.delivery import CcConnectDeliveryPort

        cc = settings.cc_connect
        if not cc.token or not cc.project:
            raise ValueError(
                "delivery=cc_connect requires RESEARCHD_CC_CONNECT__TOKEN and "
                "RESEARCHD_CC_CONNECT__PROJECT (fail-closed: no silent fallback)"
            )
        return CcConnectDeliveryPort(
            base_url=cc.base_url,
            token=cc.token,
            project=cc.project,
            session_key=cc.session_key,
            uds=cc.uds or None,
        )
    raise ValueError(f"unknown delivery {settings.scheduler.delivery!r}")
