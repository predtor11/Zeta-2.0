# ZETA

A local-first, JARVIS-style AI agent for your Windows computer. Tell Zeta what you want in
natural language (text or voice); it plans, picks tools, asks before doing anything dangerous,
executes, and reports honestly.

```
"Find the PDF I edited yesterday about bus scheduling and open it."
"What's using all my memory?"                "Open VS Code and switch to Chrome."
"Search the web for the cheapest OLED 240Hz monitors in India."
"Check why my backend isn't running."        "Send Rahul a WhatsApp saying I'll be 20 minutes late."
"Remind me at 6pm to call the bank."         "Remember that I prefer concise answers."
```

Everything runs on your laptop: Python/FastAPI backend, React UI, SQLite, a local LLM through
Ollama or LM Studio, local speech recognition and Windows voices. Cloud LLM APIs, ElevenLabs,
Docker and AWS are optional plug-ins, never requirements.

## Quick start (Windows)

Prerequisites: Python 3.11+, Node.js 18+ (for the UI), and a local model server:
[Ollama](https://ollama.com) (`ollama pull llama3.1`) or LM Studio. Any OpenAI/Anthropic-compatible API works too.

```
git clone <repository> zeta
cd zeta
install.bat      # checks Python/Node, creates venv, installs deps, builds UI, creates .env, inits DB
install_tts.bat  # optional: Zeta's own voice (Chatterbox on your GPU, ~3 GB of PyTorch)
start.bat        # starts the backend on http://127.0.0.1:8765 and opens the UI
```

Without `install_tts.bat` Zeta still speaks, using Windows voices (`TTS_PROVIDER=local`) or
ElevenLabs. With it, the voice is local, unlimited and clonable from a short recording.

The first-run wizard asks for your AI provider, voice options and what Zeta may access.
Defaults are conservative: files, apps, browser and developer tools on; terminal, messaging,
email and screen control off until you enable them.

Development mode (hot reload for both halves): `python scripts/start.py --dev`.

## What works today

| Area | Capabilities | Notes |
|------|--------------|-------|
| Voice stage | The screen is just an animated orb (click it or press Space to talk, again to send; Esc stops). It reacts to your voice, Zeta's speech and the agent state, and Zeta listens again automatically when its reply ends with a question | Everything else lives in slide-in drawers: `[` status/conversations/task, `]` or `C` chat/activity/tools/tasks/memory/schedules/audit |
| Chat | Multi-turn chat with streamed replies and Markdown, conversation list, live activity/plan/tool panels, task history, confirmation modal | WebSocket real-time updates |
| Voice | Speech in through local faster-whisper (default), ElevenLabs Scribe or any OpenAI-compatible STT. Speech out through **Chatterbox running on your own GPU** (default, clone any voice from a 10-second clip), Windows voices, Piper or ElevenLabs. Hands-free wake word "Hey Zeta". Long replies are generated sentence by sentence and scheduled gaplessly, so a long answer starts sooner without stuttering, and Markdown is never read aloud. Multilingual in and out, restricted to the languages you actually speak; English and Hindi can clone different voices | `STT_PROVIDER`, `TTS_PROVIDER`, `WAKE_WORD_ENABLED`; `install_tts.bat` for the local voice |
| Filesystem | Indexed search by name/ext/folder/date/size, full-text content search (txt, code, PDF, DOCX, XLSX), open, read, copy, move, rename, create, delete (Recycle Bin), metadata | Restricted to `FS_ALLOWED_ROOTS` |
| Computer | List/launch/close/focus apps, clipboard, screenshots, system info, resource usage, network status, lock, shutdown/restart | Windows-first |
| Terminal | PowerShell/cmd/bash with SAFE / SENSITIVE / DANGEROUS / BLOCKED classification, background jobs, processes | Dangerous commands need confirmation or are denied |
| Browser | Playwright: open sites, web search, read page, click, fill forms, download, upload, screenshots; direct HTTP/API fetch | Persistent profile keeps logins |
| Developer | git status/diff/log, Docker status/logs, project inspection, GitHub queries | |
| Messaging | WhatsApp via Web automation, Meta Business API, or WAPI-style gateway; local contact book | Sends always confirm |
| Email | Gmail/Outlook/IMAP: list, search, read, draft, send, attachments | OAuth sign-in or app passwords |
| Emotional intelligence | Reads how you seem from your words and your tone of voice (pitch, pace, pauses, energy), adapts the reply and the spoken delivery, tracks mood across the day in a Mood panel, and switches to a supportive, non-clinical response with real helplines if someone is in serious distress | Local and offline, `EMOTION_ENABLED`; see docs/EMOTION.md |
| Hardware monitor | A Monitor panel in the right-hand rail: CPU load per core, memory, GPU load, VRAM, temperature and power, disks, network, battery, and which processes are competing with Zeta for the card. Also shows where Zeta's own model and voice weights actually are | Local, no configuration; CPU temperature needs LibreHardwareMonitor on Windows |
| Memory | Long-term facts and preferences (remember / recall / forget), injected into context | Optional embeddings |
| Planning | Multi-step plans shown in the UI, step status, task manager with cancellation ("Zeta, stop", Esc) | |
| Scheduling | Reminders, one-off and recurring (cron/interval) requests, managed from the Schedules panel | Runs inside the backend |
| Storage | SQLite by default; optional Supabase/PostgreSQL via `DATABASE_URL` with a migration script; audit log viewer in the UI | `scripts/migrate_db.py` |
| Screen | Vision analysis of screenshots, click/type/hotkeys/scroll (optional pyautogui) | Asks first |
| Security | Permission manager, per-category policy, confirmations, audit log, path sandbox, untrusted-content boundaries, secret scrubbing | See docs/SECURITY.md |
| Deployment | Native (default), Docker Compose (cloud mode), AWS EC2 guide, Tauri scaffold | |

Email supports Gmail/Outlook OAuth sign-in (PKCE, XOAUTH2) as well as app passwords.

Marked **NOT IMPLEMENTED**: Tauri sidecar bundling of the backend (the desktop shell is a scaffold; run
Zeta as a service + browser tab for now) and integrations listed under "future" (Spotify, Calendar, Notion...).
A custom-trained openWakeWord model for "hey zeta" is optional; the default whisper engine needs no training.
Nothing in the UI simulates functionality; each tool does the real thing or reports failure.

## Project layout

```
zeta/
├── backend/           FastAPI app: agent, tools, providers, security, memory, emotion, tasks, scheduler
│   ├── app/           (see docs/ARCHITECTURE.md)
│   ├── config/        permissions.yaml (defaults), permissions.local.yaml (yours), contacts.json
│   ├── tests/         146 tests: permissions, classifier, validators, filesystem, memory, agent, providers, API, OAuth, streaming, wake word, database
│   └── requirements.txt / requirements-optional.txt
├── frontend/          React + TypeScript + Vite UI
├── plugins/           external plugins (drop-in)
├── deployment/        docker/, aws/, local/
├── desktop/           Tauri scaffold
├── docs/              ARCHITECTURE, SECURITY, TOOLS, PLUGINS, DEPLOYMENT, INTEGRATIONS
├── scripts/           setup.py, start.py, gen_tool_docs.py
├── install.bat / start.bat / docker-compose.yml / .env.example
```

## Configuration

Copy `.env.example` to `.env` (the installer does this) and edit, or use the in-app wizard.
Key settings:

```env
LLM_PROVIDER=ollama          # ollama | lmstudio | openai | openrouter | openai_compatible | anthropic
LLM_MODEL=llama3.1
STT_PROVIDER=whisper         # disabled | whisper | elevenlabs | openai
TTS_PROVIDER=local           # disabled | local | piper | elevenlabs
FS_ALLOWED_ROOTS=C:\Users\you;D:\Projects
WHATSAPP_PROVIDER=web        # web | business | wapi
EMAIL_PROVIDER=gmail         # gmail | outlook | imap
```

Permissions live in `backend/config/permissions.yaml` (defaults) and `permissions.local.yaml`
(written by the wizard and the Tools panel). Secrets stay in `.env` or the OS credential store
(`SECRET_STORE=keyring`) and never reach the model.

## Tests

```
cd backend
.venv\Scripts\python -m pytest -q
```

## Docs

- [Architecture](docs/ARCHITECTURE.md) - components, request flow, diagrams
- [Security](docs/SECURITY.md) - permission model, confirmations, trust boundaries, secrets
- [Emotional intelligence](docs/EMOTION.md) - how Zeta reads emotion, what it does with it, and where the limits are
- [Tools and permissions](docs/TOOLS.md) - every tool, its risk level and category
- [Plugins](docs/PLUGINS.md) - adding capabilities
- [Deployment](docs/DEPLOYMENT.md) - local, Docker, AWS and free alternatives
- [Voice, messaging and email](docs/INTEGRATIONS.md) - provider setup

## License

MIT.
