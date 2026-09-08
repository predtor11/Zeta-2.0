"""Email OAuth: PKCE flow, token store/refresh, XOAUTH2 string, callback route (no network)."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import ConfigurationError, ProviderError
from app.providers.email.oauth import OAuthManager, xoauth2_string


def _mgr(tmp_path, **kw):
    return OAuthManager(tmp_path / "tok.json", google_client_id="gid", google_client_secret="gsec", microsoft_client_id="mid",
                        redirect_uri="http://localhost:8765/api/email/oauth/callback", **kw)


def _id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_xoauth2_string():
    assert xoauth2_string("a@b.com", "tok") == "user=a@b.com\x01auth=Bearer tok\x01\x01"


def test_start_builds_pkce_url(tmp_path):
    m = _mgr(tmp_path)
    url, state = m.start("gmail")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["gid"] and q["state"] == [state] and q["code_challenge_method"] == ["S256"]
    assert "https://mail.google.com/" in q["scope"][0]
    assert q["access_type"] == ["offline"]
    url, _ = m.start("outlook")
    assert "login.microsoftonline.com/common" in url and "IMAP.AccessAsUser.All" in url


def test_not_configured(tmp_path):
    m = OAuthManager(tmp_path / "t.json", redirect_uri="http://x")
    assert not m.configured("gmail")
    assert "GOOGLE_CLIENT_ID" in m.status("gmail")["missing"]
    with pytest.raises(ConfigurationError):
        m.start("gmail")


@pytest.mark.asyncio
async def test_finish_and_refresh(tmp_path, monkeypatch):
    m = _mgr(tmp_path)
    calls = []

    async def fake_token_request(url, data):
        calls.append(data)
        if data["grant_type"] == "authorization_code":
            assert data["code_verifier"] and data["client_secret"] == "gsec"
            return {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600, "id_token": _id_token("me@gmail.com")}
        assert data["refresh_token"] == "rt1"
        return {"access_token": "at2", "expires_in": 3600}

    monkeypatch.setattr(m, "_token_request", fake_token_request)
    _, state = m.start("gmail")
    st = await m.finish(state, "code123")
    assert st["connected"] and st["email"] == "me@gmail.com"
    assert (tmp_path / "tok.json").exists()
    assert await m.access_token("gmail") == "at1"          # cached
    m._tokens["gmail"]["expires_at"] = time.time() - 1     # expire
    assert await m.access_token("gmail") == "at2"          # refreshed
    assert len(calls) == 2
    # persisted across instances
    m2 = _mgr(tmp_path)
    assert m2.status("gmail")["connected"] and m2.email_for("gmail") == "me@gmail.com"
    assert m2.disconnect("gmail") and not m2.status("gmail")["connected"]


@pytest.mark.asyncio
async def test_finish_rejects_unknown_state(tmp_path):
    m = _mgr(tmp_path)
    with pytest.raises(ProviderError):
        await m.finish("bogus", "code")


@pytest.mark.asyncio
async def test_access_token_when_not_connected(tmp_path):
    m = _mgr(tmp_path)
    with pytest.raises(ConfigurationError):
        await m.access_token("gmail")


@pytest.mark.asyncio
async def test_token_request_error_message(tmp_path, monkeypatch):
    m = _mgr(tmp_path)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **k):
            super().__init__(*a, transport=httpx.MockTransport(handler), **{x: y for x, y in k.items() if x != "transport"})

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    _, state = m.start("outlook")
    with pytest.raises(ProviderError) as ei:
        await m.finish(state, "x")
    assert "Bad code" in ei.value.user_message


@pytest_asyncio.fixture()
async def client(svc):
    from app.main import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_oauth_routes(client, svc, monkeypatch):
    r = await client.get("/api/email/oauth/status")
    assert r.status_code == 200 and not r.json()["gmail"]["configured"]
    r = await client.post("/api/email/oauth/start", params={"provider": "gmail"})
    assert r.status_code == 400
    svc.oauth._google = ("gid", "gsec")
    r = await client.post("/api/email/oauth/start", params={"provider": "gmail", "open_browser": "false"})
    assert r.status_code == 200 and "accounts.google.com" in r.json()["url"]
    state = r.json()["state"]

    async def fake(url, data):
        return {"access_token": "a", "refresh_token": "r", "expires_in": 60, "id_token": _id_token("u@gmail.com")}

    monkeypatch.setattr(svc.oauth, "_token_request", fake)
    r = await client.get("/api/email/oauth/callback", params={"state": state, "code": "c"})
    assert r.status_code == 200 and "u@gmail.com" in r.text
    assert (await client.get("/api/email/oauth/status")).json()["gmail"]["connected"]
    r = await client.get("/api/email/oauth/callback", params={"state": "old", "code": "c"})
    assert r.status_code == 400
    assert (await client.post("/api/email/oauth/disconnect", params={"provider": "gmail"})).json()["disconnected"]


def test_email_provider_uses_oauth(tmp_path):
    from app.core.config import Settings
    from app.providers.email import GmailProvider, build_email_provider, build_oauth_manager

    s = Settings(_env_file=None, fs_allowed_roots="C:/tmp", email_provider="gmail", email_auth="oauth", google_client_id="x",
                 google_client_secret="y", data_dir=str(tmp_path))
    p = build_email_provider(s, build_oauth_manager(s))
    assert isinstance(p, GmailProvider) and p.uses_oauth
    s2 = Settings(_env_file=None, fs_allowed_roots="C:/tmp", email_provider="gmail", email_address="a@b.c", email_password="pw")
    assert not build_email_provider(s2, build_oauth_manager(s2)).uses_oauth
