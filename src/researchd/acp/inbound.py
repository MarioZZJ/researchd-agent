"""ACP prompt processing: deterministic commands first, then (config-gated)
constrained intent classification. Submits to `researchd service` over the
internal UDS API — never writes the database itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from ..application.commands import UnknownCommand, parse_command
from ..config import Settings
from .intents import classify_intent
from .session_config import InteractionSession


@dataclass
class PromptReply:
    text: str
    intent: str | None = None
    command: str | None = None


async def process_prompt(settings: Settings, session: InteractionSession, prompt: str, *, message_id: str | None = None) -> PromptReply:
    """Process one user prompt from cc-connect."""
    stripped = prompt.strip()
    if not stripped:
        return PromptReply(text="empty message")

    # 1. deterministic command priority (IMPLEMENTATION.md §18)
    try:
        cmd = parse_command(stripped)
    except UnknownCommand:
        cmd = None

    if cmd is not None and settings.interaction.deterministic_commands:
        # session-level commands handled right here (never touch project policy)
        if cmd.name == "bind" and cmd.args and cmd.args[0] == "project":
            return await _bind_project(settings, session, cmd)
        if cmd.name == "model" and cmd.args and cmd.args[0] == "interaction":
            return _set_interaction(session, cmd)
        return await _submit(settings, session, stripped, intent="deterministic_command", command=cmd.name, message_id=message_id)

    # 2. optional constrained intent classification (never for deterministic)
    if (
        settings.interaction.allow_natural_language_intent
        and session.interaction_profile != "deterministic"
    ):
        intent = classify_intent(stripped, profile=session.interaction_profile)
        if intent is not None and intent.confidence >= settings.interaction.intent_confidence_threshold:
            if intent.command_text:
                return await _submit(
                    settings, session, intent.command_text, intent=intent.name,
                    command=intent.command_name, message_id=message_id,
                )
            return PromptReply(text=intent.explanation, intent=intent.name)

    return PromptReply(
        text=(
            "未识别的命令。可用：/research status、/research bind project <id>、"
            "/research pause、/research resume、/research model interaction fast|deep|deterministic、"
            "/research config show、/research config set role.<role> <profile>、"
            "/decision <id> <option> --version <n>、/explain <id>、/task <id>、/claim <id>"
        )
    )


def _set_interaction(session: InteractionSession, cmd) -> PromptReply:  # noqa: ANN001
    profile = cmd.args[1]
    session.interaction_profile = profile
    return PromptReply(f"interaction profile set to {profile} (this session only)", intent="set_interaction", command="model")


async def _bind_project(settings: Settings, session: InteractionSession, cmd) -> PromptReply:  # noqa: ANN001
    project_id = cmd.args[1]
    # verify the project exists via the service before binding
    ok, detail = await _service_check(settings, project_id)
    if not ok:
        return PromptReply(text=f"bind failed: {detail}", intent="bind")
    session.cc_project = project_id
    return PromptReply(text=f"bound to project {project_id}", intent="bind", command="bind")


async def _service_check(settings: Settings, project_id: str) -> tuple[bool, str]:
    transport = httpx.AsyncHTTPTransport(uds=settings.api.socket_path) if settings.api.socket_path else None
    headers = _auth_headers(settings)
    try:
        async with httpx.AsyncClient(transport=transport, headers=headers, timeout=10.0) as client:
            resp = await client.get(f"http://localhost/v1/projects/{project_id}/status")
        if resp.status_code == 200:
            return True, ""
        return False, f"project {project_id!r} not found"
    except httpx.HTTPError as exc:
        return False, f"service unreachable: {exc}"


async def _submit(settings: Settings, session: InteractionSession, text: str, *, intent: str, command: str | None, message_id: str | None = None) -> PromptReply:
    """POST the normalized inbound message to the service internal API.

    The idempotency key is a hash of the platform session identity + prompt
    text, so retries of the same content are deduplicated and restarting the
    shim never mints new keys for old content (IMPLEMENTATION.md §25.3).
    """
    url = f"http://localhost/v1/inbound/messages"
    transport = httpx.AsyncHTTPTransport(uds=settings.api.socket_path) if settings.api.socket_path else None
    identity = session.cc_session_key or session.session_id
    digest = hashlib.sha256(f"{identity}:{session.cc_project}:{text}".encode()).hexdigest()[:32]
    message_id = f"acp-{digest}"
    headers = _auth_headers(settings)
    payload = {
        "schema": "researchd.inbound_message.v1",
        "message_id": message_id,
        "platform": "feishu",
        "cc_project": session.cc_project,
        "cc_session_key": session.cc_session_key,
        "actor": {
            "type": "human",
            "display_name": "PI",
            # identity declared by the gateway (cc-connect); 'pi' is the
            # single-PI default matching the pilot-provisioned owner
            "platform_user_id": session.cc_user_id,
        },
        "text": text,
        "attachments": [],
    }
    try:
        async with httpx.AsyncClient(transport=transport, headers=headers, timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
    except httpx.HTTPError as exc:
        return PromptReply(text=f"service unreachable: {exc}", intent=intent)
    if resp.status_code >= 400:
        return PromptReply(text=f"service error: {data.get('detail', resp.status_code)}", intent=intent)
    return PromptReply(text=data.get("reply", "ok"), intent=intent, command=command)


def _auth_headers(settings: Settings) -> dict:
    # mutating endpoints require the bearer token on EVERY transport
    # (threat model T4 / B-08); the shim is a trusted gateway that reads
    # the same 0600 env file
    return {"Authorization": f"Bearer {settings.api.token}"} if settings.api.token else {}
