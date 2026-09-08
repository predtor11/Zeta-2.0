"""ElevenLabs STT provider, open_file/open_folder name lookup, WhatsApp send-by-name fallback."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import STTProviderName, Settings
from app.providers.stt import ElevenLabsSTT, build_stt_provider


# ----------------------------------------------------------------- STT
@pytest.mark.asyncio
async def test_elevenlabs_stt_posts_multipart_and_returns_text(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        body = request.content
        seen["has_model"] = b"scribe_v1" in body and b'name="file"' in body
        return httpx.Response(200, json={"text": " Hello Zeta, open my documents. ", "language_code": "en"})

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    stt = ElevenLabsSTT("sk_test", "small")  # whisper-style size names are ignored -> scribe_v1
    text = await stt.transcribe(b"RIFF....", "audio/webm")
    assert text == "Hello Zeta, open my documents."
    assert seen["url"].endswith("/v1/speech-to-text") and seen["key"] == "sk_test" and seen["has_model"]


@pytest.mark.asyncio
async def test_elevenlabs_stt_permission_error_is_explained(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": {"status": "missing_permissions"}})

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    from app.core.exceptions import ProviderError

    with pytest.raises(ProviderError) as ei:
        await ElevenLabsSTT("sk_test").transcribe(b"x", "audio/wav")
    assert "Speech to Text permission" in ei.value.user_message


def test_stt_elevenlabs_config(sandbox, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_abc")
    s = Settings()
    assert s.stt_provider == STTProviderName.ELEVENLABS
    assert build_stt_provider(s).name == "elevenlabs"
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    with pytest.raises(Exception):
        Settings()


# ----------------------------------------------------------------- open by name
@pytest.mark.asyncio
async def test_open_file_by_bare_name_locates_it(svc, sandbox, monkeypatch):
    opened = []
    import app.tools.filesystem as fs

    monkeypatch.setattr(fs.os, "startfile", lambda p: opened.append(p), raising=False)
    tool = svc.registry.get("open_file")
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id="t", conversation_id=None,
                      services=svc.tool_services(), emit=lambda *a, **k: None)
    res = await tool.run({"path": "bus_schedule_notes"}, ctx)
    assert res.success, res.error
    assert opened and opened[0].endswith("bus_schedule_notes.txt")

    res = await tool.run({"path": "resume_2025.md"}, ctx)
    assert res.success and opened[-1].endswith("resume_2025.md")

    res = await tool.run({"path": "does_not_exist_anywhere.docx"}, ctx)
    assert not res.success and "couldn't find" in res.error


@pytest.mark.asyncio
async def test_open_folder_by_name_and_ambiguity(svc, sandbox, monkeypatch):
    launched = []
    import app.tools.filesystem as fs

    monkeypatch.setattr(fs.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    (sandbox / "Documents" / "Games").mkdir()
    (sandbox / "Downloads" / "Games").mkdir()
    (sandbox / "Music").mkdir()
    await svc.file_index.scan()
    tool = svc.registry.get("open_folder")
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id="t", conversation_id=None,
                      services=svc.tool_services(), emit=lambda *a, **k: None)
    res = await tool.run({"path": "Music"}, ctx)
    assert res.success and str(sandbox / "Music") in " ".join(launched[-1])
    res = await tool.run({"path": "Games"}, ctx)
    assert not res.success and "Several matches" in res.error and "Downloads" in res.error and "Documents" in res.error


@pytest.mark.asyncio
async def test_open_outside_roots_still_denied(svc, sandbox):
    tool = svc.registry.get("open_file")
    from app.core.exceptions import PathNotAllowed
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id="t", conversation_id=None,
                      services=svc.tool_services(), emit=lambda *a, **k: None)
    with pytest.raises(PathNotAllowed):
        await tool.run({"path": "C:\\Windows\\System32\\drivers\\etc\\hosts"}, ctx)


# ----------------------------------------------------------------- WhatsApp by name
class FakeWeb:
    name = "whatsapp_web"
    supports_name_lookup = True

    def __init__(self):
        self.sent = []

    async def send_message(self, phone, text):
        self.sent.append(("phone", phone, text))
        return {"sent": True, "phone": phone}

    async def send_to_name(self, name, text):
        if name.lower() == "nobody":
            from app.core.exceptions import ProviderError

            raise ProviderError("no chat", user_message=f"I couldn't find a WhatsApp chat or contact named '{name}'. Please give me the phone number.")
        self.sent.append(("name", name, text))
        return {"sent": True, "chat": name.title()}


@pytest.mark.asyncio
async def test_send_whatsapp_falls_back_to_chat_search(svc):
    fake = FakeWeb()
    svc.messaging = fake
    services = svc.tool_services()
    tool = svc.registry.get("send_whatsapp_message")
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id="t", conversation_id=None,
                      services=services, emit=lambda *a, **k: None)
    prev = await tool.preview({"to": "arnish", "message": "bhai kaisa hai"}, ctx)
    assert "WhatsApp chat search" in prev["recipient"]
    res = await tool.run({"to": "arnish", "message": "bhai kaisa hai"}, ctx)
    assert res.success and fake.sent == [("name", "arnish", "bhai kaisa hai")] and res.output["recipient"] == "Arnish"

    res = await tool.run({"to": "nobody", "message": "hi"}, ctx)
    assert not res.success and "phone number" in res.error

    # contact book still wins when it has the person
    svc.contacts.add("Arnish", "+91 98765 43210")
    res = await tool.run({"to": "arnish", "message": "second"}, ctx)
    assert res.success and fake.sent[-1][0] == "phone" and fake.sent[-1][1].endswith("9876543210")


@pytest.mark.asyncio
async def test_send_whatsapp_without_name_lookup_asks_for_number(svc):
    class Business:
        name = "whatsapp_business"
        supports_name_lookup = False

        async def send_message(self, phone, text):
            return {"sent": True}

    svc.messaging = Business()
    tool = svc.registry.get("send_whatsapp_message")
    from app.tools.base import ToolContext

    ctx = ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id="t", conversation_id=None,
                      services=svc.tool_services(), emit=lambda *a, **k: None)
    res = await tool.run({"to": "someone new", "message": "hi"}, ctx)
    assert not res.success and "phone number" in res.error
