#!/usr/bin/env python
"""Start the Zeta backend (which also serves the built UI) and open the browser.

    python scripts/start.py            # production-ish: serves frontend/dist on http://127.0.0.1:8765
    python scripts/start.py --dev      # backend with reload + Vite dev server on :5173
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
IS_WIN = sys.platform == "win32"
PY = BACKEND / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


def _env_value(key: str, default: str) -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].split("#")[0].strip() or default
    return os.environ.get(key, default)


def open_browser_when_ready(url: str) -> None:
    def _wait():
        for _ in range(60):
            try:
                urllib.request.urlopen(url.rstrip("/") + "/api/ping", timeout=1)
                webbrowser.open(url)
                return
            except Exception:  # noqa: BLE001
                time.sleep(1)

    threading.Thread(target=_wait, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not PY.exists():
        print("Virtual environment not found. Run install.bat (or python scripts/setup.py) first.")
        sys.exit(1)
    host = _env_value("HOST", "127.0.0.1")
    port = _env_value("PORT", "8765")
    cmd = [str(PY), "-m", "uvicorn", "app.main:app", "--host", host, "--port", port]
    procs = []
    if args.dev:
        cmd += ["--reload"]
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm:
            procs.append(subprocess.Popen([npm, "run", "dev"], cwd=FRONTEND, shell=IS_WIN))
            ui = "http://localhost:5173"
        else:
            ui = f"http://{host}:{port}"
    else:
        if not (FRONTEND / "dist" / "index.html").exists():
            print("Frontend is not built; the API will run without the UI. Run install.bat or `npm run build` in frontend/.")
        ui = f"http://{host}:{port}"
    if not args.no_browser:
        open_browser_when_ready(ui)
    print(f"Zeta starting on {ui}  (Ctrl+C to stop)")
    try:
        subprocess.run(cmd, cwd=BACKEND)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
