"""Scheduler endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth, services
from app.models.schemas import ScheduleCreate, ScheduleOut
from app.scheduler.service import job_to_dict
from app.services import ZetaServices

router = APIRouter(prefix="/api/schedules", tags=["schedules"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(svc: ZetaServices = Depends(services)):
    return [ScheduleOut(**job_to_dict(j)) for j in await svc.scheduler.list()]


@router.post("", response_model=ScheduleOut)
async def create_schedule(body: ScheduleCreate, svc: ZetaServices = Depends(services)):
    try:
        job = await svc.scheduler.add(name=body.name, kind=body.kind, payload=body.payload, schedule_type=body.schedule_type,
                                      run_at=body.run_at, interval_seconds=body.interval_seconds, cron=body.cron)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ScheduleOut(**job_to_dict(job))


@router.delete("/{job_id}")
async def delete_schedule(job_id: str, svc: ZetaServices = Depends(services)):
    if not await svc.scheduler.delete(job_id):
        raise HTTPException(404, "Schedule not found")
    return {"deleted": True}


@router.post("/{job_id}/enable")
async def enable_schedule(job_id: str, enabled: bool = True, svc: ZetaServices = Depends(services)):
    if not await svc.scheduler.set_enabled(job_id, enabled):
        raise HTTPException(404, "Schedule not found")
    return {"enabled": enabled}
