"""Email OAuth endpoints (Gmail / Outlook).

    GET  /api/email/oauth/status            -> connection state for gmail and outlook
    POST /api/email/oauth/start?provider=   -> {"url": ...}  open it in the browser
    GET  /api/email/oauth/callback          -> provider redirects here; stores tokens; shows a close-me page
    POST /api/email/oauth/disconnect?provider=
"""

from __future__ import annotations

import webbrowser

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.api.deps import require_auth, services
from app.core.events import event_bus
from app.core.exceptions import ZetaError
from app.services import ZetaServices

router = APIRouter(prefix="/api/email/oauth", tags=["email"])

_PAGE = """<!doctype html><html><body style="font-family:Segoe UI,sans-serif;background:#0b1019;color:#d6e2f0;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center">
<h2 style="letter-spacing:4px;color:#00d4ff">ZETA</h2><p>{message}</p><p style="color:#7d8ca3">You can close this window.</p>
</div></body></html>"""


@router.get("/status", dependencies=[Depends(require_auth)])
async def status(svc: ZetaServices = Depends(services)):
    return {"gmail": svc.oauth.status("gmail"), "outlook": svc.oauth.status("outlook"),
            "email_provider": svc.settings.email_provider.value or "disabled", "email_auth": svc.settings.email_auth}


@router.post("/start", dependencies=[Depends(require_auth)])
async def start(provider: str = Query(pattern="^(gmail|outlook)$"), open_browser: bool = True, svc: ZetaServices = Depends(services)):
    try:
        url, state = svc.oauth.start(provider)
    except ZetaError as e:
        raise HTTPException(400, e.user_message)
    if open_browser and svc.settings.is_local:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return {"url": url, "state": state}


@router.get("/callback", response_class=HTMLResponse)
async def callback(state: str = "", code: str = "", error: str = "", error_description: str = "",
                   svc: ZetaServices = Depends(services)):
    # No bearer auth here: the browser is redirected by Google/Microsoft. The one-time `state` is the credential.
    if error:
        return HTMLResponse(_PAGE.format(message=f"Sign-in failed: {error_description or error}"), status_code=400)
    if not state or not code:
        return HTMLResponse(_PAGE.format(message="Missing code/state in the callback."), status_code=400)
    try:
        st = await svc.oauth.finish(state, code)
    except ZetaError as e:
        return HTMLResponse(_PAGE.format(message=e.user_message), status_code=400)
    event_bus.activity(f"Email account connected via OAuth: {st.get('email') or st['provider']}")
    event_bus.publish("notification", title="Email connected", message=f"{st['provider']} account {st.get('email', '')} is connected.")
    return HTMLResponse(_PAGE.format(message=f"{st['provider'].capitalize()} connected as {st.get('email') or 'your account'}."))


@router.post("/disconnect", dependencies=[Depends(require_auth)])
async def disconnect(provider: str = Query(pattern="^(gmail|outlook)$"), svc: ZetaServices = Depends(services)):
    return {"disconnected": svc.oauth.disconnect(provider)}
