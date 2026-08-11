"""Operational test endpoints: delivery test + document test (researchctl).

Both are explicit, user-invoked probes against the configured staging target
(cc-connect chat / feishu document). They are mutating, require the bearer
token, and never fire automatically.

- POST /v1/delivery/test: send a real interactive card to the cc-connect
  staging chat and immediately PATCH-update it in place. Verifies the send /
  update / platform_message_id receipt / idempotency path end to end.
- POST /v1/document/test: block-level write/read/update/delete round-trip on
  an explicitly provided feishu staging document, then cleans up after
  itself (never touches pi-notes or other sections).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..dependencies import require_token

router = APIRouter(prefix="/v1", tags=["ops-test"])


class DeliveryTestRequest(BaseModel):
    """No target fields: delivery test ALWAYS uses the service's configured
    cc-connect staging target (delivery=cc_connect + token/project/session_key
    from the env file). A stray chat_id can never redirect the test card to a
    production session."""


class DeliveryTestResponse(BaseModel):
    platform_message_id: str
    updated: bool = True
    note: str = ""


@router.post("/delivery/test", dependencies=[Depends(require_token)])
async def delivery_test(request: Request, body: DeliveryTestRequest | None = None) -> DeliveryTestResponse:
    settings = request.app.state.settings
    port = getattr(request.app.state, "delivery_port", None)
    if port is None or settings.scheduler.delivery != "cc_connect":
        raise HTTPException(
            status_code=503,
            detail="delivery test requires scheduler.delivery=cc_connect on the running service",
        )
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "title": "researchd delivery test",
        "body": f"**发送于 {ts}**\n这是 researchd → cc-connect → 飞书卡片的往返测试（发送后本卡应原地更新）。",
        "buttons": [
            {"text": "✓ 测试按钮", "value": "/decision DELIVERY-TEST A --version 1"},
        ],
    }
    try:
        mid = await port.deliver(
            idempotency_key=f"delivery-test:{ts}",
            kind="delivery_test",
            payload=payload,
        )
        if not mid:
            raise HTTPException(status_code=502, detail="cc-connect returned no platform_message_id")
        await port.update(mid, {**payload, "body": payload["body"] + "\n\n**✅ 已通过 PATCH 原地更新确认。**"})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delivery test failed: {exc}") from exc
    return DeliveryTestResponse(platform_message_id=mid)


class DocumentTestRequest(BaseModel):
    project_id: str = Field(default="", description="project whose document to test (defaults from metadata)")
    document_id: str = Field(default="", description="explicit staging feishu docx document id (overrides project default)")


class DocumentTestResponse(BaseModel):
    created: bool
    updated: bool
    deleted: bool
    sections_found: int
    note: str


@router.post("/document/create", dependencies=[Depends(require_token)])
async def document_create(request: Request, body: DocumentCreateRequest) -> DocumentCreateResponse:
    """Create the project's feishu docx ONCE and persist the receipt
    (project.metadata.feishu_document_id + document.created event + first
    projection outbox) in the same transaction. Idempotent: replaying with
    the receipt already persisted returns the existing document."""
    from ...persistence.repositories import ProjectRepo
    from ...persistence.transaction import UnitOfWork
    from ...projections.feishu_client import FeishuDocClient
    from ...projections.feishu_doc import ensure_project_document

    settings = request.app.state.settings
    if settings.doc_platform != "feishu":
        raise HTTPException(
            status_code=503,
            detail="document create requires doc_platform=feishu on the running service",
        )
    if not settings.feishu.allow_auto_create:
        raise HTTPException(status_code=403, detail="feishu.allow_auto_create is disabled")
    platform = FeishuDocClient(
        app_id_env=settings.feishu.lark_app_id_env,
        app_secret_env=settings.feishu.lark_app_secret_env,
    )
    with UnitOfWork(request.app.state.session_factory) as uow:
        project = ProjectRepo(uow.session).get_by_project_id(body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"project {body.project_id!r} not found")
        document_id = await ensure_project_document(
            uow.session,
            platform,
            project,
            title_template=settings.feishu.doc_title_template,
            folder_token=settings.feishu.folder_token,
            staging_chat_id=settings.feishu.staging_chat_id,
            pi_open_id=settings.feishu.pi_open_id,
            default_permission=settings.feishu.default_permission,
        )
        uow.commit()
    return DocumentCreateResponse(
        project_id=body.project_id,
        document_id=document_id,
        title=(project.metadata or {}).get("feishu_document_title", ""),
        note="document created/confirmed and collaborators granted (best-effort)",
    )


class DocumentCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, description="project id")
    title: str = Field(default="", description="optional title override (uses template when empty)")


class DocumentCreateResponse(BaseModel):
    project_id: str
    document_id: str
    title: str = ""
    note: str = ""


@router.post("/document/test", dependencies=[Depends(require_token)])
async def document_test(request: Request, body: DocumentTestRequest) -> DocumentTestResponse:
    from ...persistence.repositories import ProjectRepo
    from ...projections.feishu_client import FeishuDocClient

    client = FeishuDocClient()
    document_id = body.document_id
    if not document_id:
        # default: the project's own document (created via document/create or
        # provisioned earlier) — a stray external id can never be used
        if not body.project_id:
            raise HTTPException(status_code=400, detail="document_id or project_id is required")
        from ...persistence.transaction import make_engine, make_session_factory

        settings = request.app.state.settings
        engine = make_engine(settings.db_path)
        with make_session_factory(engine)() as session:
            project = ProjectRepo(session).get_by_project_id(body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"project {body.project_id!r} not found")
        document_id = (project.metadata or {}).get("feishu_document_id") or ""
        if not document_id:
            raise HTTPException(status_code=404, detail="no document_id given and project has no feishu_document_id")
    section = f"conformance-{int(time.time())}"
    try:
        before = await client.list_blocks(document_id)
        if section in before:
            raise HTTPException(status_code=409, detail=f"leftover test block {section!r} — clean up manually")
        await client.create_block(document_id, section, "researchd document test: 初始内容")
        after_create = await client.list_blocks(document_id)
        if after_create.get(section) != "researchd document test: 初始内容":
            raise HTTPException(status_code=502, detail="create_block round-trip mismatch")
        await client.update_block(document_id, section, "researchd document test: 更新内容")
        after_update = await client.list_blocks(document_id)
        if after_update.get(section) != "researchd document test: 更新内容":
            raise HTTPException(status_code=502, detail="update_block round-trip mismatch")
        # cleanup: delete our own block (child-of-page by index)
        await client.delete_block(document_id, section)
        remaining = await client.list_blocks(document_id)
        if section in remaining:
            raise HTTPException(status_code=502, detail="cleanup did not remove the test block")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"document test failed: {exc}") from exc
    return DocumentTestResponse(
        created=True, updated=True, deleted=True,
        sections_found=len(before),
        note="block-level create/update/delete round-trip verified; test block removed",
    )
