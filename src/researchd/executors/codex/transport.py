"""Codex App Server transport (codex-cli 0.146.0, IMPLEMENTATION.md §23 Phase 5).

Protocol: JSON-RPC over stdio (`codex app-server --listen stdio://`), v2
schemas generated from `codex app-server generate-json-schema`. Verified
methods (from the v2 schema): initialize, thread/start, thread/resume,
thread/fork, thread/read, turn/start (input+threadId required; model, effort,
outputSchema optional), turn/steer (expectedTurnId precondition), turn/interrupt.

Turn completion is delivered via notifications (TurnStartedNotification /
TurnCompletedNotification with the turn id); the transport keeps a per-thread
bounded notification queue and a completion waiter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from abc import ABC, abstractmethod
from pathlib import Path

from ..reasonix.overlay import ENV_WHITELIST

logger = logging.getLogger("researchd.codex")

COMPLETION_NOTIFICATIONS = ("turn/completed", "turn/status_changed", "turn/updated")
TURN_COMPLETED_METHODS = ("turn/completed",)


class TransportError(RuntimeError):
    pass


class CodexTransport(ABC):
    name: str = "abstract"

    @abstractmethod
    async def initialize(self) -> dict: ...

    @abstractmethod
    async def thread_start(self, params: dict) -> dict: ...

    @abstractmethod
    async def turn_start(self, params: dict) -> dict: ...

    @abstractmethod
    async def turn_steer(self, params: dict) -> dict: ...

    @abstractmethod
    async def turn_interrupt(self, params: dict) -> dict: ...

    @abstractmethod
    async def thread_close(self, thread_id: str) -> None: ...

    @abstractmethod
    async def wait_for_turn(self, thread_id: str, turn_id: str, *, timeout: float) -> dict:
        """Wait until the turn reaches a terminal status; returns the turn
        status payload (status + error + items)."""

    async def close_all(self) -> None:  # pragma: no cover - optional
        pass


# ---------------------------------------------------------------- stdio
class StdioCodexTransport(CodexTransport):
    name = "codex-stdio"

    def __init__(
        self,
        binary: str = "codex",
        *,
        workdir: str | None = None,
        codex_home: str | None = None,
        env: dict | None = None,
    ):
        self.binary = binary
        self.workdir = workdir
        self.codex_home = codex_home
        self.extra_env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._init_done = False
        self.notifications: dict[str, list] = {}  # thread_id -> bounded list
        self._notif_limit = 200
        self._turn_waiters: dict[str, list[asyncio.Future]] = {}  # turn_id -> waiters

    # ------------------------------------------------------------ lifecycle
    async def _start(self) -> None:
        async with self._start_lock:
            if self._proc is not None:
                return
            env = {k: v for k, v in os.environ.items() if k in ENV_WHITELIST}
            env.update(self.extra_env)
            if self.codex_home:
                # ~/.codex is read-only in this environment; run with an
                # isolated CODEX_HOME inside the restricted data dir.
                # Absolute path: codex resolves CODEX_HOME against its cwd.
                env["CODEX_HOME"] = os.path.abspath(self.codex_home)
            cwd = self.workdir
            self._proc = await asyncio.create_subprocess_exec(
                self.binary,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
            self._reader = self._proc.stdout
            self._writer = self._proc.stdin
            self._read_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.debug("codex stderr: %s", _truncate(line.decode("utf-8", errors="replace").rstrip()))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif mid is None:
                    self._on_notification(msg)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TransportError("codex app-server process exited"))
            self._pending.clear()
            for waiters in self._turn_waiters.values():
                for w in waiters:
                    if not w.done():
                        w.set_result({"status": "failed", "error": {"message": "codex process exited"}})
            self._turn_waiters.clear()
            self._proc = None
            self._init_done = False  # restart -> re-initialize

    def _on_notification(self, msg: dict) -> None:
        params = msg.get("params") or {}
        thread_id = params.get("threadId") or "_global"
        bucket = self.notifications.setdefault(thread_id, [])
        bucket.append(msg)
        if len(bucket) > self._notif_limit:
            del bucket[: len(bucket) - self._notif_limit]
        method = msg.get("method", "")
        params = msg.get("params") or {}
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if turn_id and method in TURN_COMPLETED_METHODS:
            turn = params.get("turn") or {}
            status = turn.get("status", "")
            if status in ("completed", "failed", "interrupted"):
                for w in self._turn_waiters.pop(turn_id, []):
                    if not w.done():
                        w.set_result(turn)

    async def _call(self, method: str, params: dict | None = None, *, timeout: float = 120.0) -> dict:
        await self._start()
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        payload = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        assert self._writer is not None
        try:
            self._writer.write((json.dumps(payload) + "\n").encode())
            await self._writer.drain()
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(mid, None)
            raise TransportError(f"codex {method}: timeout") from exc
        except asyncio.CancelledError:
            self._pending.pop(mid, None)
            raise
        if "error" in resp:
            raise TransportError(f"codex {method}: {sanitize_rpc_error(resp['error'])}")
        return resp.get("result", {})

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        await self._start()
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        assert self._writer is not None
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()

    # ------------------------------------------------------------ protocol
    async def initialize(self) -> dict:
        async with self._init_lock:
            if self._init_done:
                return {}
            result = await self._call(
                "initialize",
                {"clientInfo": {"name": "researchd", "version": "0.1.0"}},
            )
            # required by the App Server protocol after a successful initialize
            await self._send_notification("initialized", {})
            self._init_done = True
            return result

    async def thread_start(self, params: dict) -> dict:
        return await self._call("thread/start", params, timeout=60.0)

    async def turn_start(self, params: dict) -> dict:
        return await self._call("turn/start", params, timeout=60.0)

    async def turn_steer(self, params: dict) -> dict:
        return await self._call("turn/steer", params, timeout=60.0)

    async def turn_interrupt(self, params: dict) -> dict:
        return await self._call("turn/interrupt", params, timeout=60.0)

    async def thread_close(self, thread_id: str) -> None:
        """Archive the thread (thread/delete is NOT in the v2 surface; verified
        against the 0.146.0 schema — only thread/archive exists)."""
        try:
            await self._call("thread/archive", {"threadId": thread_id}, timeout=15.0)
        except TransportError:
            pass  # idempotent cleanup

    async def wait_for_turn(self, thread_id: str, turn_id: str, *, timeout: float) -> dict:
        """Wait for a terminal turn status: completed | interrupted | failed.

        Waits on notifications (TurnCompletedNotification); falls back to
        polling turn state via the completion events already received.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._turn_waiters.setdefault(turn_id, []).append(fut)
        # also scan notifications already received
        for n in self.notifications.get(thread_id, []):
            p = n.get("params") or {}
            if (p.get("turnId") or p.get("turn", {}).get("id")) == turn_id and n.get("method") in TURN_COMPLETED_METHODS:
                turn = p.get("turn") or {}
                if turn.get("status") in ("completed", "failed", "interrupted") and not fut.done():
                    fut.set_result(turn)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TransportError(f"codex turn {turn_id} did not complete within {timeout}s")
        finally:
            # remove only THIS waiter; keep the key while others wait
            waiters = self._turn_waiters.get(turn_id)
            if waiters:
                if fut in waiters:
                    waiters.remove(fut)
                if not waiters:
                    self._turn_waiters.pop(turn_id, None)

    async def close_all(self) -> None:
        proc = self._proc
        if self._read_task is not None:
            self._read_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._proc = None
        self._writer = None


