"""API dependencies: service container and optional bearer-token auth."""

from __future__ import annotations

import secrets as _secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.services import ZetaServices, get_services


def services() -> ZetaServices:
    return get_services()


def _check_token(provided: Optional[str], expected: str) -> bool:
    return bool(provided) and _secrets.compare_digest(provided, expected)


async def require_auth(request: Request, svc: ZetaServices = Depends(services)) -> None:
    token = svc.settings.api_token
    if not token:
        return
    auth = request.headers.get("authorization", "")
    provided = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token")
    if not _check_token(provided, token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API token")


async def ws_auth(ws: WebSocket, svc: ZetaServices) -> bool:
    token = svc.settings.api_token
    if not token:
        return True
    auth = ws.headers.get("authorization", "")
    provided = auth[7:] if auth.lower().startswith("bearer ") else ws.query_params.get("token")
    return _check_token(provided, token)
