"""ACP inbound shim (`researchd acp`, IMPLEMENTATION.md §3.2).

cc-connect speaks ACP to this process. It:
- resolves deterministic commands;
- (when enabled) runs a constrained interaction-profile intent classification;
- submits normalized events to `researchd service` over the internal API;
- returns acknowledgements, query results, or short explanations.

It never holds authoritative project state, never runs long research tasks,
and never writes the database directly.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ..config import Settings
from ..application.commands import UnknownCommand, parse_command
from .session_config import InteractionSession

ACP_PROTOCOL_VERSION = 1


class AcpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AcpServer:
    """Minimal ACP (Agent Client Protocol) server over stdio.

    Methods: initialize, session/new, session/prompt, session/close,
    session/list. Prompt text is first parsed as a deterministic command;
    unrecognized input may go through the interaction profile (config-gated).
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, InteractionSession] = {}
        self._seq = 0

    # ------------------------------------------------------------ protocol
    async def handle(self, msg: dict) -> dict | None:
        # JSON-RPC request validation: must be an object with jsonrpc: "2.0"
        # and a method. Notifications (no id) get no response.
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            mid = msg.get("id") if isinstance(msg, dict) else None
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32600, "message": "invalid request"}}
        method = msg.get("method")
        params = msg.get("params") or {}
        mid = msg.get("id")
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                return {"jsonrpc": "2.0", "id": mid, "result": self._initialize()}
            if method == "notifications/initialized":
                return None
            if method == "session/new":
                return {"jsonrpc": "2.0", "id": mid, "result": self._session_new(params)}
            if method == "session/prompt":
                result = await self._session_prompt(params)
                return {"jsonrpc": "2.0", "id": mid, "result": result}
            if method == "session/close":
                return {"jsonrpc": "2.0", "id": mid, "result": self._session_close(params)}
            if method == "session/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"sessions": list(self.sessions.keys())}}
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except AcpError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}

    def _initialize(self) -> dict:
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "sessionCapabilities": {"new": {}, "close": {}, "list": {}},
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                "mcpCapabilities": {},
            },
            "configOptions": {
                "interaction_profile": {
                    "type": "string",
                    "enum": ["fast", "deep", "deterministic"],
                    "default": self.settings.interaction.default_profile.split("_")[-1],
                    "description": "session interaction profile (UI level only; never changes project policy)",
                }
            },
        }

    def _session_new(self, params: dict) -> dict:
        config = params.get("sessionConfig") or {}
        profile = config.get("interaction_profile")
        if profile not in (None, "fast", "deep", "deterministic"):
            raise AcpError(-32602, f"invalid interaction_profile {profile!r}")
        # identity is REQUIRED and must come from cc-connect (env injection
        # CC_PROJECT/CC_SESSION_KEY/CC_USER_ID, or explicit sessionConfig).
        # Fail closed: an anonymous session can never be mapped to the PI,
        # and unknown users must not be auto-mapped to anything.
        identity = self._resolve_identity(config)
        if identity["cc_project"] is None or identity["cc_session_key"] is None or identity["cc_user_id"] is None:
            raise AcpError(
                -32602,
                "cc-connect identity missing: sessionConfig (cc_project/cc_session_key/"
                "cc_user_id) or env (CC_PROJECT/CC_SESSION_KEY/CC_USER_ID) must be provided; "
                "refusing anonymous session (never auto-mapping unknown users to PI)",
            )
        self._seq += 1
        session = InteractionSession(
            session_id=f"SES-{self._seq:04d}",
            interaction_profile=profile or "fast",
            cc_project=identity["cc_project"],
            cc_session_key=identity["cc_session_key"],
            cc_user_id=identity["cc_user_id"],
        )
        self.sessions[session.session_id] = session
        return {"sessionId": session.session_id, "sessionConfig": config}

    @staticmethod
    def _resolve_identity(config: dict) -> dict:
        """Identity is ATOMIC: either the caller provides all three fields in
        sessionConfig, or all three come from the cc-connect env injection
        (CC_PROJECT/CC_SESSION_KEY/CC_USER_ID). Mixing (e.g. overriding only
        cc_user_id while inheriting the env project) is rejected so an
        identity can never be half-spoofed."""
        import os

        configured = {k: config.get(k) for k in ("cc_project", "cc_session_key", "cc_user_id")}
        provided = [v for v in configured.values() if v]
        if provided:
            if len(provided) != 3:
                return {"cc_project": None, "cc_session_key": None, "cc_user_id": None}
            return {k: str(v) for k, v in configured.items()}
        env = {
            "cc_project": os.environ.get("CC_PROJECT"),
            "cc_session_key": os.environ.get("CC_SESSION_KEY"),
            "cc_user_id": os.environ.get("CC_USER_ID"),
        }
        return {k: (v if v else None) for k, v in env.items()}

    async def _session_prompt(self, params: dict) -> dict:
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id)
        if session is None:
            raise AcpError(-32602, f"unknown session {session_id!r}")
        prompt = params.get("prompt", "")
        if isinstance(prompt, list):  # content blocks
            prompt = " ".join(b.get("text", "") for b in prompt if isinstance(b, dict))
        from .inbound import process_prompt

        reply = await process_prompt(self.settings, session, prompt)
        return {
            "sessionId": session.session_id,
            "requestId": f"REQ-{session.request_counter()}",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": reply.text}],
            },
        }

    def _session_close(self, params: dict) -> dict:
        session_id = params.get("sessionId")
        self.sessions.pop(session_id, None)
        return {"sessionId": session_id}


async def run_acp_stdio(settings: Settings) -> None:
    """Read JSON-RPC lines from stdin, write responses to stdout."""
    server = AcpServer(settings)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(), sys.stdout  # type: ignore[arg-type]
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)

    while True:
        line = await reader.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # JSON-RPC parse error (-32700); no request id available
            writer.write((json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n").encode())
            await writer.drain()
            continue
        if not isinstance(msg, dict):
            writer.write((json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}) + "\n").encode())
            await writer.drain()
            continue
        is_notification = "id" not in msg
        response = await server.handle(msg)
        if response is not None and not is_notification:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
