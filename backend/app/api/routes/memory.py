"""Long-term memory endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth, services
from app.models.schemas import MemoryCreate, MemoryOut
from app.services import ZetaServices

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_auth)])


def _out(r) -> MemoryOut:
    return MemoryOut(id=r.id, content=r.content, category=r.category, tags=[t for t in r.tags.split(",") if t],
                     importance=r.importance, created_at=r.created_at, updated_at=r.updated_at)


@router.get("", response_model=list[MemoryOut])
async def list_memory(q: str = "", svc: ZetaServices = Depends(services)):
    if q:
        items = await svc.long_term.search(q, limit=50)
        return [MemoryOut(**{k: v for k, v in i.items() if k != "score"}) for i in items]
    return [_out(r) for r in await svc.long_term.list()]


@router.post("", response_model=MemoryOut)
async def add_memory(body: MemoryCreate, svc: ZetaServices = Depends(services)):
    item = await svc.long_term.add(body.content, body.category, body.tags, body.importance)
    return _out(item)


@router.put("/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: str, body: MemoryCreate, svc: ZetaServices = Depends(services)):
    item = await svc.long_term.update(memory_id, body.content, body.category, body.tags, body.importance)
    if item is None:
        raise HTTPException(404, "Memory not found")
    return _out(item)


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, svc: ZetaServices = Depends(services)):
    if not await svc.long_term.delete(memory_id):
        raise HTTPException(404, "Memory not found")
    return {"deleted": True}


@router.delete("")
async def clear_memory(svc: ZetaServices = Depends(services)):
    return {"deleted": await svc.long_term.clear()}
