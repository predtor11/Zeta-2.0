# Zeta architecture

## Overview

```
                    ┌──────────────────────────┐
                    │        ZETA UI           │  React + TypeScript (frontend/)
                    │ Chat · Voice · Plan ·    │  REST + WebSocket
                    │ Activity · Tools · Memory│
                    └────────────┬─────────────┘
                                 │ /api/*  /ws
                    ┌────────────▼─────────────┐
                    │      FastAPI (app/main)  │  routes: chat, voice, tasks, system, memory, schedules, ws
                    └────────────┬─────────────┘
                                 │ ZetaServices (app/services.py) – composition root
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
┌───────────────┐      ┌──────────────────┐      ┌──────────────────┐
│ Orchestrator  │◄────►│  TaskManager     │      │ Providers        │
│ (agent loop)  │      │  Confirmations   │      │ LLM · STT · TTS  │
│ prompts,      │      │  EventBus        │      │ Messaging · Email│
│ planning      │      └──────────────────┘      └──────────────────┘
└──────┬────────┘
       │ tool calls
       ▼
┌────────────────────────────────────────────────────────────────────┐
│ PermissionManager → risk classification → ALLOW / CONFIRM / DENY   │  app/security
│ validators (paths, commands, urls) · trust boundary · audit log    │
└──────┬─────────────────────────────────────────────────────────────┘
       ▼
┌────────────┬────────────┬───────────┬──────────┬──────────┬────────┐
│ filesystem │ computer   │ terminal  │ browser  │ developer│ memory │  app/tools/* (plugins)
│ messaging  │ email      │ screen    │ scheduler│ agent    │ + plugins/
└────────────┴────────────┴───────────┴──────────┴──────────┴────────┘
       ▼
  Windows OS · files · processes · Playwright/Chrome · IMAP/SMTP · WhatsApp
```

Every box is replaceable: providers are selected by configuration, tools are plugins, the event
bus and scheduler are small in-process classes with the same interface a broker or cloud
scheduler would implement.

## Backend layout (`backend/app`)

| Package | Responsibility |
|---------|----------------|
| `core/` | `config.py` (pydantic-settings, validated), `logging.py` (console + JSON lines, secret scrubbing), `events.py` (EventBus), `database.py` (async SQLAlchemy), `exceptions.py` |
| `models/` | ORM (`db.py`: conversations, messages, tasks, memories, audit log, scheduled jobs, emotion log) and API schemas |
| `providers/llm` | `LLMProvider` base, Ollama (native API), OpenAI-compatible (OpenAI, LM Studio, custom), Anthropic, Mock; `registry.py` factory |
| `providers/stt`, `providers/tts` | faster-whisper / ElevenLabs Scribe / OpenAI-compatible STT; Chatterbox (local GPU), Windows SAPI, Piper, ElevenLabs TTS |
| `providers/messaging`, `providers/email` | WhatsApp Web / Business / WAPI; Gmail / Outlook / IMAP |
| `security/` | `permissions.py`, `classifier.py` (command risk), `validators.py` (path sandbox), `trust.py` (untrusted content), `confirmation.py`, `secrets.py`, `audit.py` |
| `tools/` | `base.py` (Tool, ToolContext, ToolResult, ToolRegistry) and one plugin package per capability |
| `agent/` | `orchestrator.py` (loop), `prompts.py` (personality + rules), `toolcall_fallback.py` (JSON protocol) |
| `memory/` | `short_term.py` (conversation window), `long_term.py` (facts/preferences, keyword or embedding retrieval) |
| `emotion/` | `lexicon.py` (words), `prosody.py` (tone of voice), `crisis.py` (safety tiers + helplines), `engine.py` (fusion, trend, `emotion_log`), `speech.py` (delivery: voice settings and v3 audio tags), `state.py` (valence/arousal vocabulary) |
| `tasks/` | `manager.py` (Task lifecycle, cancellation, persistence) |
| `scheduler/` | `service.py` (LocalScheduler: once / interval / cron) |
| `plugins/` | `loader.py` (Plugin dataclass, builtin + external discovery) |
| `scripts/chatterbox_server.py` | Separate process and virtualenv (`.venv-tts`): PyTorch + Chatterbox behind a small HTTP API on port 8766. The backend starts it on demand and stops it on shutdown |
| `services.py` | Builds and wires everything; `submit()` entry point; `status()`; `reload()` after setup |

## Request flow

```
POST /api/chat {"message": "..."}                         (or WebSocket {"type":"chat"})
  └─ ZetaServices.submit()
       ├─ "stop"/"cancel" phrase? → cancel active tasks, reply immediately
       └─ TaskManager.create() → asyncio task → Orchestrator.run()
            1. persist user message (short-term memory)
            1b. emotion: reuse the reading staged by the voice route (words + tone), else read the words;
                record it, publish an `emotion` event, and turn it into one line of context
            2. system prompt = personality + rules + env + tool names (static, so the prompt prefix stays cached);
               long-term memory and the emotion note are appended to the last user message instead
            3. loop (≤ AGENT_MAX_STEPS):
                 LLM.chat(messages, tool schemas)
                 ├─ no tool calls → final reply → COMPLETED
                 └─ tool calls → for each:
                      validate args (pydantic) → tool.classify(args) → PermissionManager.check()
                      ├─ DENY    → tool result "denied", model told not to retry
                      ├─ CONFIRM → task WAITING_FOR_CONFIRMATION, UI modal, await user (timeout)
                      └─ ALLOW   → run with timeout → audit → result (wrapped as UNTRUSTED if external)
                      append result to messages, continue
            4. events published throughout: task_update, tool_start/end, plan, confirmation_required, assistant_message, emotion
```

## Data

SQLite (`data/zeta.db`) for conversations, tasks, memories, audit, schedules; a separate
`data/file_index.db` for the filesystem index (metadata + FTS5 content). Switch to PostgreSQL
with `DATABASE_URL`.

## Modes

`ZETA_MODE=local` (default): everything on the laptop, all tools available.
`ZETA_MODE=cloud`: API token required when bound beyond localhost; tools flagged
`available_in_cloud=False` (apps, clipboard, screenshots, open_file, power, screen) are hidden.

## Extending

- New capability → plugin (`docs/PLUGINS.md`).
- New model backend → subclass `LLMProvider`, call `register_provider()`.
- New voice → subclass `STTProvider` / `TTSProvider`, add to the `build_*` factory.
- Wake word → hook a listener that calls `POST /api/voice` (architecture only; NOT IMPLEMENTED).
- Vector memory → replace `LongTermMemory._rank`/`search` with Chroma/FAISS/Qdrant behind the same methods.
