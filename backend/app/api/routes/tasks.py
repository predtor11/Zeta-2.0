"""Task and confirmation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth, services
from app.models.schemas import ConfirmRequest
from app.services import ZetaServices

router = APIRouter(prefix="/api", tags=["tasks"], dependencies=[Depends(require_auth)])


@router.get("/tasks")
async def list_tasks(limit: int = 50, active: bool = False, history: bool = False, svc: ZetaServices = Depends(services)):
    if history:
        return await svc.tasks.load_history(limit)
    return [t.to_dict() for t in svc.tasks.list(limit=limit, active_only=active)]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, svc: ZetaServices = Depends(services)):
    t = svc.tasks.get(task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    return t.to_dict()


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, svc: ZetaServices = Depends(services)):
    svc.confirmations.cancel_for_task(task_id)
    ok = svc.tasks.cancel(task_id)
    if not ok:
        raise HTTPException(409, "Task is not running")
    return {"cancelled": True}


@router.post("/tasks/cancel_all")
async def cancel_all(svc: ZetaServices = Depends(services)):
    svc.confirmations.cancel_all()
    return {"cancelled": svc.tasks.cancel_all()}


@router.get("/confirmations")
async def pending_confirmations(svc: ZetaServices = Depends(services)):
    return svc.confirmations.pending()


@router.post("/confirm")
async def confirm(req: ConfirmRequest, svc: ZetaServices = Depends(services)):
    conf = svc.confirmations.resolve(req.confirmation_id, req.approved, req.remember)
    if conf is None:
        raise HTTPException(404, "Confirmation not found or already resolved")
    return {"confirmation_id": req.confirmation_id, "approved": req.approved}
