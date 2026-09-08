"""Short-term memory = conversation history persisted in SQLite.

Provides the recent message window handed to the LLM and the append/persist
operations used by the orchestrator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.database import session_scope
from app.models.db import Conversation, Message


class ShortTermMemory:
    def __init__(self, window: int = 30):
        self.window = window

    async def ensure_conversation(self, conversation_id: Optional[str], title_hint: str = "") -> str:
        async with session_scope() as s:
            if conversation_id:
                conv = await s.get(Conversation, conversation_id)
                if conv:
                    return conv.id
            conv = Conversation(title=(title_hint[:80] or "New conversation"))
            s.add(conv)
            await s.flush()
            return conv.id

    async def append(self, conversation_id: str, role: str, content: str, *, tool_calls: Optional[Any] = None,
                     tool_call_id: Optional[str] = None, tool_name: Optional[str] = None,
                     task_id: Optional[str] = None) -> str:
        async with session_scope() as s:
            m = Message(conversation_id=conversation_id, role=role, content=content or "", tool_calls=tool_calls,
                        tool_call_id=tool_call_id, tool_name=tool_name, task_id=task_id)
            s.add(m)
            conv = await s.get(Conversation, conversation_id)
            if conv is not None and conv.title == "New conversation" and role == "user":
                conv.title = content[:80]
            await s.flush()
            return m.id

    async def history(self, conversation_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Recent messages in LLM format, oldest first, trimmed to a window that starts at a user message."""
        limit = limit or self.window
        async with session_scope() as s:
            rows = (await s.execute(
                select(Message).where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc()).limit(limit * 3)
            )).scalars().all()
        rows = list(reversed(rows))
        msgs = [r.to_llm() for r in rows if r.role in ("user", "assistant", "tool")]
        # Keep only the last `limit` messages but never start with a dangling tool result
        msgs = msgs[-limit:]
        while msgs and msgs[0]["role"] == "tool":
            msgs.pop(0)
        # If we start with an assistant message that has tool_calls whose results were cut, drop it.
        return msgs

    async def list_conversations(self, limit: int = 50) -> List[Conversation]:
        async with session_scope() as s:
            rows = (await s.execute(select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit))).scalars().all()
            return list(rows)

    async def messages(self, conversation_id: str, limit: int = 200) -> List[Message]:
        async with session_scope() as s:
            rows = (await s.execute(
                select(Message).where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc()).limit(limit)
            )).scalars().all()
            return list(rows)

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with session_scope() as s:
            conv = await s.get(Conversation, conversation_id)
            if conv is None:
                return False
            await s.delete(conv)
            return True
