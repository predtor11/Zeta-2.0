"""In-process event bus.

The orchestrator, task manager and tools publish events; the WebSocket
endpoint and the activity log subscribe.  Keeping this in-process (rather
than Redis etc.) matches the local-first design; the class is small enough
to be swapped for a message broker in cloud mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger(__name__)

ACTIVITY_TYPES = {
    "activity", "task_update", "confirmation_required", "confirmation_resolved",
    "tool_start", "tool_end", "notification", "plan",
}


class Event(dict):
    """A plain dict with a few guaranteed keys: type, ts, and optional task_id."""


class EventBus:
    def __init__(self, history_size: int = 500):
        self._subscribers: List[asyncio.Queue] = []
        self._activity: Deque[Event] = deque(maxlen=history_size)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def publish(self, type_: str, task_id: Optional[str] = None, **data: Any) -> Event:
        ev = Event(type=type_, ts=datetime.now(timezone.utc).isoformat(), task_id=task_id, **data)
        if type_ in ACTIVITY_TYPES:
            self._activity.append(ev)
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("Dropping event for slow subscriber")
        return ev

    def activity(self, message: str, task_id: Optional[str] = None, level: str = "info", **extra: Any) -> Event:
        return self.publish("activity", task_id=task_id, message=message, level=level, **extra)

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        items = list(self._activity)[-limit:]
        return [dict(e) for e in items]

    def clear(self) -> None:
        self._activity.clear()


event_bus = EventBus()