# ---------------------------------------------------------------- fake
class FakeCodexTransport(CodexTransport):
    """Scripted double recording the request sequence (conformance tests)."""

    name = "codex-fake"

    def __init__(self):
        self.calls: list[dict] = []
        self._thread_counter = 0
        self._turn_counter = 0
        self.scripted_turn_results: list[dict] = []
        self.auto_complete: bool = True

    def script_turn_completion(self, *, status: str = "completed", items: list | None = None, error: str | None = None) -> None:
        self.scripted_turn_results.append(
            {"status": status, "items": items or [], "error": {"message": error} if error else None}
        )

    async def _next(self, method: str, params: dict) -> dict:
        self.calls.append({"method": method, "params": params})
        if method == "thread/start":
            self._thread_counter += 1
            return {"thread": {"id": f"THREAD-{self._thread_counter}"}}
        if method == "turn/start":
            self._turn_counter += 1
            return {"turn": {"id": f"TURN-{self._turn_counter}", "status": "inProgress", "items": []}}
        if method == "turn/steer":
            return {"turn": {"id": f"TURN-{self._turn_counter}", "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            return {"turnId": params.get("turnId"), "interrupted": True}
        if method == "thread/archive":
            return {"threadId": params.get("threadId")}
        return {}

    async def initialize(self) -> dict:
        return await self._next("initialize", {})

    async def thread_start(self, params: dict) -> dict:
        return await self._next("thread/start", params)

    async def turn_start(self, params: dict) -> dict:
        return await self._next("turn/start", params)

    async def turn_steer(self, params: dict) -> dict:
        return await self._next("turn/steer", params)

    async def turn_interrupt(self, params: dict) -> dict:
        return await self._next("turn/interrupt", params)

    async def thread_close(self, thread_id: str) -> None:
        await self._next("thread/archive", {"threadId": thread_id})

    async def wait_for_turn(self, thread_id: str, turn_id: str, *, timeout: float) -> dict:
        if self.scripted_turn_results:
            return self.scripted_turn_results.pop(0)
        if self.auto_complete:
            return {"status": "completed", "items": [], "error": None}
        raise TransportError(f"codex turn {turn_id} did not complete within {timeout}s")


def sanitize_rpc_error(err: dict) -> str:
    """Only code + status + short message; never raw additionalDetails."""
    code = err.get("code")
    message = str(err.get("message", ""))[:300]
    return f"code={code} message={message}"


def _truncate(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
