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
from app.core import gpu, metrics
from app.voice.wakeword import WakeWordService

VOICE_VRAM_MB = 2400    # fallback for a voice that does not report its own footprint
DESKTOP_VRAM_MB = 800   # what Windows, the browser and a live wallpaper take before Zeta starts
SMALL_GPU_MB = 12288    # fallback guess before the model's real size is known
PARK_ATTEMPTS = 8       # a sentence takes a few seconds; wait it out rather than load into a full card
PARK_RETRY_SECONDS = 2.0

log = logging.getLogger(__name__)

STOP_RE = re.compile(r"^\s*(?:hey\s+)?(?:zeta[,!.\s]*)?(?:please\s+)?(stop|cancel|abort|halt|never\s*mind)(?:\s+(?:that|it|everything|the task))?[.!\s]*$", re.I)


class ZetaServices:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.started = False
        self._llm_vram_mb = 0        # measured footprint of the language model; 0 until it loads once
        self._rewarm: Optional[asyncio.Task] = None
        # One owner of the graphics card at a time. Without it, a queued spoken reply can resume
        # the voice in the middle of a turn's model load - both end up half on the CPU.
        self._gpu_lock = asyncio.Lock()
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

    # ------------------------------------------------------------------ sharing one GPU
    async def balance_gpu(self, want: str) -> bool:
        """Make room on the GPU for whichever of the two big models is about to run.

        Ollama needs about 6 GB for an 8B model and Chatterbox about 2 GB. On an 8 GB laptop
        card they do not both fit, and neither of them says so: Ollama silently moves layers to
        the CPU (ten times slower, which is where the timeouts came from) and Chatterbox falls
        back to CPU generation. Since a reply is generated and *then* spoken, they never
        actually need the card at the same time - so Zeta hands it over explicitly.

        Only small cards pay for this. With plenty of VRAM both models stay resident.
        Returns whether the card is now clear for `want` - the caller should not load a model
        into VRAM that something else is still holding.
        """
        if not self._gpu_shared():
            return True                       # both fit: nothing to hand over
        try:
            if want == "llm":
                return await self._park_voice()
            elif want == "voice" and not self.tasks.active():
                # Measured on an RTX 4070 Laptop: speaking with the language model still resident
                # takes 57s for a 5s clip, because Chatterbox spills into shared memory. With the
                # model out of the way the same clip takes 8s. Reloading it on the next turn costs
                # two or three seconds, so this is worth doing whenever the card is this small -
                # not only once free VRAM has already run out. Never mid-turn, though: a running
                # task will want the model again in a moment.
                unload = getattr(self.llm, "unload", None)
                if unload and await unload():
                    log.info("Language model unloaded so the voice can speak on the GPU "
                             "(it reloads on the next turn).")
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("gpu handover (%s) failed: %s", want, e)
            return False

    async def _park_voice(self) -> bool:
        """Get the voice off the GPU, waiting out a sentence it is in the middle of.

        Loading the language model while the voice still holds its ~2 GB is the whole failure
        this class of bug is about: it does not error, it just puts a slice of the model on the
        CPU and takes minutes. Better to wait a few seconds for a sentence to finish.
        """
        release = getattr(self.tts, "release_gpu", None)
        if not release:
            return True                       # a voice that owns no VRAM (ElevenLabs, Windows TTS)
        for _ in range(PARK_ATTEMPTS):
            state = await release()
            if isinstance(state, bool):       # a provider with the older signature
                return state
            if state.get("parked"):
                log.info("Voice parked so the language model gets the whole GPU.")
                return True
            if state.get("free"):
                return True                   # already parked, or never on the GPU
            if not state.get("busy"):
                return False
            await asyncio.sleep(PARK_RETRY_SECONDS)   # mid-sentence: it will be done shortly
        log.warning("The voice is still using the GPU; the language model may not fit.")
        return False

    def _gpu_shared(self) -> bool:
        """Should Zeta arbitrate the GPU, or can both models simply stay resident?

        Handing the card back and forth costs a model reload on the next turn, so it is only
        worth doing when the two genuinely do not fit together. Rather than guess from the card
        size, this compares the language model's measured footprint (whatever model is
        configured) against the card, and a smaller model turns the whole mechanism off by
        itself - which is the better fix, when it is available.
        """
        mem = gpu.gpu_memory()
        return self.gpu_shared_for(mem[0] if mem else None)

    def gpu_shared_for(self, total_mb: Optional[float]) -> bool:
        """The same decision, given a card size someone has already measured.

        Kept separate because the monitoring endpoint asks this on every poll, and spawning
        nvidia-smi on the event loop to re-measure something it just read cost seconds on a
        machine that was short of memory.
        """
        mode = (self.settings.gpu_share or "auto").lower()
        if mode in ("off", "false", "no"):
            return False
        if not total_mb:
            return False                     # no NVIDIA GPU: nothing to hand over
        if not hasattr(self.llm, "unload"):
            return False                     # a cloud model holds no VRAM; parking the voice
                                             # before every turn would cost a reload for nothing
        if mode == "on":
            return True
        if not self._llm_vram_mb:            # not measured yet: fall back to the card size
            return total_mb < SMALL_GPU_MB
        voice_mb = getattr(self.tts, "vram_mb", VOICE_VRAM_MB)
        return total_mb < self._llm_vram_mb + voice_mb + DESKTOP_VRAM_MB

    async def synthesize(self, text: str, lead: bool = True, final: bool = True):
        """Speak `text` in the current emotional delivery, GPU handover included.

        The card is held for the whole generation: a turn starting underneath this would load its
        model into VRAM the voice is about to take back.

        A long reply is spoken in pieces (see `/api/voice/speak/plan`). `lead` marks the first
        piece - only that one opens with a breath or a sigh - and `final` marks the last, which
        is the only point at which the language model is worth putting back on the card. Without
        that, a six-sentence answer would park and reload the model six times.
        """
        style = self.speech_style(text)
        async with self._gpu_lock:
            await self.balance_gpu("voice")
            audio = await self.tts.synthesize(text, style, lead)
        if final:
            self._rewarm_llm_soon()
        return audio

    def _rewarm_llm_soon(self) -> None:
        """Put the language model back on the GPU while the reply is still being listened to.

        Speaking evicts it, so without this the *next* thing the person says waits 15-20 s for a
        reload. The audio has just been generated but not yet played, which is several seconds of
        the person's attention going spare - exactly enough to load a model in. If they interrupt
        during it, the reload was going to happen anyway.
        """
        if not (self._gpu_shared() and self.started):
            return
        if self._rewarm and not self._rewarm.done():
            return
        warm = getattr(self.llm, "warm", None)
        if not warm:
            return

        async def run() -> None:
            try:
                async with self._gpu_lock:
                    if not await self.balance_gpu("llm"):
                        return      # the voice is still speaking; the next turn will try again
                    self._note_llm_size(await warm())
            except Exception as e:  # noqa: BLE001
                log.debug("could not re-warm the language model: %s", e)

        try:
            self._rewarm = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            pass

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
                # The voice loads first and parks itself straight afterwards, so the language
                # model - loaded next, and much the larger of the two - finds the card empty.
                # Getting this order wrong is what leaves one of them stranded on the CPU.
                await self.balance_gpu("voice")
                await self.tts._ensure_server()          # type: ignore[attr-defined]
                log.info("Local voice ready.")
        except Exception as e:  # noqa: BLE001
            log.warning("could not start the local voice: %s", str(e)[:160])
        await self._warm_llm()

    async def _warm_llm(self) -> None:
        """Load the language model before the first turn needs it, and check where it landed.

        Ollama loads on first use, which puts 10-30 seconds of loading inside the first reply.
        Worse, if the model does not fit it runs partly on the CPU without saying so - so this
        also reports the split, which is the one number that explains a slow local model.
        """
        warm = getattr(self.llm, "warm", None)
        if not warm:
            return
        try:
            async with self._gpu_lock:
                await self.balance_gpu("llm")
                placement = await warm()
        except Exception as e:  # noqa: BLE001
            log.debug("could not preload the language model: %s", e)
            return
        self._note_llm_size(placement)
        if placement.get("on_cpu"):
            log.warning("%s does not fit on the GPU: %d MB of %d MB is running on the CPU, which makes replies "
                        "roughly ten times slower. Free VRAM, or lower LLM_CONTEXT_LENGTH.",
                        self.settings.llm_model, placement["cpu_mb"], placement["total_mb"])
        elif placement.get("vram_mb"):
            log.info("%s loaded: %d MB on the GPU, %d MB VRAM free.%s", self.settings.llm_model,
                     placement["vram_mb"], gpu.free_mb(),
                     "" if self._gpu_shared() else " The voice fits alongside it, so both stay loaded.")

    def _note_llm_size(self, placement: Dict[str, Any]) -> None:
        """Remember how big this model really is; it decides whether the handover is needed."""
        size = int(placement.get("total_mb") or 0)
        if size:
            self._llm_vram_mb = max(self._llm_vram_mb, size)

    async def stop(self) -> None:
        self.tasks.cancel_all("server shutting down")
        self.confirmations.cancel_all()
        if self._rewarm and not self._rewarm.done():
            self._rewarm.cancel()
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
        metrics.sampler.stop()
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
        async with self._gpu_lock:
            await self.balance_gpu("llm")
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
            "gpu": await self._gpu_status(),
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

    async def _gpu_status(self) -> Dict[str, Any]:
        """VRAM, and whether the language model is really on it. Absent when there is no GPU."""
        mem = gpu.gpu_memory()
        if not mem:
            return {"present": False}
        total, free = mem
        out: Dict[str, Any] = {"present": True, "total_mb": total, "free_mb": free, "shared": self._gpu_shared()}
        placement = getattr(self.llm, "placement", None)
        if placement:
            try:
                out["llm"] = await placement()
                self._note_llm_size(out["llm"])
            except Exception:  # noqa: BLE001
                pass
        return out

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
