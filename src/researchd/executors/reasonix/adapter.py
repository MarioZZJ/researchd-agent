"""ReasonixAdapter: reasonix (v1.21.2) as an executor via ACP
(IMPLEMENTATION.md §12.4, §15.2, §23 Phase 4).

- Runs reasonix with an isolated REASONIX_HOME overlay (never touches
  ~/.reasonix; api keys stay in the 0600 overlay file inside the data dir).
- Structured output: the prompt embeds the actual JSON Schema; the result is
  validated locally, and up to MAX_REPAIRS targeted repair turns fix schema
  failures. Validation errors sent back to the model are SANITIZED (field
  location + error type only — never raw input values).
- Session/turn provenance is returned on ExecutorSessionInfo (session_id) for
  the scheduler to persist on the Run.
- Steering (`_reasonix.io/session/steer`) and cancel are available.
- Capabilities reflect what the transport actually implements: load/resume
  are NOT implemented by v1.21.2's ACP surface and are declared false.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

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
from .overlay import ensure_overlay
from .transport import FakeReasonixTransport, ReasonixTransport, StdioReasonixTransport, TransportError

MAX_REPAIRS = 2
SCHEMA_DIR = None  # resolved lazily


def load_schema(name: str) -> dict:
    from pathlib import Path

    schemas = Path(__file__).resolve().parent.parent / "schemas"
    return json.loads((schemas / name).read_text())


def extract_json(text: str) -> dict:
    """Extract the JSON document from an assistant reply.

    Tries, in order: a fenced ```json block, the whole reply, then any
    balanced JSON document found via JSONDecoder.raw_decode scanning. Multiple
    documents or trailing garbage are rejected.
    """
    import re

    from json import JSONDecoder

    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # raw_decode scanning: find the first complete JSON document
    decoder = JSONDecoder()
    idx = 0
    while True:
        idx = text.find("{", idx)
        if idx < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        rest = text[end:].strip()
        if rest:
            # never echo the raw reply fragment anywhere
            raise ValueError("trailing content after JSON document")
        return obj
    raise ValueError("no JSON document found in executor reply")


def sanitize_validation_error(exc: ValidationError) -> str:
    """Field location + error type only; raw input values are never echoed
    back to the model or written to logs (IMPLEMENTATION.md §22)."""
    parts = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", []))
        parts.append(f"{loc}:{e.get('type', 'invalid')}")
    return "; ".join(parts) if parts else str(exc)


PROMPT_TEMPLATE = """\
你是 {role}。严格按以下任务执行，最后只输出一个 JSON 文档（不要输出其他文字）。

# 任务
{objective}

# 上下文（只使用与你任务相关的部分）
{context}

# 输出要求
- 只输出 JSON，放在 ```json 代码块中；
- 必须符合以下 JSON Schema：
```json
{schema}
```
- 不得编造证据或产物；不存在的文件不得引用；
- 不确定处写入 limitations/issues，不得臆测。
"""


class ReasonixAdapter(ExecutorAdapter):
    name = "reasonix"
    capabilities = ExecutorCapabilities(
        supports_session_new=True,
        supports_session_load=False,  # not implemented by reasonix v1.21.2 ACP
        supports_session_resume=False,  # not implemented by reasonix v1.21.2 ACP
        supports_steering=True,
        supports_cancel=True,
        supports_structured_output=True,
        supports_model_override=True,
        supports_reasoning_override=True,
        supports_tool_approval=False,
    )

    def __init__(self, settings=None, *, transport: ReasonixTransport | None = None, overlay_dir: str | None = None):
        self.settings = settings
        self.on_session_started = None  # callback(session_id) for early provenance
        if transport is not None:
            self.transport = transport
        else:
            overlay = ensure_overlay(overlay_dir or (settings.data_dir if settings else ".data"))
            self.transport = StdioReasonixTransport(overlay)

    # ------------------------------------------------------------ protocol
    async def _ensure_initialize(self) -> None:
        # transport.initialize() is idempotent internally and resets its
        # capability cache when the process restarts (generation bump), so we
        # always delegate to it.
        await self.transport.initialize()

    def _session_config(self, profile: dict) -> dict:
        cfg: dict = {}
        model = profile.get("model")
        if model:
            cfg["model"] = model
        effort = profile.get("reasoning_effort")
        if effort:
            cfg["reasoningEffort"] = effort
        return cfg

    def _prompt(self, role: str, context: dict, schema_name: str) -> str:
        schema_text = json.dumps(load_schema(schema_name), ensure_ascii=False)[:12000]
        return PROMPT_TEMPLATE.format(
            role=role,
            objective=context.get("objective", ""),
            context=json.dumps(context.get("package", context), ensure_ascii=False, indent=1)[:20000],
            schema=schema_text,
        )

    async def _run_structured(
        self,
        *,
        role: str,
        context: dict,
        profile: dict,
        schema_name: str,
        validator,
    ) -> tuple[Any, ExecutorSessionInfo]:
        await self._ensure_initialize()
        session_id = await self.transport.new_session(self._session_config(profile))
        session_info = ExecutorSessionInfo(
            executor=self.name,
            session_id=session_id,
        )
        # persist session provenance as early as possible (recovery signal)
        if self.on_session_started is not None:
            self.on_session_started(session_id)
        try:
            prompt = self._prompt(role, context, schema_name)
            text = await self.transport.prompt(session_id, prompt)
            for attempt in range(MAX_REPAIRS + 1):
                try:
                    raw = extract_json(text)
                    return validator(raw), session_info
                except (ValueError, json.JSONDecodeError, ValidationFailure) as exc:
                    error = str(exc)
                    if isinstance(exc, ValidationFailure):
                        error = sanitize_validation_error(exc.__cause__) if isinstance(exc.__cause__, ValidationError) else error
                    if attempt >= MAX_REPAIRS:
                        break
                    repair = (
                        f"你的上一个输出无法解析为符合 {schema_name} 的 JSON：{error}。"
                        f"请只输出修正后的完整 JSON 文档（```json 代码块），不要解释。"
                    )
                    text = await self.transport.prompt(session_id, repair)
            raise TransportError(f"structured output failed after {MAX_REPAIRS} repairs: {error}")
        finally:
            # sessions are closed on every path (success, failure, cancel)
            await self.transport.close(session_id)

    # ------------------------------------------------------------ ExecutorAdapter
    async def run_planner(self, context: dict, *, profile: dict) -> tuple[PlannerResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="研究规划者",
            context=context,
            profile=profile,
            schema_name="planner_result.json",
            validator=validate_planner_result,
        )

    async def run_worker(self, context: dict, *, profile: dict) -> tuple[WorkResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="科研工作者",
            context=context,
            profile=profile,
            schema_name="work_result.json",
            validator=validate_work_result,
        )

    async def run_auditor(self, context: dict, *, profile: dict) -> tuple[AuditResult, ExecutorSessionInfo]:
        return await self._run_structured(
            role="科研审计者",
            context=context,
            profile=profile,
            schema_name="audit_result.json",
            validator=validate_audit_result,
        )

    # ------------------------------------------------------------ control
    async def steer(self, session_id: str, instruction: str) -> dict:
        return await self.transport.steer(session_id, instruction)

    async def cancel(self, session_id: str) -> dict:
        return await self.transport.cancel(session_id)

    async def close(self) -> None:
        if isinstance(self.transport, StdioReasonixTransport):
            await self.transport.close_all()
