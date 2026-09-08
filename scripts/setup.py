#!/usr/bin/env python
"""Zeta installer (cross-platform, Windows-first).

    python scripts/setup.py            # full install
    python scripts/setup.py --start    # install then start
    python scripts/setup.py --no-frontend

Steps: check Python/Node, create venv, install backend deps, install frontend deps
and build, create .env from .env.example, check Ollama/LM Studio, initialise the
database, optionally install Playwright's Chromium.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = BACKEND / ".venv"
IS_WIN = sys.platform == "win32"
PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def say(msg: str) -> None:
    print(f"[zeta] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[zeta] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, cwd=None, check=True, **kw):
    say("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, **kw)


def check_python() -> None:
    if sys.version_info < (3, 11):
        fail(f"Python 3.11+ required, found {sys.version.split()[0]}")
    say(f"Python {sys.version.split()[0]} OK")


def check_node() -> bool:
    node = shutil.which("node")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not node or not npm:
        say("Node.js/npm not found - the frontend will not be built. Install Node 18+ from https://nodejs.org")
        return False
    v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    say(f"Node {v} OK")
    return True


def create_venv() -> None:
    if not PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(PY), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([str(PY), "-m", "pip", "install", "-q", "-r", str(BACKEND / "requirements.txt")])
    say("Backend dependencies installed")


def install_playwright(auto: bool) -> None:
    if not auto:
        return
    try:
        run([str(PY), "-m", "playwright", "install", "chromium"], check=False)
    except Exception as e:  # noqa: BLE001
        say(f"Playwright browser install skipped: {e}")


def build_frontend() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    run([npm, "install", "--silent"], cwd=FRONTEND, shell=IS_WIN)
    run([npm, "run", "build"], cwd=FRONTEND, shell=IS_WIN)
    say("Frontend built to frontend/dist")


def create_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        say(".env already exists (kept)")
        return
    shutil.copy(ROOT / ".env.example", env)
    say("Created .env from .env.example - the setup wizard in the UI will fill it in")


def probe(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def check_llm() -> None:
    tags = probe("http://localhost:11434/api/tags")
    if tags is not None:
        models = [m.get("name") for m in tags.get("models", [])]
        say(f"Ollama detected with models: {', '.join(models) or '(none - run `ollama pull llama3.1`)'}")
        return
    lm = probe("http://localhost:1234/v1/models")
    if lm is not None:
        say("LM Studio server detected")
        return
    say("No local LLM detected. Install Ollama (https://ollama.com) and run `ollama pull llama3.1`,")
    say("or start LM Studio's server, or set an API provider in .env / the setup wizard.")


def init_db() -> None:
    code = (
        "import asyncio, sys; sys.path.insert(0, r'%s');"
        "from app.core.config import get_settings; from app.core import database;"
        "s=get_settings(); database.init_engine(s.database_url);"
        "asyncio.run(database.create_all()); print('database ready at', s.database_url)" % BACKEND
    )
    run([str(PY), "-c", code], cwd=BACKEND)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-frontend", action="store_true")
    ap.add_argument("--no-playwright", action="store_true")
    ap.add_argument("--start", action="store_true")
    args = ap.parse_args()

    os.chdir(ROOT)
    check_python()
    has_node = check_node()
    create_venv()
    install_playwright(not args.no_playwright)
    if has_node and not args.no_frontend:
        build_frontend()
    create_env()
    check_llm()
    init_db()
    say("Setup complete.")
    say("Start Zeta with:  start.bat   (or: python scripts/start.py)")
    if args.start:
        run([sys.executable, str(ROOT / "scripts" / "start.py")])


if __name__ == "__main__":
    main()
