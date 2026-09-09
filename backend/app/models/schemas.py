"""Pydantic API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: Optional[str] = None
    wait: bool = Field(default=False, description="Block until the task completes and include the reply")
    speak: bool = Field(default=False, description="Also synthesise the reply to speech (returned as URL)")


class ChatResponse(BaseModel):
    task_id: str
    conversation_id: str
    status: str
    reply: Optional[str] = None
    error: Optional[str] = None
    audio_url: Optional[str] = None


class PlanStep(BaseModel):
    index: int
    description: str
    status: str = "pending"  # pending | running | done | failed | skipped


class TaskOut(BaseModel):
    id: str
    conversation_id: Optional[str]
    request: str
    status: str
    plan: List[PlanStep] = []
    current_step: int = 0
    tools_used: List[str] = []
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


class ConfirmRequest(BaseModel):
    confirmation_id: str
    approved: bool
    remember: bool = Field(default=False, description="Remember this decision for identical actions in this session")


class PendingConfirmation(BaseModel):
    id: str
    task_id: str
    tool: str
    risk: str
    description: str
    details: Dict[str, Any] = {}
    created_at: datetime
    expires_at: datetime


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    category: str = "fact"
    tags: List[str] = []
    importance: float = 0.5


class MemoryOut(BaseModel):
    id: str
    content: str
    category: str
    tags: List[str]
    importance: float
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    tool_name: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class SystemStatus(BaseModel):
    name: str
    version: str
    online: bool
    mode: str
    llm: Dict[str, Any]
    voice: Dict[str, Any]
    internet: Dict[str, Any]
    computer: Dict[str, Any]
    tools: int
    plugins: List[Dict[str, Any]]
    active_tasks: int
    pending_confirmations: int
    setup_complete: bool


class ToolInfo(BaseModel):
    name: str
    description: str
    category: str
    risk_level: str
    requires_confirmation: bool
    enabled: bool
    parameters: Dict[str, Any]


class SetupRequest(BaseModel):
    llm_provider: str
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    tts_provider: str = "disabled"
    stt_provider: str = "disabled"
    elevenlabs_api_key: str = ""
    tts_voice: Optional[str] = None
    chatterbox_model: str = ""
    chatterbox_voice: Optional[str] = None      # "" clears it back to the built-in voice
    chatterbox_device: str = ""
    emotion_enabled: Optional[bool] = None
    emotion_adapt_voice: Optional[bool] = None
    emotion_region: str = ""
    elevenlabs_model: str = ""
    wake_word_enabled: Optional[bool] = None
    wake_word: str = ""
    llm_stream: Optional[bool] = None
    database_url: Optional[str] = None   # empty string = back to local SQLite
    api_token: Optional[str] = None
    permissions: Dict[str, bool] = {}
    allowed_roots: List[str] = []


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)   # a whole reply for /speak/plan; one piece for /speak
    lead: bool = True     # first piece of the reply: the only one that may open with a sigh/breath
    final: bool = True    # last piece: the only one after which the language model is reloaded


class ScheduleCreate(BaseModel):
    name: str
    kind: str = "reminder"        # reminder | request
    payload: str
    schedule_type: str = "once"   # once | interval | cron
    run_at: Optional[datetime] = None
    interval_seconds: Optional[int] = None
    cron: Optional[str] = None


class ScheduleOut(BaseModel):
    id: str
    name: str
    kind: str
    payload: str
    schedule_type: str
    run_at: Optional[datetime]
    interval_seconds: Optional[int]
    cron: Optional[str]
    enabled: bool
    last_run: Optional[datetime]
    next_run: Optional[datetime]
