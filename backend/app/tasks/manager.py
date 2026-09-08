"""Task manager - one Task per user request.

    PENDING -> PLANNING -> EXECUTING <-> WAITING_FOR_CONFIRMATION -> COMPLETED | FAILED | CANCELLED

Tasks run as asyncio tasks; cancellation propagates `CancelledError` into the
orchestrator, which unwinds tool execution and releases pending confirmations.
State is mirrored to the `tasks` table for history.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core import database
from app.core.events import event_bus

log = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


@dataclass
class Task:
    id: str
    request: str
    conversation_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    plan: List[Dict[str, Any]] = field(default_factory=list)
    current_step: int = 0
    tools_used: List[str] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    _started: float = field(default_factory=time.monotonic, repr=False)
    _aio: Optional[asyncio.Task] = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "conversation_id": self.conversation_id, "request": self.request,
            "status": self.status.value, "plan": self.plan, "current_step": self.current_step,
            "tools_used": self.tools_used, "result": self.result, "error": self.error,
            "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
        }


class TaskManager:
    def __init__(self, max_history: int = 200):
        self._tasks: Dict[str, Task] = {}
        self.max_history = max_history

    # ---- lifecycle -------------------------------------------------------
    def create(self, request: str, conversation_id: Optional[str] = None) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], request=request, conversation_id=conversation_id)
        self._tasks[task.id] = task
        self._trim()
        self._broadcast(task)
        asyncio.ensure_future(self._persist(task))
        return task

    def start(self, task: Task, coro: Awaitable[Any]) -> asyncio.Task:
        task._aio = asyncio.ensure_future(coro)
        task._aio.add_done_callback(lambda _: task._done.set())
        return task._aio

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self, limit: int = 50, active_only: bool = False) -> List[Task]:
        items = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        if active_only:
            items = [t for t in items if not t.status.terminal]
        return items[:limit]

    def active(self) -> List[Task]:
        return [t for t in self._tasks.values() if not t.status.terminal]

    async def wait(self, task: Task, timeout: Optional[float] = None) -> Task:
        try:
            await asyncio.wait_for(task._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return task

    # ---- state updates ---------------------------------------------------
    def set_status(self, task: Task, status: TaskStatus, *, result: Optional[str] = None, error: Optional[str] = None,
                   message: Optional[str] = None) -> None:
        task.status = status
        task.updated_at = datetime.now(timezone.utc)
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if status.terminal:
            task.finished_at = task.updated_at
            task.duration_ms = int((time.monotonic() - task._started) * 1000)
        self._broadcast(task, message=message)
        asyncio.ensure_future(self._persist(task))

    def set_plan(self, task: Task, steps: List[str]) -> None:
        task.plan = [{"index": i + 1, "description": s, "status": "pending"} for i, s in enumerate(steps)]
        task.current_step = 0
        task.updated_at = datetime.now(timezone.utc)
        event_bus.publish("plan", task_id=task.id, plan=task.plan, message=f"Plan created with {len(steps)} steps")
        self._broadcast(task)
        asyncio.ensure_future(self._persist(task))

    def update_step(self, task: Task, index: int, status: str, note: str = "") -> None:
        for step in task.plan:
            if step["index"] == index:
                step["status"] = status
                if note:
                    step["note"] = note
        if status == "running":
            task.current_step = index
            for step in task.plan:
                if step["index"] < index and step["status"] in ("pending", "running"):
                    step["status"] = "done"
        task.updated_at = datetime.now(timezone.utc)
        event_bus.publish("plan", task_id=task.id, plan=task.plan, message=f"Step {index}: {status}")
        self._broadcast(task)

    def add_tool_used(self, task: Task, tool_name: str) -> None:
        if tool_name not in task.tools_used:
            task.tools_used.append(tool_name)

    # ---- cancellation ----------------------------------------------------
    def cancel(self, task_id: str, reason: str = "cancelled by user") -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status.terminal:
            return False
        if task._aio is not None and not task._aio.done():
            task._aio.cancel()
        self.set_status(task, TaskStatus.CANCELLED, error=reason, message=f"Task cancelled: {reason}")
        return True

    def cancel_all(self, reason: str = "stopped by user") -> int:
        n = 0
        for t in self.active():
            if self.cancel(t.id, reason):
                n += 1
        return n

    # ---- internals -------------------------------------------------------
    def _broadcast(self, task: Task, message: Optional[str] = None) -> None:
        event_bus.publish("task_update", task_id=task.id, task=task.to_dict(),
                          message=message or f"Task {task.status.value.lower().replace('_', ' ')}")

    def _trim(self) -> None:
        if len(self._tasks) <= self.max_history:
            return
        finished = sorted((t for t in self._tasks.values() if t.status.terminal), key=lambda t: t.created_at)
        for t in finished[: len(self._tasks) - self.max_history]:
            self._tasks.pop(t.id, None)

    async def _persist(self, task: Task) -> None:
        if not database.is_initialised():
            return
        try:
            from app.models.db import TaskRecord

            async with database.session_scope() as s:
                rec = await s.get(TaskRecord, task.id)
                if rec is None:
                    rec = TaskRecord(id=task.id, conversation_id=task.conversation_id, request=task.request,
                                     status=task.status.value, created_at=task.created_at)
                    s.add(rec)
                rec.status = task.status.value
                rec.plan = task.plan
                rec.current_step = task.current_step
                rec.tools_used = task.tools_used
                rec.result = task.result
                rec.error = task.error
                rec.updated_at = task.updated_at
                rec.finished_at = task.finished_at
                rec.duration_ms = task.duration_ms
        except Exception as e:  # noqa: BLE001
            log.debug("task persist failed: %s", e)

    async def load_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not database.is_initialised():
            return []
        from sqlalchemy import select

        from app.models.db import TaskRecord

        async with database.session_scope() as s:
            rows = (await s.execute(select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(limit))).scalars().all()
        return [
            {"id": r.id, "conversation_id": r.conversation_id, "request": r.request, "status": r.status,
             "plan": r.plan or [], "current_step": r.current_step, "tools_used": r.tools_used or [],
             "result": r.result, "error": r.error, "created_at": r.created_at.isoformat(),
             "updated_at": r.updated_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "duration_ms": r.duration_ms}
            for r in rows
        ]
