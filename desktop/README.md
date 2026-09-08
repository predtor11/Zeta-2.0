# Zeta Desktop (Tauri scaffold)

The desktop wrapper is a thin Tauri shell around the local FastAPI backend:

```
Zeta Desktop (Tauri window)
     ↓ loads
Zeta Frontend (frontend/dist)
     ↓ HTTP/WebSocket on 127.0.0.1:8765
Local FastAPI Backend → Agent + Tools → Operating System
```

Status: **scaffold** (config + minimal Rust main). The backend is *not* bundled yet; start it with
`start.bat --no-browser` and the desktop window connects to it. Bundling the Python backend as a
sidecar (PyInstaller build placed in `src-tauri/binaries/`) is the next step.

## Build

Requirements: Rust toolchain, Node 18+, the Tauri prerequisites for Windows (WebView2, MSVC build tools).

```
cd frontend && npm run build
cd ../desktop
cargo install tauri-cli --version "^2"
cargo tauri build
```

The window opens `http://127.0.0.1:8765`. Set `HOST`/`PORT` in `.env` and `tauri.conf.json` consistently.
