"""Chatterbox: Zeta's local neural voice.

The model itself runs in `scripts/chatterbox_server.py` (its own virtualenv, its own
process, PyTorch + CUDA). This provider is the thin client: it POSTs text and the
delivery parameters chosen by the emotion engine, and gets WAV back.

If the server is not running and `CHATTERBOX_AUTOSTART` is on, the first request
starts it and waits for the model to load, so a fresh machine only needs
`install_tts.bat` once and then behaves like any other provider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from app.core.config import PROJECT_DIR
from app.core.exceptions import ConfigurationError, ProviderError
from app.providers.tts.base import TTSProvider

log = logging.getLogger(__name__)

SERVER = PROJECT_DIR / "scripts" / "chatterbox_server.py"
VENV_PYTHON = PROJECT_DIR / ".venv-tts" / "Scripts" / ("python.exe" if os.name == "nt" else "python")
VOICE_DIR = PROJECT_DIR / "voice"


class ChatterboxTTS(TTSProvider):
    name = "chatterbox"

    def __init__(self, base_url: str = "http://127.0.0.1:8766", voice: str = "", model: str = "turbo",
                 device: str = "auto", exaggeration: float = 0.5, cfg_weight: float = 0.5,
                 temperature: float = 0.8, autostart: bool = True, startup_timeout: float = 180.0,
                 idle_unload: float = 60.0):
        self.base_url = (base_url or "http://127.0.0.1:8766").rstrip("/")
        self.voice = voice
        self.model = model
        self.device = device
        self.exaggeration = exaggeration
        self.cfg_weight = cfg_weight
        self.temperature = temperature
        self.autostart = autostart
        self.startup_timeout = startup_timeout
        self.idle_unload = idle_unload
        self._process: Optional[subprocess.Popen] = None
        self._starting: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ delivery
    @property
    def expressive(self) -> bool:
        """Chatterbox performs [laugh], [sigh], [chuckle] and friends."""
        return True

    @property
    def speech_model(self) -> str:
        return "chatterbox"

    def _params(self, style: Any) -> Dict[str, float]:
        """Emotion -> generation knobs. Exaggeration is intensity; cfg_weight is pacing (lower = slower)."""
        p = getattr(style, "chatterbox", None) or {}
        return {"exaggeration": float(p.get("exaggeration", self.exaggeration)),
                "cfg_weight": float(p.get("cfg_weight", self.cfg_weight)),
                "temperature": float(p.get("temperature", self.temperature))}

    # ------------------------------------------------------------------ speaking
    async def synthesize(self, text: str, style: Any = None) -> Tuple[bytes, str]:
        from app.emotion.speech import prepare

        body = {"text": prepare(text, style, self.speech_model) if style is not None else text,
                "voice": self.voice, **self._params(style)}
        try:
            data = await self._post(body)
        except (httpx.ConnectError, httpx.ReadError):
            await self._ensure_server()
            data = await self._post(body)
        return data, "audio/wav"

    async def _post(self, body: Dict[str, Any]) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{self.base_url}/tts", json=body)
        except httpx.TimeoutException as e:
            raise ProviderError("chatterbox timed out",
                                user_message="The local voice took too long. Something else is probably using the "
                                             "GPU; try again, or set CHATTERBOX_DEVICE=cpu.") from e
        if r.status_code == 503:
            raise ProviderError(r.text[:200], user_message="The Chatterbox voice is still loading. Try again in a few seconds.")
        if r.status_code >= 400:
            raise ProviderError(r.text[:300], user_message=f"Chatterbox returned an error ({r.status_code}).")
        return r.content

    # ------------------------------------------------------------------ process
    async def _ensure_server(self) -> None:
        """Start the local model server once, and wait until it can actually speak."""
        if not self.autostart:
            raise ProviderError("chatterbox server unreachable",
                                user_message=f"The Chatterbox voice server is not running. Start it with start_tts.bat "
                                             f"(expected at {self.base_url}).")
        if not VENV_PYTHON.exists():
            raise ConfigurationError(f"The Chatterbox environment is missing ({VENV_PYTHON}). Run install_tts.bat once "
                                     f"to install it, or set TTS_PROVIDER to another voice.")
        if self._starting and not self._starting.done():
            await self._starting
            return
        self._starting = asyncio.get_running_loop().create_task(self._start_and_wait())
        await self._starting

    async def _start_and_wait(self) -> None:
        # Someone may already be serving this URL: a start_tts.bat window, or a server left
        # running from an earlier session. Use it rather than fighting over the port.
        mine = self._process is not None and self._process.poll() is None
        if not mine and not await self._reachable():
            cmd = [str(VENV_PYTHON), "-u", str(SERVER), "--model", self.model, "--device", self.device,
                   "--idle-unload", str(self.idle_unload)]
            if self.voice:
                cmd += ["--voice", self.voice]
            log.info("Starting the Chatterbox voice server: %s", " ".join(cmd[1:]))
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            log_path = PROJECT_DIR / "logs" / "chatterbox.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                sink = open(log_path, "ab", buffering=0)          # noqa: SIM115  (lives as long as the child)
            except OSError:
                sink = subprocess.DEVNULL                          # type: ignore[assignment]
            self._process = subprocess.Popen(cmd, cwd=str(PROJECT_DIR), stdout=sink, stderr=subprocess.STDOUT,
                                             creationflags=flags)
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            h = await self.health()
            if h.get("ok"):
                return
            if self._process is not None and self._process.poll() is not None and not await self._reachable():
                raise ProviderError("chatterbox server exited",
                                    user_message="The Chatterbox voice server stopped straight away. See "
                                                 "logs/chatterbox.log, or run start_tts.bat to watch it.")
            await asyncio.sleep(2.0)
        raise ProviderError("chatterbox startup timed out",
                            user_message="The Chatterbox voice did not finish loading in time. It is probably still "
                                         "downloading the model; try again shortly.")

    async def _reachable(self) -> bool:
        """Is anything answering on the voice URL, loaded or still loading?"""
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{self.base_url}/health")
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    async def release_gpu(self) -> bool:
        """Ask the voice to hand its VRAM back now, without waiting for the idle timer.

        On an 8 GB laptop card the voice (~2 GB) and a local 8B model do not both fit, and a
        model that spills onto the CPU runs about ten times slower. Zeta calls this before a
        long generation so the LLM gets the whole card.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self.base_url}/park")
            return bool(r.status_code < 400 and r.json().get("parked"))
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        """Only stops a server this process started; a manually launched one is left alone."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._process = None

    # ------------------------------------------------------------------ info
    async def health(self) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/health")
            h = r.json()
        except Exception:  # noqa: BLE001
            started = self._process is not None and self._process.poll() is None
            return {"ok": False, "detail": "loading…" if started else f"server not running at {self.base_url} "
                                                                      f"(start_tts.bat)"}
        if not h.get("ok"):
            return {"ok": False, "detail": h.get("error") or "model loading"}
        where = h.get("device")
        if h.get("parked"):
            where = f"{where}, parked in RAM"      # the VRAM is currently the LLM's
        return {"ok": True, "detail": f"{h.get('model')} on {where}, voice {h.get('voice')}", **h}

    async def voices(self) -> list:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/voices")
            names = r.json().get("voices", [])
        except Exception:  # noqa: BLE001
            names = sorted(p.name for p in VOICE_DIR.glob("*.wav")) if VOICE_DIR.exists() else []
        return [{"id": n, "name": Path(n).stem.replace("_", " ")} for n in names] or [{"id": "", "name": "built-in voice"}]
