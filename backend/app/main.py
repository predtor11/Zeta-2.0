"""Zeta backend entry point.

    uvicorn app.main:app --host 127.0.0.1 --port 8765

Serves the API, the WebSocket, and (when built) the React frontend from
`frontend/dist`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import chat, email_oauth, memory, schedules, system, tasks, voice, ws
from app.core.config import PROJECT_DIR, get_settings
from app.core.exceptions import ZetaError
from app.services import get_services

log = logging.getLogger("zeta")


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc = get_services()
    await svc.start()
    try:
        yield
    finally:
        await svc.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Zeta", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(ZetaError)
    async def _zeta_error(_: Request, exc: ZetaError):
        return JSONResponse(status_code=400, content={"detail": exc.user_message})

    for r in (chat.router, voice.router, tasks.router, system.router, memory.router, schedules.router, email_oauth.router, ws.router):
        app.include_router(r)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True, "name": settings.zeta_name, "version": __version__}

    dist = PROJECT_DIR / "frontend" / "dist"
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def root():
            return {"name": "Zeta", "version": __version__, "ui": "Frontend not built. Run `npm run dev` in frontend/ or `npm run build`.",
                    "docs": "/api/docs"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=s.debug, log_level=s.log_level.lower())
