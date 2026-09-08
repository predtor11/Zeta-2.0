"""Shared fixtures: an isolated ZetaServices with the mock LLM, temp data dir and temp allowed roots."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Temp home-like folder with a few files, exposed as the only allowed root."""
    root = tmp_path / "home"
    (root / "Documents").mkdir(parents=True)
    (root / "Downloads").mkdir()
    (root / "Documents" / "bus_schedule_notes.txt").write_text("Multiline bus scheduling documentation for the UBA project.", encoding="utf-8")
    (root / "Documents" / "resume_2025.md").write_text("# Resume\nSenior engineer.", encoding="utf-8")
    (root / "Downloads" / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "Downloads" / "big.bin").write_bytes(b"0" * 200_000)
    for key in list(os.environ):
        if key.startswith(("LLM_", "ZETA_", "FS_", "TTS_", "STT_", "WHATSAPP_", "EMAIL_", "WAKE_", "EMOTION_", "DATA_DIR", "LOGS_DIR", "DATABASE_URL")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WAKE_WORD_ENABLED", "false")
    # Explicit overrides so the project's real .env (providers, API keys) never leaks into tests.
    monkeypatch.setenv("ZETA_MODE", "local")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_MODEL", "mock")
    for key, val in {"TTS_PROVIDER": "disabled", "STT_PROVIDER": "disabled", "VOICE_PROVIDER": "", "ELEVENLABS_API_KEY": "",
                     "EMAIL_PROVIDER": "", "EMAIL_AUTH": "password", "EMAIL_ADDRESS": "", "EMAIL_PASSWORD": "",
                     "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "MICROSOFT_CLIENT_ID": "", "WHATSAPP_PROVIDER": "",
                     "EMBEDDING_PROVIDER": "", "LLM_VISION_MODEL": "", "API_TOKEN": "", "TERMINAL_CWD": "",
                     "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GITHUB_TOKEN": ""}.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("FS_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("FS_INDEX_ROOTS", str(root))
    monkeypatch.setenv("FS_INDEX_ON_STARTUP", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CONFIRMATION_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CONTACTS_FILE", str(tmp_path / "contacts.json"))
    return root


@pytest_asyncio.fixture()
async def svc(sandbox):
    from app.core.config import reload_settings
    from app.services import ZetaServices, set_services

    reload_settings()
    services = ZetaServices()
    set_services(services)
    await services.start()
    yield services
    await services.stop()
    set_services(None)


@pytest.fixture()
def llm(svc):
    return svc.llm
