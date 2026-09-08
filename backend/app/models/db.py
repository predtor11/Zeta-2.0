"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def to_llm(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            d["name"] = self.tool_name
        return d


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    plan: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    tools_used: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class MemoryItem(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="fact")  # preference | fact | project | contact | other
    tags: Mapped[str] = mapped_column(String(500), default="")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    embedding: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_accessed: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(50))  # tool_call | permission | confirmation | error | user_request
    tool: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    risk: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    details: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class EmotionRecord(Base):
    """One reading of how the user seemed, so Zeta can notice patterns over days."""

    __tablename__ = "emotion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    label: Mapped[str] = mapped_column(String(32), index=True)
    valence: Mapped[float] = mapped_column(Float, default=0.0)
    arousal: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    intensity: Mapped[float] = mapped_column(Float, default=0.0)
    cues: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    crisis_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # reminder | request
    payload: Mapped[str] = mapped_column(Text)      # reminder text or natural-language request for the agent
    schedule_type: Mapped[str] = mapped_column(String(20))  # once | interval | cron
    run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cron: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
