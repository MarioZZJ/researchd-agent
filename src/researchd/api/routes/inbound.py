"""Inbound message route: POST /v1/inbound/messages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import require_token
from pydantic import BaseModel, Field

from ...application.handlers import HandlerError, handle_inbound, normalize_inbound
from ...persistence.transaction import UnitOfWork
from ..dependencies import get_uow

router = APIRouter(prefix="/v1/inbound", tags=["inbound"])


class InboundMessageRequest(BaseModel):
    schema: str = "researchd.inbound_message.v1"
    message_id: str
    platform: str = "feishu"
    cc_project: str | None = None
    cc_session_key: str | None = None
    actor: dict = Field(default_factory=dict)
    text: str = ""
    attachments: list | None = None
    received_at: str | None = None


@router.post("/messages", dependencies=[Depends(require_token)])
def post_message(req: InboundMessageRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    msg = normalize_inbound(
        message_id=req.message_id,
        platform=req.platform,
        cc_project=req.cc_project,
        cc_session_key=req.cc_session_key,
        actor=req.actor,
        text=req.text,
        attachments=req.attachments,
    )
    try:
        reply = handle_inbound(uow.session, msg, fallback_project=req.cc_project)
        uow.commit()
    except HandlerError as exc:
        uow.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    except Exception as exc:  # unexpected: roll back, surface a fixed error
        uow.rollback()
        raise HTTPException(status_code=500, detail="internal error (see service log)") from exc
    return {
        "message_id": req.message_id,
        "duplicate": reply.text.startswith("duplicate"),
        "reply": reply.text,
        "data": reply.data,
    }
