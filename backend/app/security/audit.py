"""Audit logging - every permission decision, confirmation and tool call.

Written to the `audit_log` table and mirrored to the structured log.  Arguments
are stored only when the tool marks them safe to log (default) and are scrubbed
for secrets.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.core import database
from app.core.logging import scrub

log = logging.getLogger("zeta.audit")

MAX_DETAILS_CHARS = 4000


def _safe_details(details: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not details:
        return None
    try:
        text = json.dumps(details, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(details)
    text = scrub(text)
    if len(text) > MAX_DETAILS_CHARS:
        return {"_truncated": text[:MAX_DETAILS_CHARS]}
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_raw": text}


async def record(event: str, *, task_id: Optional[str] = None, tool: Optional[str] = None, risk: Optional[str] = None,
                 decision: Optional[str] = None, details: Optional[Dict[str, Any]] = None, success: Optional[bool] = None,
                 duration_ms: Optional[int] = None) -> None:
    payload = _safe_details(details)
    log.info("%s tool=%s risk=%s decision=%s success=%s task=%s %s", event, tool, risk, decision, success, task_id,
             json.dumps(payload, ensure_ascii=False, default=str) if payload else "",
             extra={"task_id": task_id, "tool": tool, "event": event, "duration_ms": duration_ms})
    if not database.is_initialised():
        return
    try:
        from app.models.db import AuditLog

        async with database.session_scope() as s:
            s.add(AuditLog(task_id=task_id, event=event, tool=tool, risk=risk, decision=decision,
                           details=payload, success=success, duration_ms=duration_ms))
    except Exception as e:  # noqa: BLE001
        log.warning("audit write failed: %s", e.__class__.__name__)


async def query(limit: int = 100, *, event: Optional[str] = None, task_id: Optional[str] = None,
                tool: Optional[str] = None) -> list:
    """Most recent audit rows (newest first) for the UI / API."""
    if not database.is_initialised():
        return []
    from sqlalchemy import select

    from app.models.db import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).limit(max(1, min(limit, 1000)))
    if event:
        stmt = stmt.where(AuditLog.event == event)
    if task_id:
        stmt = stmt.where(AuditLog.task_id == task_id)
    if tool:
        stmt = stmt.where(AuditLog.tool == tool)
    async with database.session_scope() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [
        {"id": r.id, "ts": r.ts.isoformat() if r.ts else None, "task_id": r.task_id, "event": r.event, "tool": r.tool,
         "risk": r.risk, "decision": r.decision, "details": r.details, "success": r.success, "duration_ms": r.duration_ms}
        for r in rows
    ]
