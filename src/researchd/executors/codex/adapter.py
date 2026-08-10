"""CodexAdapter: codex app-server (0.146.0) as an executor
(IMPLEMENTATION.md §12.4, §23 Phase 5).

- Runs `codex app-server --listen stdio://` with a whitelisted environment and
  a restricted working directory.
- Thread/turn lifecycle: thread/start -> turn/start (input + optional
  model/effort + outputSchema constraint) -> wait for TurnCompletedNotification
  -> extract the assistant's JSON -> local schema validation -> targeted
  repair via turn/steer (expectedTurnId precondition).
- steer/interrupt pass through to turn/steer and turn/interrupt.
- Capabilities: structured output via the protocol-level outputSchema AND
  local validation; sessions are per-turn threads (closed after the turn;
  thread ids recorded on the Run for recovery).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..base import (
    AuditResult,
    ExecutorAdapter,
    ExecutorCapabilities,
    ExecutorSessionInfo,
    PlannerResult,
    ValidationFailure,
    WorkResult,
    validate_audit_result,
    validate_planner_result,
    validate_work_result,
)
from ..reasonix.adapter import extract_json, sanitize_validation_error, load_schema
from .overlay import ensure_codex_home
from .transport import CodexTransport, FakeCodexTransport, StdioCodexTransport, TransportError

MAX_REPAIRS = 2

PROMPT_TEMPLATE = """\
你是 {role}。严格按以下任务执行，最后只输出一个 JSON 文档（不要输出其他文字）。

# 任务
{objective}

# 上下文（只使用与你任务相关的部分）
{context}

