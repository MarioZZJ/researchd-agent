"""researchd service: the long-lived process and sole database writer.

Runs the FastAPI internal API over a Unix domain socket plus the scheduler
loop (Phase 3 fills the loop body; the frame is here so the process lifecycle
is real).
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


class SchedulerLoop:
    """Main loop per IMPLEMENTATION.md §14. Phase 3 implements each stage;
    the harness with graceful shutdown lives here."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        # Phase 3: ingest, reconcile, dispatch, collect, review, gate, report, outbox
        pass

    def stop(self) -> None:
        self._stop.set()


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

    scheduler = SchedulerLoop(settings)

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
        lock.release()
        logger.info("service_stopped")
