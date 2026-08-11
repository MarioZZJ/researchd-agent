"""ACP transport for reasonix (v1.21.2, IMPLEMENTATION.md §15.2).

Two implementations behind one interface:
- StdioReasonixTransport spawns the native reasonix binary (the `reasonix`
  bin on PATH is an npm shim that spawnSyncs the native binary — we resolve
  the native binary directly and run it in its own process group so
  terminate/kill reaches the real model process) with the isolated
  REASONIX_HOME overlay, and speaks JSON-RPC over stdio;
- FakeReasonixTransport is a scripted double used by conformance tests.

Real handshake verified in Phase 0/4: initialize + session/new work under the
isolated overlay; `session/status` is NOT implemented by v1.21.2 (-32601).
The `session/prompt` response shape follows the ACP spec (sessionId,
requestId, message{role,content}); the real wire shape is pinned by the gated
real-conformance test once model calls are authorized (B-03).
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import signal
from abc import ABC, abstractmethod
from pathlib import Path

from .overlay import ensure_overlay, overlay_env, overlay_workdir

logger = logging.getLogger("researchd.reasonix")

ACP_PROTOCOL_VERSION = 1
STEER_METHOD = "_reasonix.io/session/steer"


class TransportError(RuntimeError):
    pass


def resolve_native_binary() -> str:
    """Resolve the native reasonix binary, bypassing the npm shim."""
    import subprocess
    import sys

    # 1. try resolving from the reasonix package dir (nvm layout)
    pkg_roots = glob.glob(
        str(Path.home() / ".nvm/versions/node" / "*" / "lib/node_modules/reasonix")
    )
    for root in pkg_roots:
        candidates = glob.glob(str(Path(root) / "node_modules/@reasonix/cli-*/bin/reasonix"))
        if candidates:
            return candidates[-1]
    # 2. try node require from the package dir
    node = sys.executable
    if pkg_roots:
        probe = (
            "try{console.log(require.resolve('@reasonix/cli-'+process.platform+'-'+process.arch+'/bin/reasonix'))}"
            "catch(e){process.exit(1)}"
        )
        try:
            out = subprocess.run(
                [node, "-e", probe], capture_output=True, text=True, timeout=15, cwd=pkg_roots[-1]
            )
        except Exception:  # noqa: BLE001
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    # 3. fallback: any cli-* binary on the host
    candidates = glob.glob(str(Path.home() / ".nvm/versions/node" / "*" / "lib/node_modules" / "@reasonix" / "cli-*" / "bin" / "reasonix"))
    if candidates:
        return candidates[-1]
    raise TransportError("cannot resolve native reasonix binary")


class ReasonixTransport(ABC):
    name: str = "abstract"

    @abstractmethod
    async def initialize(self) -> dict: ...

    @abstractmethod
    async def new_session(self, session_config: dict) -> str: ...

    @abstractmethod
    async def prompt(self, session_id: str, text: str, *, request_id: str | None = None) -> str:
        """Run one prompt; returns the assistant text reply."""

    @abstractmethod
    async def status(self, session_id: str) -> dict: ...

    @abstractmethod
    async def close(self, session_id: str) -> None: ...

    @abstractmethod
    async def steer(self, session_id: str, instruction: str) -> dict: ...

    @abstractmethod
    async def cancel(self, session_id: str) -> dict: ...


# ---------------------------------------------------------------- stdio
class StdioReasonixTransport(ReasonixTransport):
    """Real native reasonix process over stdio JSON-RPC.

    `cwd` is the working directory for the subprocess: the project workspace
    for a real run (so the model's file operations are confined to the
    workspace), or the overlay work dir as fallback.
    """

    name = "reasonix-stdio"

    def __init__(self, overlay: Path, *, cwd: str | Path | None = None, binary: str | None = None):
        self.overlay = overlay
        self.cwd = Path(cwd) if cwd else overlay_workdir(overlay)
        self.binary = binary or resolve_native_binary()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stderr_task: asyncio.Task | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._init_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.capabilities: dict | None = None
        self._generation = 0
        self.notifications: dict[str, list] = {}  # session_id -> bounded list
        self._notif_limit = 200

    # ------------------------------------------------------------ lifecycle
    async def _start(self) -> None:
        async with self._start_lock:
            if self._proc is not None:
                return
            env = overlay_env(self.overlay)
            self.cwd.mkdir(parents=True, exist_ok=True)
            self._proc = await asyncio.create_subprocess_exec(
                self.binary,
                "acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,  # project workspace (per run) or restricted fallback
                start_new_session=True,  # own process group -> killpg reaches the native process
            )
            self._reader = self._proc.stdout
            self._writer = self._proc.stdin
            self._read_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Drain stderr so the pipe never blocks the child; log at debug."""
        assert self._proc is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.debug("reasonix stderr: %s", line.decode("utf-8", errors="replace").rstrip())

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
                    # per-session, bounded notification queue (no cross-session
                    # leakage, no unbounded growth)
                    sid = (msg.get("params") or {}).get("sessionId") or "_global"
                    bucket = self.notifications.setdefault(sid, [])
                    bucket.append(msg)
                    if len(bucket) > self._notif_limit:
                        del bucket[: len(bucket) - self._notif_limit]
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            # EOF/crash: fail all pending calls so no caller hangs forever
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TransportError("reasonix acp process exited"))
            self._pending.clear()
            self._proc = None
            self._generation += 1
            self.capabilities = None  # force re-initialize after restart

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
            raise TransportError(f"reasonix acp {method}: timeout") from exc
        except asyncio.CancelledError:
            self._pending.pop(mid, None)
            raise
        if "error" in resp:
            raise TransportError(f"reasonix acp {method}: {resp['error']}")
        return resp.get("result", {})

    # ------------------------------------------------------------ protocol
    async def initialize(self) -> dict:
        async with self._init_lock:
            if self.capabilities is not None:
                return {"agentCapabilities": self.capabilities}
            result = await self._call(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "researchd", "version": "0.1.0"},
                },
            )
            self.capabilities = result.get("agentCapabilities", {})
            return result

    async def new_session(self, session_config: dict) -> str:
        result = await self._call("session/new", {"sessionConfig": session_config})
        return result.get("sessionId") or result.get("session", {}).get("sessionId", "")

    async def prompt(self, session_id: str, text: str, *, request_id: str | None = None) -> str:
        params = {"sessionId": session_id, "prompt": text}
        if request_id:
            params["requestId"] = request_id
        result = await self._call("session/prompt", params)
        message = result.get("message", {})
        content = message.get("content", [])
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        text_out = "\n".join(parts)
        if not text_out.strip():
            # possible streaming shape: fall back to aggregated notifications
            agg = []
            bucket = self.notifications.pop(session_id, None) or []
            for n in bucket:
                p = n.get("params") or {}
                if p.get("sessionId") == session_id and p.get("message"):
                    for b in (p.get("message") or {}).get("content", []):
                        if isinstance(b, dict) and b.get("text"):
                            agg.append(b["text"])
            text_out = "\n".join(agg)
        if not text_out.strip():
            raise TransportError(
                f"reasonix acp session/prompt returned no text (result keys: {sorted(result.keys())})"
            )
        return text_out

    async def status(self, session_id: str) -> dict:
        """Session status. reasonix v1.21.2 does not implement session/status
        (verified: -32601); callers degrade to UNKNOWN and rely on session/list
        or prompt outcomes for recovery. Other errors propagate."""
        try:
            return await self._call("session/status", {"sessionId": session_id})
        except TransportError as exc:
            if "method not found" in str(exc) or "-32601" in str(exc):
                return {"sessionId": session_id, "status": "UNKNOWN"}
            raise

    async def close(self, session_id: str) -> None:
        try:
            await self._call("session/close", {"sessionId": session_id}, timeout=15.0)
        except TransportError:
            pass  # close is idempotent; the process teardown below is authoritative

    async def steer(self, session_id: str, instruction: str) -> dict:
        """Steer an in-flight session (reasonix extension, verified in Phase 0)."""
        return await self._call(
            STEER_METHOD, {"sessionId": session_id, "instruction": instruction}, timeout=30.0
        )

    async def cancel(self, session_id: str) -> dict:
        try:
            await self._call("session/close", {"sessionId": session_id}, timeout=15.0)
            return {"cancelled": True}
        except TransportError:
            return {"cancelled": False}

    async def close_all(self) -> None:
        """Terminate the process group (kills the native binary, not just a shim)."""
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
class FakeReasonixTransport(ReasonixTransport):
    """Scripted double: records the request sequence, returns scripted replies.

    Used by conformance tests to pin the exact protocol interaction the
    adapter performs (request order, sessionConfig content, repair prompts).
    """

    name = "reasonix-fake"

    def __init__(self):
        self.calls: list[dict] = []
        self.scripted_replies: dict[str, list] = {
            "initialize": [
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": {"list": {}, "resume": {}, "close": {}, "delete": {}},
                        "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                        "mcpCapabilities": {"http": True, "sse": False},
                    },
                }
            ],
            "session/prompt": [],
            "session/status": [{"sessionId": "SES-FAKE-1", "status": "RUNNING"}],
            "session/close": [{"sessionId": "SES-FAKE-1"}],
            STEER_METHOD: [{"sessionId": "SES-FAKE-1", "steered": True}],
        }
        self._seq = 0
        self._session_counter = 0
        self._initialized = None

    def script_prompt(self, *texts: str) -> None:
        """Queue assistant replies for session/prompt calls (in order)."""
        self.scripted_replies["session/prompt"].extend(
            [
                {
                    "sessionId": f"SES-FAKE-{self._session_counter}",
                    "requestId": f"REQ-{self._seq}",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": t}]},
                }
                for t in texts
            ]
        )
        self._seq += 1

    async def _next(self, method: str, params: dict) -> dict:
        self.calls.append({"method": method, "params": params})
        if method == "initialize":
            if self._initialized is None:
                self._initialized = self.scripted_replies["initialize"][0]
            return self._initialized
        if method == "session/new":
            self._session_counter += 1
            return {"sessionId": f"SES-FAKE-{self._session_counter}"}
        if method == "session/close":
            return {"sessionId": params.get("sessionId", "SES-FAKE-1")}  # idempotent
        replies = self.scripted_replies.get(method, [])
        if not replies:
            raise TransportError(f"fake transport: no scripted reply for {method}")
        return replies.pop(0)

    async def initialize(self) -> dict:
        return await self._next("initialize", {})

    async def new_session(self, session_config: dict) -> str:
        result = await self._next("session/new", {"sessionConfig": session_config})
        return result["sessionId"]

    async def prompt(self, session_id: str, text: str, *, request_id: str | None = None) -> str:
        result = await self._next("session/prompt", {"sessionId": session_id, "prompt": text, "requestId": request_id})
        return result["message"]["content"][0]["text"]

    async def status(self, session_id: str) -> dict:
        return await self._next("session/status", {"sessionId": session_id})

    async def close(self, session_id: str) -> None:
        await self._next("session/close", {"sessionId": session_id})

    async def steer(self, session_id: str, instruction: str) -> dict:
        return await self._next(STEER_METHOD, {"sessionId": session_id, "instruction": instruction})

    async def cancel(self, session_id: str) -> dict:
        await self._next("session/close", {"sessionId": session_id})
        return {"cancelled": True}
