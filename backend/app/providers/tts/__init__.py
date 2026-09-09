"""Text-to-speech providers.

    VoiceProvider (TTSProvider)
    ├── ChatterboxTTS   Resemble AI's Chatterbox, running locally on your GPU (default)
    ├── LocalTTS        Windows SAPI via PowerShell System.Speech - zero dependencies
    ├── PiperTTS        piper executable (fast neural local voices)
    ├── ElevenLabsTTS   ElevenLabs API
    └── DisabledTTS

`synthesize()` returns (audio_bytes, mime_type).  Nothing here is hard-coded
to a vendor; TTS_PROVIDER selects the implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Tuple

import httpx

from app.core.config import Settings, TTSProviderName
from app.core.exceptions import ConfigurationError, ProviderError
from app.providers.tts.base import TTSProvider
from app.providers.tts.chatterbox import ChatterboxTTS

log = logging.getLogger(__name__)


class DisabledTTS(TTSProvider):
    name = "disabled"

    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        raise ConfigurationError("Text-to-speech is disabled. Set TTS_PROVIDER=chatterbox, local, piper or elevenlabs.")

    async def health(self) -> Dict[str, Any]:
        return {"ok": False, "detail": "disabled"}


class LocalTTS(TTSProvider):
    """Windows built-in voices through System.Speech (PowerShell). No pip dependencies."""

    name = "local"

    def __init__(self, voice: str = "", rate: int = 0):
        self.voice = voice
        self.rate = max(-10, min(10, rate))

    def _script(self, text_file: str, out_file: str, rate: int = 0) -> str:
        voice_line = f'try {{ $s.SelectVoice("{self.voice}") }} catch {{ }}' if self.voice else ""
        return (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{voice_line} $s.Rate = {rate}; "
            f"$t = Get-Content -Raw -Encoding UTF8 '{text_file}'; "
            f"$s.SetOutputToWaveFile('{out_file}'); $s.Speak($t); $s.Dispose()"
        )

    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        from app.emotion.speech import for_voice

        text = for_voice(text)
        # Windows voices carry no emotion, but pace does a surprising amount of the work.
        rate = self.rate + {"gentle": -2, "steady": -2, "delighted": 2, "bright": 1, "playful": 1}.get(getattr(style, "name", ""), 0)
        rate = max(-10, min(10, rate))
        if sys.platform != "win32":
            raise ConfigurationError("LocalTTS uses Windows System.Speech; use piper or elevenlabs on other platforms.")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write(text)
            text_file = tf.name
        out_file = text_file.replace(".txt", ".wav")

        def _run():
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", self._script(text_file, out_file, rate)],
                                   capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.exists(out_file):
                    raise ProviderError(r.stderr.decode(errors="ignore")[:300], user_message="Windows speech synthesis failed.")
                return Path(out_file).read_bytes()
            finally:
                for f in (text_file, out_file):
                    try:
                        os.unlink(f)
                    except OSError:
                        pass

        data = await asyncio.get_running_loop().run_in_executor(None, _run)
        return data, "audio/wav"

    async def voices(self) -> list:
        if sys.platform != "win32":
            return []

        def _run():
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | % { $_.VoiceInfo.Name }"],
                               capture_output=True, timeout=30)
            return [l.strip() for l in r.stdout.decode(errors="ignore").splitlines() if l.strip()]

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception:  # noqa: BLE001
            return []

    async def health(self) -> Dict[str, Any]:
        return {"ok": sys.platform == "win32", "detail": "Windows System.Speech" + (f" ({self.voice})" if self.voice else "")}


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, executable: str = "piper", model_path: str = ""):
        self.executable = executable
        self.model_path = model_path

    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        from app.emotion.speech import for_voice

        text = for_voice(text)
        if not self.model_path:
            raise ConfigurationError("Set TTS_VOICE to the path of a piper .onnx voice model.")
        out_file = tempfile.mktemp(suffix=".wav")

        def _run():
            try:
                r = subprocess.run([self.executable, "--model", self.model_path, "--output_file", out_file],
                                   input=text.encode("utf-8"), capture_output=True, timeout=120)
                if r.returncode != 0:
                    raise ProviderError(r.stderr.decode(errors="ignore")[:300], user_message="Piper failed to synthesize speech.")
                return Path(out_file).read_bytes()
            except FileNotFoundError as e:
                raise ConfigurationError(f"piper executable '{self.executable}' not found (PIPER_EXECUTABLE).") from e
            finally:
                try:
                    os.unlink(out_file)
                except OSError:
                    pass

        return await asyncio.get_running_loop().run_in_executor(None, _run), "audio/wav"

    async def health(self) -> Dict[str, Any]:
        import shutil

        ok = bool(shutil.which(self.executable)) and bool(self.model_path)
        return {"ok": ok, "detail": f"piper at {shutil.which(self.executable) or 'not found'}"}


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"
    # Premade voices work on the free tier; "library" voices (e.g. Rachel 21m00Tcm4TlvDq8ikWAM) need a paid plan.
    DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # "George" (premade, British male)
    PREMADE = {"george": "JBFqnCBsd6RMkjVDRZzb", "daniel": "onwK4e9ZLuTAKqWW03F9", "sarah": "EXAVITQu4vr4xnSDxMaL",
               "brian": "nPczCjzI2devNBz1zQrb", "alice": "Xb7hH8MSUJpSbSDYk0k2", "charlie": "IKne3meq5aSn9XLyUdCD",
               "lily": "pFZP5JQG7iQjIQuC4Bku", "matilda": "XrExE9yKIg1WjnnlVkGX", "liam": "TX3LPaxmHKxFdv7VOQHJ",
               "will": "bIHbv24MWmeRgasZH58o", "jessica": "cgSgspJ2msm6clMCkdW9", "eric": "cjVigY5qzO86Huf0OWal",
               "chris": "iP95p4xoKVk53GoZ742B", "laura": "FGY2WhTYpPnrIDTdsKH5", "callum": "N2lVS1w4EtoT3dr4eOWO"}

    def __init__(self, api_key: str, voice_id: str = "", model: str = "eleven_multilingual_v2"):
        self._api_key = api_key
        self.voice_id = self.PREMADE.get((voice_id or "").strip().lower(), voice_id) or self.DEFAULT_VOICE
        self.model = model

    @property
    def expressive(self) -> bool:
        from app.emotion.speech import TAG_MODELS

        return any(self.model.startswith(m) for m in TAG_MODELS)

    @property
    def speech_model(self) -> str:
        return self.model

    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        from app.emotion.speech import for_voice, prepare

        if not self._api_key:
            raise ConfigurationError("ELEVENLABS_API_KEY is not set.")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
        settings = getattr(style, "settings", None) or {"stability": 0.5, "similarity_boost": 0.75}
        payload = {"text": prepare(text, style, self.model, lead) if style is not None else for_voice(text),
                   "model_id": self.model, "voice_settings": settings}
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="ElevenLabs could not be reached.") from e
        if r.status_code >= 400:
            body = r.text[:300]
            if "paid_plan_required" in body and self.voice_id != self.DEFAULT_VOICE:
                log.warning("ElevenLabs voice %s needs a paid plan; falling back to the premade voice %s", self.voice_id, self.DEFAULT_VOICE)
                self.voice_id = self.DEFAULT_VOICE
                return await self.synthesize(text)
            if r.status_code in (400, 422) and payload.get("voice_settings"):
                log.warning("ElevenLabs rejected voice_settings %s; retrying with defaults", settings)
                payload["voice_settings"] = {"stability": 0.5, "similarity_boost": 0.75}
                async with httpx.AsyncClient(timeout=60) as c:
                    r = await c.post(url, headers=headers, json=payload)
                if r.status_code < 400:
                    return r.content, "audio/mpeg"
                body = r.text[:300]
            if r.status_code == 401:
                raise ProviderError(body, user_message="ElevenLabs rejected the API key (check ELEVENLABS_API_KEY and its permissions).")
            raise ProviderError(body, user_message=f"ElevenLabs returned an error ({r.status_code}).")
        return r.content, "audio/mpeg"

    async def voices(self) -> list:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": self._api_key})
            return [{"id": v["voice_id"], "name": v["name"]} for v in r.json().get("voices", [])]
        except Exception:  # noqa: BLE001
            return []

    async def health(self) -> Dict[str, Any]:
        return {"ok": bool(self._api_key), "detail": "configured" if self._api_key else "missing API key"}


def build_tts_provider(settings: Settings) -> TTSProvider:
    p = settings.tts_provider
    if p == TTSProviderName.CHATTERBOX:
        return ChatterboxTTS(settings.chatterbox_base_url, settings.chatterbox_voice, settings.chatterbox_model,
                             settings.chatterbox_device, settings.chatterbox_exaggeration, settings.chatterbox_cfg_weight,
                             settings.chatterbox_temperature, settings.chatterbox_autostart,
                             idle_unload=settings.chatterbox_idle_unload, language=settings.chatterbox_language)
    if p == TTSProviderName.LOCAL:
        return LocalTTS(settings.tts_voice, settings.tts_rate)
    if p == TTSProviderName.PIPER:
        return PiperTTS(settings.piper_executable, settings.tts_voice)
    if p == TTSProviderName.ELEVENLABS:
        return ElevenLabsTTS(settings.elevenlabs_api_key, settings.tts_voice, settings.elevenlabs_model)
    return DisabledTTS()
