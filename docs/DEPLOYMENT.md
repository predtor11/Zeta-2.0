# Deployment

## 1. Local (default, full computer control)

```
install.bat   →   start.bat   →   http://127.0.0.1:8765
```

`scripts/setup.py` checks Python and Node, creates `backend/.venv`, installs requirements,
installs Playwright's Chromium, builds the UI into `frontend/dist` (served by the backend),
copies `.env.example` to `.env`, detects Ollama / LM Studio and initialises the database.

Optional extras: `pip install -r backend/requirements-optional.txt` (faster-whisper, pyautogui,
keyring, PDF/DOCX/XLSX extraction). Whisper needs `ffmpeg` on PATH to decode browser audio.

Development: `python scripts/start.py --dev` runs uvicorn with reload plus the Vite dev server
on http://localhost:5173 (proxying `/api` and `/ws`).

Run at logon: see `deployment/local/README.md`.

## 2. Docker Compose (cloud mode)

```
cp .env.example .env      # set API_TOKEN and an LLM
docker compose up -d --build
# UI: http://localhost:8080   API: http://localhost:8765
docker compose --profile ollama up -d   # add a containerised Ollama
```

Cloud mode deliberately hides host-computer tools (apps, clipboard, screenshots, open_file,
power, screen) because a container cannot control your desktop. Files are limited to the mounted
`./workspace`. Browser tools run headless. See `deployment/local/README.md` for the host-agent
pattern if you need both a hosted API and desktop control.

## 3. AWS (optional)

`deployment/aws/README.md` covers a single EC2 instance running the compose stack, plus optional
RDS, S3, CloudFront, Route53 and Lambda/EventBridge for schedules, and cheaper alternatives
(Oracle free tier, Fly.io, Hetzner, Cloudflare Tunnel to your laptop).

## 4. Desktop app

`desktop/` contains a Tauri scaffold that opens the local UI in a native window. Bundling the
Python backend as a sidecar is NOT IMPLEMENTED yet; start the backend separately.

## Switching LOCAL ↔ CLOUD

One variable: `ZETA_MODE=local|cloud`. Nothing else in the code changes. In cloud mode:

- `API_TOKEN` is mandatory when `HOST` is not localhost; the UI sends it as a bearer token.
- host-only tools are hidden; everything else (browser, email, messaging, memory, scheduler,
  terminal inside the container, developer tools) keeps working.
- use `DATABASE_URL` for PostgreSQL and mount `/data` for SQLite/file index persistence.
