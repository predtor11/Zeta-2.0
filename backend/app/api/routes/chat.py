"""Chat and conversation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth, services
from app.models.schemas import ChatRequest, ChatResponse, ConversationOut, MessageOut
from app.services import ZetaServices

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_auth)])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, svc: ZetaServices = Depends(services)) -> ChatResponse:
    task = await svc.submit(req.message, req.conversation_id, wait=req.wait)
    audio_url = None
    if req.wait and req.speak and task.result:
        try:
            data, mime = await svc.synthesize(task.result[:2000])
            audio_url = f"/api/voice/audio/{svc.cache_audio(data, mime)}"
        except Exception:  # noqa: BLE001
            audio_url = None
    return ChatResponse(task_id=task.id, conversation_id=task.conversation_id or req.conversation_id or "",
                        status=task.status.value, reply=task.result if req.wait else None,
                        error=task.error if req.wait else None, audio_url=audio_url)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(svc: ZetaServices = Depends(services)):
    rows = await svc.short_term.list_conversations()
    return [ConversationOut(id=r.id, title=r.title, created_at=r.created_at, updated_at=r.updated_at) for r in rows]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def conversation_messages(conversation_id: str, svc: ZetaServices = Depends(services)):
    rows = await svc.short_term.messages(conversation_id)
    return [MessageOut(id=r.id, role=r.role, content=r.content, tool_name=r.tool_name, task_id=r.task_id, created_at=r.created_at)
            for r in rows if r.role in ("user", "assistant") and not (r.role == "user" and r.tool_name)]


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, svc: ZetaServices = Depends(services)):
    if not await svc.short_term.delete_conversation(conversation_id):
        raise HTTPException(404, "Conversation not found")
    return {"deleted": True}
