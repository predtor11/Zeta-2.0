"""ConfirmationManager - pauses a task until the user confirms or cancels.

The orchestrator awaits `request(...)`; the API route `/api/confirm` resolves
it.  Confirmations time out (CONFIRMATION_TIMEOUT_SECONDS) and are cancelled
when their task is cancelled.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.events import event_bus
from app.core.exceptions import ConfirmationDenied, ConfirmationTimeout


@dataclass
class Confirmation:
    id: str
    task_id: str
    tool: str
    risk: str
    description: str
    details: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    action_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "task_id": self.task_id, "tool": self.tool, "risk": self.risk,
            "description": self.description, "details": self.details,
            "created_at": self.created_at.isoformat(), "expires_at": self.expires_at.isoformat(),
        }


class ConfirmationManager:
    def __init__(self, timeout_seconds: int = 300):
        self.timeout = timeout_seconds
        self._pending: Dict[str, Confirmation] = {}

    def pending(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self._pending.values()]

    def pending_for_task(self, task_id: str) -> List[Confirmation]:
        return [c for c in self._pending.values() if c.task_id == task_id]

    async def request(self, *, task_id: str, tool: str, risk: str, description: str,
                      details: Optional[Dict[str, Any]] = None, action_key: Optional[str] = None,
                      timeout: Optional[int] = None) -> bool:
        """Block until the user answers. Returns the 'remember' flag if approved; raises on deny/timeout."""
        loop = asyncio.get_running_loop()
        cid = uuid.uuid4().hex[:12]
        conf = Confirmation(
            id=cid, task_id=task_id, tool=tool, risk=risk, description=description, details=details or {},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=timeout or self.timeout),
            future=loop.create_future(), action_key=action_key,
        )
        self._pending[cid] = conf
        event_bus.publish("confirmation_required", task_id=task_id, confirmation=conf.to_dict(),
                          message=f"Waiting for confirmation: {description}")
        try:
            approved, remember = await asyncio.wait_for(conf.future, timeout=timeout or self.timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(cid, None)
            event_bus.publish("confirmation_resolved", task_id=task_id, confirmation_id=cid, approved=False, reason="timeout")
            raise ConfirmationTimeout(f"Confirmation {cid} timed out") from e
        except asyncio.CancelledError:
            self._pending.pop(cid, None)
            event_bus.publish("confirmation_resolved", task_id=task_id, confirmation_id=cid, approved=False, reason="cancelled")
            raise
        finally:
            self._pending.pop(cid, None)
        event_bus.publish("confirmation_resolved", task_id=task_id, confirmation_id=cid, approved=approved, reason="user")
        if not approved:
            raise ConfirmationDenied(f"User declined: {description}")
        return bool(remember)

    def resolve(self, confirmation_id: str, approved: bool, remember: bool = False) -> Optional[Confirmation]:
        conf = self._pending.get(confirmation_id)
        if conf is None:
            return None
        if not conf.future.done():
            conf.future.set_result((approved, remember))
        return conf

    def cancel_for_task(self, task_id: str) -> int:
        n = 0
        for conf in list(self._pending.values()):
            if conf.task_id == task_id and not conf.future.done():
                conf.future.set_result((False, False))
                n += 1
        return n

    def cancel_all(self) -> int:
        n = 0
        for conf in list(self._pending.values()):
            if not conf.future.done():
                conf.future.set_result((False, False))
                n += 1
        return n
