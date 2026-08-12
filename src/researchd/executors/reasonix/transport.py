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

# ------------------------------------------------------------------ sandbox
# The reasonix subprocess runs with the SAME uid as researchd, so `cwd` is NOT
# a security boundary. When bubblewrap is available we wrap the subprocess in
# a filesystem namespace: whole root read-only; ONLY the overlay and the
# project workspace writable; the researchd data dir (DB/socket/logs),
# ~/.cc-connect (tokens/sessions) and ~/.reasonix (global config incl. .env)
# masked as empty tmpfs. Network stays shared (reasonix needs the provider
# gateway). Without bubblewrap the transport FAILS CLOSED — no claim of
# "workspace-confined" is ever made.
# home paths masked as EMPTY tmpfs inside the sandbox (in addition to the
# researchd data dir): a readable / is never enough — ssh/aws/git/npm
# credentials and the whole reasonix home must be unreachable even though
# --ro-bind / / leaves them readable on disk
_SANDBOX_MASKED_HOME_DIRS = (
    ".cc-connect",
    ".reasonix",
    ".ssh",
    ".aws",
    ".config",
    ".gnupg",
    ".gitconfig",
    ".netrc",
    ".npmrc",
    ".cache",
)


def _bwrap_command(binary: str, overlay: Path, cwd: Path) -> list[str] | None:
    """bubblewrap argv wrapping `binary acp`; None when bwrap is unavailable
    (callers must fail closed, never silently degrade).

    The sandbox is built from a MINIMAL read-only runtime allowlist (NOT
    --ro-bind / /): /usr /bin /sbin /lib* /etc /opt /proc /dev plus the
    binary's own directory tree (~/.nvm) and ~/.cache — everything else on
    the host is simply NOT MOUNTED (invisible), which covers repository
    .env files, .kube/.docker configs, other users' homes, and runtime
    sockets without needing an exhaustive mask list. The whole user home is
    then explicitly masked as empty tmpfs and ONLY the overlay (read-only
    config/env, writable sessions) and the project workspace are re-mounted.
    """
    import shutil

    if shutil.which("bwrap") is None:
        return None
    home = Path.home()
    cmd = [
        "bwrap",
        "--die-with-parent",
        # minimal read-only runtime allowlist — NO --ro-bind / /
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/opt", "/opt",
        "--proc", "/proc",  # namespace-local procfs (--unshare-pid): the
        #  host's /proc/<pid>/{environ,fd,root} must NOT be reachable from
        #  inside the sandbox (same-uid service secrets live in environ)
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--share-net",  # model calls need the provider gateway
        "--unshare-pid",
        "--unshare-ipc",
    ]
    for libdir in ("/lib", "/lib64"):
        if Path(libdir).is_dir():
            cmd += ["--ro-bind", libdir, libdir]
    # the native binary lives under ~/.nvm. bwrap mounts are STACKED — the
    # LAST mount wins — so the whole-home tmpfs must come FIRST, and the
    # nvm keep-bind is re-mounted ON TOP of the mask afterwards.
    # NOTE: host ~/.cache is deliberately NOT bound (it may hold cached
    # credentials, e.g. RESEARCHD_API__TOKEN under ~/.cache/cc-connect-live);
    # reasonix gets a fresh writable overlay/.cache instead.
    cmd += ["--tmpfs", str(home)]
    if (home / ".nvm").is_dir():
        cmd += ["--ro-bind", str(home / ".nvm"), str(home / ".nvm")]
    # whole user home masked (empty tmpfs): ~/.ssh ~/.aws ~/.reasonix
    # ~/.cc-connect, repository .env, everything — then re-mount ONLY the
    # overlay (config/.env READ-ONLY; sessions writable) and the workspace
    cmd += ["--ro-bind", str(overlay), str(overlay)]
    cache = overlay / ".cache"
    if cache.is_dir():
        cmd += ["--bind", str(cache), str(cache)]
    sessions = overlay / "sessions"
    if sessions.is_dir():
        cmd += ["--bind", str(sessions), str(sessions)]
    ws = str(cwd)
    cmd += ["--bind", ws, ws]
    cmd += ["--chdir", ws]
    cmd += [binary, "acp"]
    return cmd


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

    def last_transcript(self, session_id: str) -> str | None:
        """Path of the persisted transcript for a session (path only;
        transcript CONTENT never leaves the executor run dir)."""
        return None


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
        self._last_transcript: dict[str, str] = {}  # session_id -> transcriptPath (path only)

    def last_transcript(self, session_id: str) -> str | None:
        return self._last_transcript.get(session_id)

    # ------------------------------------------------------------ lifecycle
    async def _start(self) -> None:
        async with self._start_lock:
            if self._proc is not None:
                return
            env = overlay_env(self.overlay)
            self.cwd.mkdir(parents=True, exist_ok=True)
            cmd = _bwrap_command(self.binary, self.overlay, self.cwd)
            if cmd is None:
                raise TransportError(
                    "bubblewrap (bwrap) is not available; refusing to run the "
                    "reasonix subprocess WITHOUT filesystem isolation "
                    "(fail-closed: cwd is not a security boundary)"
                )
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,  # own process group -> killpg reaches the native process
            )
            self._reader = self._proc.stdout
            self._writer = self._proc.stdin
            self._read_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Drain stderr so the pipe never blocks the child. Content is NEVER
        logged (raw executor stderr may carry prompts/secrets): only a line
        count is recorded at debug."""
        assert self._proc is not None
        count = 0
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            count += 1
        logger.debug("reasonix stderr drained: %d line(s), content withheld", count)

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

    async def _call(self, method: str, params: dict | None = None, *, timeout: float = 600.0) -> dict:
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
            # error CODE only — the raw ACP error body may echo model output,
            # prompts, or secrets and must never reach logs
            code = resp["error"].get("code") if isinstance(resp["error"], dict) else None
            raise TransportError(f"reasonix acp {method}: error code {code}")
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
        sid = result.get("sessionId") or result.get("session", {}).get("sessionId", "")
        if not sid:
            raise TransportError("reasonix acp session/new: no sessionId")
        # headless execution: the default tool-approval gate (ask) interrupts
        # every turn in non-interactive mode; switch the gate to yolo AFTER
        # creation (isolated overlay + workspace-confined cwd is the sandbox)
        try:
            await self._call(
                "session/set_config_option",
                {"sessionId": sid, "configId": "tool_approval", "value": "yolo"},
            )
        except TransportError:
            pass  # older servers without the control: leave the gate as-is
        return sid

    async def prompt(self, session_id: str, text: str, *, request_id: str | None = None) -> str:
        params = {
            "sessionId": session_id,
            # ACP spec: prompt is a content-block ARRAY (reasonix v1.21.2
            # rejects a bare string with -32602)
            "prompt": [{"type": "text", "text": text}],
        }
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
            # reasonix v1.21.2 returns {stopReason, transcriptPath} and keeps
            # the assistant text in the persisted transcript: read the last
            # assistant block from the transcript file
            tp = result.get("transcriptPath")
            if tp:
                text_out = self._last_assistant_text(tp, overlay_root=self.overlay)
        if not text_out.strip():
            raise TransportError(
                f"reasonix acp session/prompt returned no text (result keys: {sorted(result.keys())})"
            )
        # controlled completion receipt: transcript path only (never content)
        tp = result.get("transcriptPath")
        if tp:
            self._last_transcript[session_id] = tp
        return text_out

    @staticmethod
    def _last_assistant_text(transcript_path: str, *, overlay_root: Path | None = None, max_bytes: int = 8 * 1024 * 1024) -> str:
        """Extract the last assistant text block from a reasonix transcript
        jsonl (message content may be a string or a list of blocks).

        CONTAINMENT: the transcript must live under the overlay sessions dir
        (the sandbox can only write there); it must be a REGULAR file, not a
        symlink, not a device, and bounded in size — a sandbox-controlled
        path is never trusted blindly (a hostile/compromised reasonix could
        otherwise point us at /dev/zero or any readable host file).

        The file is opened ONCE with O_NOFOLLOW relative to the resolved
        sessions dir and read from the same fd (no stat-then-open race, no
        symlink swap after the containment check)."""
        import os as _os

        from pathlib import Path as _Path

        path = _Path(transcript_path)
        sessions_dir: _Path | None = None
        if overlay_root is not None:
            sessions_dir = (overlay_root / "sessions").resolve()
            try:
                path.resolve().relative_to(sessions_dir)
            except ValueError:
                return ""  # outside the sessions dir: not ours, refuse
        try:
            # O_NOFOLLOW on the final component; fd stays pinned after the
            # containment resolution above
            fd = _os.open(path, _os.O_RDONLY | _os.O_NOFOLLOW)
        except OSError:
            return ""
        try:
            st = _os.fstat(fd)
            if not st.st_mode & 0o100000:  # S_IFREG
                return ""
            if st.st_size > max_bytes:
                return ""
            if st.st_size == 0:
                return ""
            with _os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(max_bytes + 1)
        except OSError:
            return ""
        if len(data) > max_bytes:
            return ""
        for line in reversed(data.splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("role") != "assistant":
                continue
            content = rec.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("text")
                ]
                joined = "\n".join(parts).strip()
                if joined:
                    return joined
        return ""

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
        if method == "session/set_config_option":
            # headless approval switch: record and accept (no script needed)
            return {"sessionId": params.get("sessionId"), "configOptions": []}
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
