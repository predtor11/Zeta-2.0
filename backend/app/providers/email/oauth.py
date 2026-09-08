"""OAuth 2.0 for Gmail and Outlook (IMAP/SMTP via XOAUTH2).

Authorization-code flow with PKCE, redirecting back to the local Zeta backend
(`http://localhost:<PORT>/api/email/oauth/callback`).  Tokens are stored in
`data/oauth_tokens.json` (never in prompts or logs) and refreshed automatically.

Setup (one time, both free):

* Gmail  - Google Cloud Console → APIs & Services → Credentials → Create OAuth client
           (type "Desktop app"). Enable nothing else; the `https://mail.google.com/`
           scope covers IMAP + SMTP. Put client id/secret in GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.
           While the OAuth consent screen is in "Testing", add your own Gmail address as a test user.
* Outlook - Microsoft Entra admin center → App registrations → New registration,
           "Accounts in any organizational directory and personal Microsoft accounts",
           platform "Mobile and desktop applications", redirect URI
           http://localhost:8765/api/email/oauth/callback, "Allow public client flows" = Yes.
           API permissions: IMAP.AccessAsUser.All, SMTP.Send (Office 365 Exchange Online), offline_access.
           Put the Application (client) ID in MICROSOFT_CLIENT_ID.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core.exceptions import ConfigurationError, ProviderError

log = logging.getLogger(__name__)


def _provider_config(name: str, tenant: str = "common") -> Dict[str, Any]:
    if name == "gmail":
        return {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://mail.google.com/", "openid", "email"],
            "extra_auth": {"access_type": "offline", "prompt": "consent"},
            "needs_secret": True,
        }
    if name == "outlook":
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        return {
            "auth_url": f"{base}/authorize",
            "token_url": f"{base}/token",
            "scopes": ["https://outlook.office.com/IMAP.AccessAsUser.All", "https://outlook.office.com/SMTP.Send",
                       "offline_access", "openid", "email"],
            "extra_auth": {"response_mode": "query"},
            "needs_secret": False,
        }
    raise ConfigurationError(f"OAuth is not supported for email provider '{name}' (use gmail or outlook)")


def _jwt_email(id_token: Optional[str]) -> str:
    """Extract the email claim from an id_token without verification (display/login-name only)."""
    if not id_token or id_token.count(".") != 2:
        return ""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    except Exception:  # noqa: BLE001
        return ""


def xoauth2_string(email: str, access_token: str) -> str:
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01"


class OAuthManager:
    def __init__(self, token_path: Path, *, google_client_id: str = "", google_client_secret: str = "",
                 microsoft_client_id: str = "", microsoft_tenant: str = "common", redirect_uri: str = ""):
        self.token_path = Path(token_path)
        self._google = (google_client_id, google_client_secret)
        self._microsoft = (microsoft_client_id, "")
        self.tenant = microsoft_tenant or "common"
        self.redirect_uri = redirect_uri
        self._pending: Dict[str, Dict[str, Any]] = {}  # state -> {provider, verifier, created}
        self._tokens: Dict[str, Dict[str, Any]] = self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self.token_path.exists():
                return json.loads(self.token_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("could not read oauth tokens: %s", e.__class__.__name__)
        return {}

    def _save(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(self._tokens, indent=2), encoding="utf-8")

    # ---- config ----------------------------------------------------------
    def _client(self, provider: str) -> Tuple[str, str]:
        return self._google if provider == "gmail" else self._microsoft

    def configured(self, provider: str) -> bool:
        cid, secret = self._client(provider)
        cfg = _provider_config(provider, self.tenant)
        return bool(cid) and (bool(secret) or not cfg["needs_secret"])

    def status(self, provider: str) -> Dict[str, Any]:
        tok = self._tokens.get(provider)
        return {
            "provider": provider, "configured": self.configured(provider), "connected": bool(tok and tok.get("refresh_token")),
            "email": (tok or {}).get("email", ""), "expires_at": (tok or {}).get("expires_at"),
            "redirect_uri": self.redirect_uri,
            "missing": ([] if self.configured(provider) else
                        (["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"] if provider == "gmail" else ["MICROSOFT_CLIENT_ID"])),
        }

    def email_for(self, provider: str) -> str:
        return (self._tokens.get(provider) or {}).get("email", "")

    # ---- flow ------------------------------------------------------------
    def start(self, provider: str) -> Tuple[str, str]:
        """Return (authorization_url, state)."""
        if not self.configured(provider):
            st = self.status(provider)
            raise ConfigurationError(f"OAuth for {provider} is not configured. Set {', '.join(st['missing'])} in .env.")
        cfg = _provider_config(provider, self.tenant)
        cid, _ = self._client(provider)
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        self._pending = {k: v for k, v in self._pending.items() if time.time() - v["created"] < 900}
        self._pending[state] = {"provider": provider, "verifier": verifier, "created": time.time()}
        params = {
            "client_id": cid, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": " ".join(cfg["scopes"]), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            **cfg["extra_auth"],
        }
        return f"{cfg['auth_url']}?{urlencode(params)}", state

    async def finish(self, state: str, code: str) -> Dict[str, Any]:
        pend = self._pending.pop(state, None)
        if pend is None:
            raise ProviderError("unknown state", user_message="This sign-in link has expired or was already used. Start again from Zeta.")
        provider = pend["provider"]
        cfg = _provider_config(provider, self.tenant)
        cid, secret = self._client(provider)
        data = {"client_id": cid, "code": code, "code_verifier": pend["verifier"], "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri}
        if secret:
            data["client_secret"] = secret
        tok = await self._token_request(cfg["token_url"], data)
        email = _jwt_email(tok.get("id_token"))
        stored = {
            "access_token": tok.get("access_token"), "refresh_token": tok.get("refresh_token") or (self._tokens.get(provider) or {}).get("refresh_token"),
            "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60, "email": email, "scope": tok.get("scope", ""),
        }
        if not stored["refresh_token"]:
            raise ProviderError("no refresh token", user_message="The provider did not return a refresh token. Remove Zeta from your account's connected apps and try again.")
        self._tokens[provider] = stored
        self._save()
        return self.status(provider)

    async def access_token(self, provider: str) -> str:
        tok = self._tokens.get(provider)
        if not tok or not tok.get("refresh_token"):
            raise ConfigurationError(f"{provider} is not connected. Open Settings → Integrations and click Connect.")
        if tok.get("access_token") and time.time() < float(tok.get("expires_at", 0)):
            return tok["access_token"]
        cfg = _provider_config(provider, self.tenant)
        cid, secret = self._client(provider)
        data = {"client_id": cid, "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}
        if provider == "outlook":
            data["scope"] = " ".join(cfg["scopes"])
        if secret:
            data["client_secret"] = secret
        new = await self._token_request(cfg["token_url"], data)
        tok["access_token"] = new.get("access_token")
        tok["expires_at"] = time.time() + int(new.get("expires_in", 3600)) - 60
        if new.get("refresh_token"):
            tok["refresh_token"] = new["refresh_token"]
        self._tokens[provider] = tok
        self._save()
        return tok["access_token"]

    def disconnect(self, provider: str) -> bool:
        if provider in self._tokens:
            del self._tokens[provider]
            self._save()
            return True
        return False

    async def _token_request(self, url: str, data: Dict[str, str]) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="Could not reach the OAuth server.") from e
        if r.status_code >= 400:
            try:
                err = r.json()
                desc = err.get("error_description") or err.get("error") or r.text[:200]
            except ValueError:
                desc = r.text[:200]
            log.error("oauth token request failed: %s", desc)
            raise ProviderError(desc, user_message=f"OAuth sign-in failed: {desc}")
        return r.json()
