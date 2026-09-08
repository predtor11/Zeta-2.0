"""ZetaServices - the composition root.

Builds every component from Settings, wires them together, and exposes the
few operations the API layer needs (submit a request, status, reload).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app import __version__
from app.agent.orchestrator import Orchestrator
from app.core import database
from app.core.config import BACKEND_DIR, PROJECT_DIR, Settings, get_settings, reload_settings
from app.core.events import event_bus
from app.core.logging import configure_logging
from app.emotion import speech as speech_style
from app.emotion.engine import EmotionEngine
from app.memory.long_term import LongTermMemory
from app.memory.short_term import ShortTermMemory
from app.plugins.loader import PluginManager
from app.providers.email import build_email_provider, build_oauth_manager
from app.providers.llm.registry import build_embedding_provider, build_llm_provider
from app.providers.messaging import ContactBook, build_messaging_provider
from app.providers.stt import build_stt_provider
from app.providers.tts import build_tts_provider
from app.scheduler.service import LocalScheduler, notify_reminder
from app.security.confirmation import ConfirmationManager
from app.security.permissions import PermissionManager
from app.security.secrets import SecretStore
from app.tasks.manager import Task, TaskManager
from app.tools.base import ToolRegistry
from app.tools.browser.service import BrowserService
from app.tools.filesystem.index import FileIndex
from app.tools.terminal import TerminalService
from app.voice.wakeword import WakeWordService

log = logging.getLogger(__name__)

STOP_RE = re.compile(r"^\s*(?:hey\s+)?(?:zeta[,!.\s]*)?(?:please\s+)?(stop|cancel|abort|halt|never\s*mind)(?:\s+(?:that|it|everything|the task))?[.!\s]*$", re.I)


class ZetaServices:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.started = False
        self.audio_cache: Dict[str, Tuple[bytes, str]] = {}
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        s = self.settings
        configure_logging(s.logs_dir, s.log_level, s.secret_values())
        self.secrets = SecretStore(s)
        self.permissions = PermissionManager.from_files(BACKEND_DIR / "config" / "permissions.yaml",
                                                        BACKEND_DIR / "config" / "permissions.local.yaml")
        self.confirmations = ConfirmationManager(s.confirmation_timeout_seconds)
        self.tasks = TaskManager()
        self.registry = ToolRegistry()
        self.plugins = PluginManager(self.registry, s, PROJECT_DIR / "plugins")
        self.plugins.load_builtin()
        self.plugins.load_external()

        self.llm = build_llm_provider(s)
        self.embedder = build_embedding_provider(s)
        self.short_term = ShortTermMemory(s.agent_history_messages)
        self.long_term = LongTermMemory(self.embedder, s.embedding_model)
        self.file_index = FileIndex(s.data_dir / "file_index.db", s.index_roots, s.index_excludes, index_content=s.fs_index_content,
                                    content_max_bytes=s.fs_index_content_max_bytes, embedder=self.embedder, embedding_model=s.embedding_model)
        self.browser = BrowserService(s.browser_profile_dir, s.browser_headless, s.browser_download_dir)
        self.terminal = TerminalService()
        self.contacts = ContactBook(s.contacts_file)
        try:
            self.messaging = build_messaging_provider(s, self.browser)
        except Exception as e:  # noqa: BLE001
            log.error("messaging provider failed to build: %s", e)
            from app.providers.messaging import DisabledMessaging

            self.messaging = DisabledMessaging()
        self.oauth = build_oauth_manager(s)
        self.email = build_email_provider(s, self.oauth)
        self.stt = build_stt_provider(s)
        self.tts = build_tts_provider(s)
        self.scheduler = LocalScheduler(self._run_scheduled_job)
        self.emotion = EmotionEngine(enabled=s.emotion_enabled, use_prosody=s.emotion_prosody, region=s.emotion_region,
                                     support_mode=s.emotion_support_mode)
        self.wake = self._build_wake(s)
        self.orchestrator = Orchestrator(settings=s, llm=self.llm, registry=self.registry, permissions=self.permissions,
                                         confirmations=self.confirmations, tasks=self.tasks, short_term=self.short_term,
                                         long_term=self.long_term, secrets=self.secrets, services=self.tool_services(),
                                         emotion=self.emotion, speech_note=self._speech_note())

    def _speech_note(self) -> str:
        """Tell the model about expressive delivery only when the configured voice can perform it."""
        if not (self.settings.emotion_enabled and self.settings.emotion_adapt_voice and getattr(self.tts, "expressive", False)):
            return ""
        return speech_style.prompt_note(getattr(self.tts, "speech_model", ""))

    def speech_style(self, reply: str = ""):
        """Delivery for the next spoken reply, from how the person currently seems."""
        return speech_style.style_for(self.emotion.current if self.settings.emotion_enabled else None,
                                      reply, enabled=self.settings.emotion_adapt_voice)

    def _build_wake(self, s: Settings) -> WakeWordService:
        return WakeWordService(enabled=bool(s.wake_word_enabled and s.is_local), phrase=s.wake_word, engine=s.wake_word_engine,
                               model=s.wake_word_model, sensitivity=s.wake_word_sensitivity, device=s.wake_word_device,
                               on_wake=self.on_wake_word)

    def on_wake_word(self, engine: str, text: str) -> None:
        """Called from the listener thread: hand over to the event loop and notify the UI."""
        loop = getattr(self, "_loop", None)

        def _publish() -> None:
            event_bus.publish("wake_word", engine=engine, text=text, phrase=self.settings.wake_word)
            event_bus.activity(f"Wake word heard ({engine}): {text}")

        if loop and loop.is_running():
            loop.call_soon_threadsafe(_publish)
        else:
            _publish()

    def tool_services(self) -> Dict[str, Any]:
        return {
            "tasks": self.tasks, "llm": self.llm, "long_term_memory": self.long_term, "short_term_memory": self.short_term,
            "file_index": self.file_index, "browser": self.browser, "terminal": self.terminal, "contacts": self.contacts,
            "messaging": self.messaging, "email": self.email, "emotion": self.emotion, "oauth": self.oauth, "stt": self.stt, "tts": self.tts, "scheduler": self.scheduler,
            "permissions": self.permissions, "services": self,
        }

    # ------------------------------------------------------------------
    async def start(self) -> None:
        s = self.settings
        database.init_engine(s.database_url, echo=False)
        await database.create_all()
        await self.scheduler.start()
        self._loop = asyncio.get_running_loop()
        if s.fs_index_on_startup and s.is_local:
            self.file_index.start_background_scan()
        self.wake.start()
        asyncio.create_task(self._warm_voice())
        self.started = True
        health = await self.llm.health()
        log.info("Zeta %s started in %s mode. LLM=%s/%s (%s). %d tools from %d plugins.", __version__, s.zeta_mode.value,
                 s.llm_provider.value, s.llm_model, health.get("detail"), len(self.registry), len(self.plugins.plugins))
        event_bus.activity(f"Zeta online. LLM: {s.llm_provider.value} ({s.llm_model}) - {health.get('detail')}")

    async def _warm_voice(self) -> None:
        """Load the speech models in the background, so the first spoken turn is not the slow one.

        Whisper takes a few seconds to load and Chatterbox a good deal longer; doing it at start-up
        costs nothing the user waits for.
        """
        try:
            if hasattr(self.stt, "_load"):
                await asyncio.get_running_loop().run_in_executor(None, self.stt._load)
                log.info("Speech recognition ready (%s on %s).", self.settings.stt_model, getattr(self.stt, "active_device", "?"))
        except Exception as e:  # noqa: BLE001
            log.warning("could not preload the speech model: %s", str(e)[:160])
        try:
            h = await self.tts.health()
            if not h.get("ok") and getattr(self.tts, "autostart", False):
                await self.tts._ensure_server()          # type: ignore[attr-defined]
                log.info("Local voice ready.")
        except Exception as e:  # noqa: BLE001
            log.warning("could not start the local voice: %s", str(e)[:160])

    async def stop(self) -> None:
        self.tasks.cancel_all("server shutting down")
        self.confirmations.cancel_all()
        self.wake.stop()
        try:
            self.tts.stop()          # stops a voice server this process started
        except Exception:  # noqa: BLE001
            pass
        await self.emotion.drain()
        await self.scheduler.stop()
        await self.browser.close()
        try:
            await self.llm.close()
        except Exception:  # noqa: BLE001
            pass
        self.file_index.close()
        await database.dispose()
        self.started = False

    async def reload(self) -> None:
        """Rebuild providers/permissions after the setup wizard changed `.env`."""
        self.settings = reload_settings()
        s = self.settings
        configure_logging(s.logs_dir, s.log_level, s.secret_values())
        try:
            await self.llm.close()
        except Exception:  # noqa: BLE001
            pass
        self.llm = build_llm_provider(s)
        self.embedder = build_embedding_provider(s)
        self.long_term = LongTermMemory(self.embedder, s.embedding_model)
        self.permissions = PermissionManager.from_files(BACKEND_DIR / "config" / "permissions.yaml",
                                                        BACKEND_DIR / "config" / "permissions.local.yaml")
        self.secrets = SecretStore(s)
        self.stt = build_stt_provider(s)
        self.tts = build_tts_provider(s)
        self.oauth = build_oauth_manager(s)
        self.email = build_email_provider(s, self.oauth)
        try:
            self.messaging = build_messaging_provider(s, self.browser)
        except Exception:  # noqa: BLE001
            from app.providers.messaging import DisabledMessaging

            self.messaging = DisabledMessaging()
        self.file_index.roots = list(s.index_roots)
        self.emotion.enabled = s.emotion_enabled
        self.emotion.use_prosody = s.emotion_prosody
        self.emotion.region = s.emotion_region
        self.emotion.support_mode = s.emotion_support_mode
        self.wake.stop()
        self.wake = self._build_wake(s)
        self.wake.start()
        self.orchestrator = Orchestrator(settings=s, llm=self.llm, registry=self.registry, permissions=self.permissions,
                                         confirmations=self.confirmations, tasks=self.tasks, short_term=self.short_term,
                                         long_term=self.long_term, secrets=self.secrets, services=self.tool_services(),
                                         emotion=self.emotion, speech_note=self._speech_note())
        event_bus.activity(f"Configuration reloaded. LLM: {s.llm_provider.value} ({s.llm_model})")

    # ------------------------------------------------------------------
    async def submit(self, message: str, conversation_id: Optional[str] = None, *, wait: bool = False,
                     timeout: Optional[float] = None) -> Task:
        """Create a task for a user message and run it in the background (or wait)."""
        if STOP_RE.match(message) and self.tasks.active():
            n = self.tasks.cancel_all("stopped by user")
            self.confirmations.cancel_all()
            task = self.tasks.create(message, conversation_id)
            from app.tasks.manager import TaskStatus

            reply = f"Stopped {n} running task{'s' if n != 1 else ''}."
            if conversation_id:
                await self.short_term.append(conversation_id, "user", message, task_id=task.id)
                await self.short_term.append(conversation_id, "assistant", reply, task_id=task.id)
            self.tasks.set_status(task, TaskStatus.COMPLETED, result=reply, message=reply)
            event_bus.publish("assistant_message", task_id=task.id, conversation_id=conversation_id, content=reply)
            return task
        task = self.tasks.create(message, conversation_id)
        self.tasks.start(task, self.orchestrator.run(task, message))
        if wait:
            await self.tasks.wait(task, timeout=timeout or (self.settings.confirmation_timeout_seconds + self.settings.llm_timeout_seconds * 3))
        return task

    async def _run_scheduled_job(self, job) -> None:
        if job.kind == "reminder":
            notify_reminder(job)
            return
        task = await self.submit(job.payload, None, wait=True, timeout=600)
        event_bus.publish("notification", title=f"Scheduled: {job.name}", message=task.result or task.error or "(no result)",
                          job_id=job.id, task_id=task.id, level="info")

    # ------------------------------------------------------------------
    async def status(self) -> Dict[str, Any]:
        s = self.settings
        llm_health = await self.llm.health()
        internet = await asyncio.get_running_loop().run_in_executor(None, _internet_ok)
        stt_h, tts_h = await self.stt.health(), await self.tts.health()
        return {
            "name": s.zeta_name, "version": __version__, "online": True, "mode": s.zeta_mode.value,
            "llm": {"provider": s.llm_provider.value, "model": s.llm_model, "ok": llm_health.get("ok", False),
                    "detail": llm_health.get("detail", ""), "supports_tools": self.llm.supports_tools,
                    "models": llm_health.get("models", [])[:50]},
            "voice": {"stt": s.stt_provider.value, "tts": s.tts_provider.value, "stt_ok": stt_h.get("ok", False),
                      "tts_ok": tts_h.get("ok", False), "detail": f"STT {stt_h.get('detail')}; TTS {tts_h.get('detail')}",
                      "wake": self.wake.status()},
            "database": {**database.describe_database_url(s.database_url), **(await database.ping())},
            "emotion": {**self.emotion.status(), "expressive_voice": bool(getattr(self.tts, "expressive", False)),
                        "adapt_voice": s.emotion_adapt_voice},
            "internet": {"connected": internet},
            "computer": {"connected": s.is_local, "index": self.file_index.status(), "browser": self.browser.health()},
            "tools": len(self.registry.enabled(self.permissions, cloud=not s.is_local)),
            "plugins": [p.info() for p in self.plugins.plugins.values()],
            "active_tasks": len(self.tasks.active()),
            "pending_confirmations": len(self.confirmations.pending()),
            "setup_complete": (PROJECT_DIR / ".env").exists() or (BACKEND_DIR / ".env").exists(),
            "config": s.public_summary(),
        }

    def cache_audio(self, data: bytes, mime: str) -> str:
        aid = uuid.uuid4().hex[:12]
        self.audio_cache[aid] = (data, mime)
        if len(self.audio_cache) > 50:
            for k in list(self.audio_cache)[:-50]:
                self.audio_cache.pop(k, None)
        return aid


def _internet_ok() -> bool:
    import socket

    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            return True
    except OSError:
        return False


_services: Optional[ZetaServices] = None


def get_services() -> ZetaServices:
    global _services
    if _services is None:
        _services = ZetaServices()
    return _services


def set_services(svc: Optional[ZetaServices]) -> None:
    global _services
    _services = svc
