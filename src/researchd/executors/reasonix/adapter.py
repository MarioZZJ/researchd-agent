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
from pathlib import Path
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
from .overlay import OverlayError, ensure_overlay, installed_skills, overlay_workdir
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
        self._overlay_dir = overlay_dir or (settings.data_dir if settings else ".data")
        self._skills: list[str] = []
        self._transports: dict[str, ReasonixTransport] = {}
        if transport is not None:
            # explicit transport (tests): single shared instance, no overlay
            self._transports["<explicit>"] = transport
            self._explicit = True
        else:
            self._explicit = False

    # ------------------------------------------------------------ overlay
    @property
    def installed_skills(self) -> list[str]:
        """Skills actually mounted in the overlay (recorded on the Run)."""
        return list(self._skills)

    def _transport_for(self, workspace_root: str | None) -> ReasonixTransport:
        """Per-workspace transport: each project workspace gets its own
        reasonix process cwd; the fallback is the restricted overlay work dir.
        A workspace is keyed by its RESOLVED path so two runs of the same
        project share one process and different projects never share cwd."""
        if self._explicit:
            return self._transports["<explicit>"]
        if workspace_root:
            resolved = Path(workspace_root).resolve()
            if not resolved.is_dir():
                raise OverlayError(
                    f"workspace root {workspace_root!r} does not exist; refusing to create "
                    "arbitrary directories for executor cwd (fail-closed)"
                )
            key = str(resolved)
        else:
            key = "<fallback>"
        if key not in self._transports:
            overlay = ensure_overlay(self._overlay_dir)
            self._skills = installed_skills(overlay)
            cwd = Path(key) if workspace_root else overlay_workdir(overlay)
            self._transports[key] = StdioReasonixTransport(overlay, cwd=cwd)
        return self._transports[key]

    # ------------------------------------------------------------ protocol
    async def _ensure_initialize(self) -> None:
        # transport.initialize() is idempotent internally and resets its
        # capability cache when the process restarts (generation bump), so we
        # always delegate to it. Initializes ALL live transports (per
        # workspace) so a later run never pays a cold start.
        for transport in self._transports.values():
            await transport.initialize()

    def _transport_for_run(self, context: dict) -> ReasonixTransport:
        """Transport whose subprocess cwd is the run's project workspace
        (from the persisted ContextPackage). A REAL run without a workspace
        fails closed — the overlay work dir is never used as a run cwd
        (IMPLEMENTATION.md §15.2: workspace-confined means confined)."""
        if self._explicit:
            return self._transports["<explicit>"]
        workspace = (context or {}).get("workspace_root")
        if not workspace:
            raise OverlayError(
                "no workspace_root in context; refusing to run the executor "
                "in the overlay work dir (fail-closed: real runs need a "
                "project workspace)"
            )
        return self._transport_for(workspace)

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
        transport = self._transport_for_run(context)
        await transport.initialize()
        session_id = await transport.new_session(self._session_config(profile))
        session_info = ExecutorSessionInfo(
            executor=self.name,
            session_id=session_id,
        )
        # persist session provenance as early as possible (recovery signal)
        if self.on_session_started is not None:
            self.on_session_started(session_id)
        try:
            prompt = self._prompt(role, context, schema_name)
            text = await transport.prompt(session_id, prompt)
            for attempt in range(MAX_REPAIRS + 1):
                try:
                    raw = extract_json(text)
                    # transcript path (path ONLY — content stays in the
                    # executor's run dir, never in the DB) becomes part of
                    # the session provenance for the scheduler to persist
                    session_info.transcript_path = transport.last_transcript(session_id)
                    # controlled runtime receipt: the model already produced
                    # the full structured result — if we crash before
                    # collect, recovery can finish WITHOUT calling the model
                    # again (see reconcile_orphans receipt recovery)
                    self._write_receipt(context, raw, session_info)
                    return validator(raw), session_info
                except (ValueError, json.JSONDecodeError, ValidationFailure) as exc:
                    self._clear_receipt(context)
                    error = str(exc)
                    if isinstance(exc, ValidationFailure):
                        error = sanitize_validation_error(exc.__cause__) if isinstance(exc.__cause__, ValidationError) else error
                    if attempt >= MAX_REPAIRS:
                        break
                    repair = (
                        f"你的上一个输出无法解析为符合 {schema_name} 的 JSON：{error}。"
                        f"请只输出修正后的完整 JSON 文档（```json 代码块），不要解释。"
                    )
                    text = await transport.prompt(session_id, repair)
            self._clear_receipt(context)
            raise TransportError(f"structured output failed after {MAX_REPAIRS} repairs: {error}")
        finally:
            # sessions are closed on every path (success, failure, cancel)
            await transport.close(session_id)

    # ------------------------------------------------------------ runtime receipts
    def _receipt_path(self, context: dict) -> Path | None:
        run_id = (context or {}).get("run_id")
        if not run_id:
            return None
        return Path(self._overlay_dir).resolve() / "receipts" / f"{run_id}.json"

    def _write_receipt(self, context: dict, raw: dict, session_info: ExecutorSessionInfo) -> None:
        """Durable completion receipt (0600) so a crash between model output
        and collect never re-invokes the model. Contains the structured JSON +
        provenance, NOT the transcript text."""
        path = self._receipt_path(context)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "role": context.get("role", "worker"),
            "run_id": context.get("run_id"),
            "session_id": session_info.session_id,
            "transcript_path": session_info.transcript_path,
            "extracted_at": __import__("researchd.domain.base", fromlist=["utcnow"]).utcnow(),
            "raw": raw,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(path)  # atomic

    def _clear_receipt(self, context: dict) -> None:
        path = self._receipt_path(context)
        if path is not None and path.exists():
            path.unlink()

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
    def last_transcript(self, session_id: str) -> str | None:
        """Path of the persisted transcript for a session (path only)."""
        if isinstance(self._transport, StdioReasonixTransport):
            return self._transport.last_transcript(session_id)
        return None

    async def steer(self, session_id: str, instruction: str) -> dict:
        for transport in self._transports.values():
            try:
                return await transport.steer(session_id, instruction)
            except TransportError:
                continue
        raise TransportError(f"session {session_id} not found on any transport")

    async def cancel(self, session_id: str) -> dict:
        for transport in self._transports.values():
            try:
                return await transport.cancel(session_id)
            except TransportError:
                continue
        return {"cancelled": False}

    async def close(self) -> None:
        for transport in self._transports.values():
            if isinstance(transport, StdioReasonixTransport):
                await transport.close_all()