# 输出要求
- 只输出 JSON，放在 ```json 代码块中；
- 必须符合 schema {schema}；
- 不得编造证据或产物；不存在的文件不得引用；
- 不确定处写入 limitations/issues，不得臆测。
"""


def assistant_text_from_turn(turn: dict) -> str:
    """Extract the final assistant text from a completed turn's items.

    v2 wire shape (verified from the 0.146.0 schema): assistant messages are
    {"type": "agentMessage", "text": ..., "id": ...} — the text is TOP-LEVEL,
    not nested in role/content blocks.
    """
    parts: list[str] = []
    for item in turn.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agentMessage" and item.get("text"):
            parts.append(item["text"])
    return "\n".join(parts)


class CodexAdapter(ExecutorAdapter):
    name = "codex"
    capabilities = ExecutorCapabilities(
        supports_session_new=True,  # thread/start per run
        supports_session_load=True,  # thread/read + thread/resume exist in v2
        supports_session_resume=True,  # thread/resume
        supports_steering=True,
        supports_cancel=True,  # turn/interrupt
        supports_structured_output=True,  # outputSchema + local validation
        supports_model_override=True,
        supports_reasoning_override=True,
        supports_tool_approval=False,  # researchd gates approvals itself (approvalPolicy=never)
    )

    def __init__(self, settings=None, *, transport: CodexTransport | None = None, workdir: str | None = None):
        self.settings = settings
        self.on_session_started = None
        if transport is not None:
            self.transport = transport
        else:
            base = (Path(settings.data_dir) if settings else Path(".data")).resolve()
            work = workdir or str(base / "codex-work")
            Path(work).mkdir(parents=True, exist_ok=True)
            codex_home = str(ensure_codex_home(base).resolve())
            self.transport = StdioCodexTransport(workdir=work, codex_home=codex_home)

    # ------------------------------------------------------------ helpers
    def _thread_params(self, profile: dict, cwd: str | None) -> dict:
        params: dict = {
            "cwd": cwd,
            "approvalPolicy": "never",  # researchd gates approvals; no interactive prompts
        }
        model = profile.get("model")
        if model:
            params["model"] = model
        return params

    def _turn_params(self, thread_id: str, text: str, profile: dict, schema: dict | None) -> dict:
        params: dict = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        model = profile.get("model")
        if model:
            params["model"] = model
        effort = profile.get("reasoning_effort")
        if effort:
            params["effort"] = effort
        if schema is not None:
            params["outputSchema"] = schema  # protocol-level constraint
        return params

    def _prompt(self, role: str, context: dict, schema_name: str) -> str:
        return PROMPT_TEMPLATE.format(
            role=role,
            objective=context.get("objective", ""),
            context=json.dumps(context.get("package", context), ensure_ascii=False, indent=1)[:20000],
            schema=schema_name,
        )

    async def _run_structured(
        self,
        *,
        role: str,
        context: dict,
        profile: dict,
        schema_name: str,
        schema: dict,
        validator,
    ) -> tuple[Any, ExecutorSessionInfo]:
        await self.transport.initialize()
        thread_result = await self.transport.thread_start(self._thread_params(profile, context.get("cwd")))
        thread_id = thread_result.get("thread", {}).get("id") or thread_result.get("threadId", "")
        session_info = ExecutorSessionInfo(
            executor=self.name,
            session_id=thread_id,
        )
        if not thread_id:
            raise TransportError("codex thread/start returned no thread id")
        try:
            if self.on_session_started is not None:
                self.on_session_started(thread_id)
            prompt = self._prompt(role, context, schema_name)
            turn_result = await self.transport.turn_start(self._turn_params(thread_id, prompt, profile, schema))
            turn_id = turn_result.get("turn", {}).get("id", "")
            session_info.turn_id = turn_id
            completion = await self.transport.wait_for_turn(thread_id, turn_id, timeout=600.0)
            status = completion.get("status", "")
            if status in ("failed", "interrupted"):
                err = (completion.get("error") or {}).get("message", status)
                raise TransportError(f"codex turn {turn_id} {status}: {err}")
            text = assistant_text_from_turn(completion)
            error: str | None = None
            for attempt in range(MAX_REPAIRS + 1):
                try:
                    raw = extract_json(text)
                    return validator(raw), session_info
                except (ValueError, json.JSONDecodeError, ValidationFailure) as exc:
                    error = str(exc)
                    if isinstance(exc, ValidationFailure):
                        import pydantic

                        error = (
                            sanitize_validation_error(exc.__cause__)
                            if isinstance(exc.__cause__, pydantic.ValidationError)
                            else error
                        )
                    if attempt >= MAX_REPAIRS:
                        break
                    repair = (
                        f"你的上一个输出无法解析为符合 {schema_name} 的 JSON：{error}。"
                        f"请只输出修正后的完整 JSON 文档（```json 代码块），不要解释。"
                    )
                    # steer only appends to a RUNNING turn; a completed turn is
                    # repaired by starting a NEW turn on the same thread
                    turn_result = await self.transport.turn_start(
                        self._turn_params(thread_id, repair, profile, None)
                    )
                    turn_id = turn_result.get("turn", {}).get("id", "")
                    if not turn_id:
                        raise TransportError("codex turn/start for repair returned no turn id")
                    session_info.turn_id = turn_id
                    completion = await self.transport.wait_for_turn(thread_id, turn_id, timeout=600.0)
                    if completion.get("status") in ("failed", "interrupted"):
                        err = (completion.get("error") or {}).get("message", completion.get("status"))
                        raise TransportError(f"codex repair turn {turn_id} {completion.get('status')}: {err}")
                    text = assistant_text_from_turn(completion)
            raise TransportError(f"structured output failed after {MAX_REPAIRS} repairs: {error}")
        finally:
            await self.transport.thread_close(thread_id)

    # ------------------------------------------------------------ ExecutorAdapter
    async def run_planner(self, context: dict, *, profile: dict) -> tuple[PlannerResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="研究规划者",
            context=context,
            profile=profile,
            schema_name="researchd.planner_result.v1",
            schema=load_schema("planner_result.json"),
            validator=validate_planner_result,
        )

    async def run_worker(self, context: dict, *, profile: dict) -> tuple[WorkResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="科研工作者",
            context=context,
            profile=profile,
            schema_name="researchd.work_result.v1",
            schema=load_schema("work_result.json"),
            validator=validate_work_result,
        )

    async def run_auditor(self, context: dict, *, profile: dict) -> tuple[AuditResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="科研审计者",
            context=context,
            profile=profile,
            schema_name="researchd.audit_result.v1",
            schema=load_schema("audit_result.json"),
            validator=validate_audit_result,
        )

    # ------------------------------------------------------------ control
    async def steer(self, thread_id: str, instruction: str, turn_id: str) -> dict:
        """Steer a RUNNING turn (v2 requires expectedTurnId; steering a
        completed turn is invalid)."""
        return await self.transport.turn_steer(
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": instruction}],
            }
        )

    async def cancel(self, thread_id: str, turn_id: str | None = None) -> dict:
        if turn_id:
            return await self.transport.turn_interrupt({"threadId": thread_id, "turnId": turn_id})
        return {"interrupted": False}

    async def close(self) -> None:
        if isinstance(self.transport, StdioCodexTransport):
            await self.transport.close_all()
