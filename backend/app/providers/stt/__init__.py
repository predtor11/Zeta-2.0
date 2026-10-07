"""Speech-to-text providers.

    STTProvider
    ├── WhisperSTT        (faster-whisper, fully local)
    ├── OpenAICompatibleSTT (any /v1/audio/transcriptions endpoint)
    ├── ElevenLabsSTT     (ElevenLabs Scribe, cloud; same key as the TTS)
    └── DisabledSTT
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.core.config import STTProviderName, Settings
from app.core.exceptions import ConfigurationError, ProviderError

log = logging.getLogger(__name__)


class STTProvider(ABC):
    name = "base"

    @abstractmethod
    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str: ...

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "detail": self.name}


class DisabledSTT(STTProvider):
    name = "disabled"

    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str:
        raise ConfigurationError("Speech-to-text is disabled. Set STT_PROVIDER=whisper (pip install faster-whisper) or STT_PROVIDER=openai.")

    async def health(self) -> Dict[str, Any]:
        return {"ok": False, "detail": "disabled"}


def _to_wav(audio: bytes, mime: str) -> bytes:
    """Convert browser audio (webm/ogg/mp4) to 16 kHz mono WAV using ffmpeg if available."""
    if mime in ("audio/wav", "audio/x-wav", "audio/wave"):
        return audio
    try:
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                              input=audio, capture_output=True, timeout=60)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        log.warning("ffmpeg conversion failed: %s", proc.stderr.decode(errors="ignore")[:200])
    except FileNotFoundError:
        log.debug("ffmpeg not found; passing audio through as-is")
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out")
    return audio


def _enable_cuda_dlls() -> None:
    """Let CTranslate2 find cuBLAS/cuDNN when they came from pip rather than a CUDA install.

    The `nvidia-*-cu12` wheels drop their DLLs in site-packages/nvidia/<lib>/bin, which is not on
    the Windows DLL search path, so faster-whisper would report "cublas64_12.dll is not found" and
    fall back to the CPU. Called once, before the model loads.
    """
    if os.name != "nt" or getattr(_enable_cuda_dlls, "done", False):
        return
    _enable_cuda_dlls.done = True                      # type: ignore[attr-defined]
    try:
        import nvidia
    except ImportError:
        return
    # `nvidia` is a namespace package, so it has __path__ but no __file__.
    added = []
    for root in [Path(p) for p in getattr(nvidia, "__path__", [])]:
        for folder in sorted(root.glob("*/bin")):
            try:
                os.add_dll_directory(str(folder))
                added.append(str(folder))
            except OSError:                            # already added, or gone
                pass
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
        log.info("CUDA libraries found in %d pip folder(s); GPU transcription enabled.", len(added))


class WhisperSTT(STTProvider):
    name = "whisper"

    def __init__(self, model_size: str = "base", language: str = "", device: str = "auto", compute_type: str = "",
                 languages: str = ""):
        self.model_size = model_size
        self.language = language or None
        # Which languages this household actually speaks. Whisper otherwise picks from 99, and on
        # a two-second utterance it will cheerfully return Welsh or Urdu; narrowing the choice to
        # the ones that can really occur removes a whole class of nonsense transcripts.
        self.languages = [c.strip().lower() for c in (languages or "").split(",") if c.strip()]
        self._warned_detect = False
        self.device = (device or "auto").lower()
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8" if self.device == "cpu" else "default")
        self._model = None
        self._lock = asyncio.Lock()
        self.active_device = ""

    def _load(self, device: Optional[str] = None):
        if self._model is None or device:
            dev = device or self.device
            if dev != "cpu":
                _enable_cuda_dlls()
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except ImportError as e:
                raise ConfigurationError("faster-whisper is not installed. Run: pip install faster-whisper") from e
            ctype = "int8" if dev == "cpu" else self.compute_type
            log.info("Loading faster-whisper model '%s' on %s (%s)…", self.model_size, dev, ctype)
            self._model = WhisperModel(self.model_size, device=dev, compute_type=ctype)
            self.active_device = dev
        return self._model

    def _detect(self, path: str) -> Optional[str]:
        """Pick a language, but only from the ones that are actually spoken here.

        `detect_language` hands back the probability of every language it knows, so restricting
        the choice is just a matter of ignoring the ones not on the list rather than trusting its
        top pick. Returns None when no list is configured, which leaves Whisper free to choose.
        """
        if not self.languages:
            return None
        if len(self.languages) == 1:
            return self.languages[0]
        try:
            # `detect_language` wants a 1-D float array at 16 kHz, NOT a path. Handing it a path
            # raises, and if that is swallowed quietly Whisper falls back to choosing from all 99
            # languages - which is the exact failure this method exists to prevent. It produced a
            # confident Portuguese transcript of Hindi speech before this was fixed.
            from faster_whisper.audio import decode_audio

            samples = decode_audio(path, sampling_rate=16000)
            _, _, probs = self._model.detect_language(audio=samples, vad_filter=True)
        except Exception as e:  # noqa: BLE001
            if not self._warned_detect:
                self._warned_detect = True
                log.warning("Language detection failed (%s: %s); Whisper is choosing from all languages, "
                            "which STT_LANGUAGES is meant to prevent.", type(e).__name__, str(e)[:120])
            return None
        allowed = {c: p for c, p in probs if c in self.languages}
        if not allowed:
            return self.languages[0]
        best = max(allowed, key=allowed.get)
        log.debug("language %s (%.2f) chosen from %s", best, allowed[best], ", ".join(self.languages))
        return best

    @staticmethod
    def _is_gpu_error(e: Exception) -> bool:
        m = str(e).lower()
        return any(k in m for k in ("cublas", "cudnn", "cuda", "cudart", "gpu"))

    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str:
        async with self._lock:
            model = await asyncio.get_running_loop().run_in_executor(None, self._load)
        data = await asyncio.get_running_loop().run_in_executor(None, _to_wav, audio, mime)

        def _run():
            with tempfile.NamedTemporaryFile(suffix=".wav" if data is not audio else Path("x" + _ext(mime)).suffix, delete=False) as f:
                f.write(data)
                path = f.name
            try:
                lang = language or self.language or self._detect(path)
                segments, info = self._model.transcribe(path, language=lang, vad_filter=True, beam_size=5)
                return " ".join(s.text.strip() for s in segments).strip()
            finally:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception as e:  # noqa: BLE001
            if self._is_gpu_error(e) and self.active_device != "cpu":
                log.warning("faster-whisper GPU run failed (%s); falling back to CPU. For GPU speed: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
                            str(e)[:120])
                async with self._lock:
                    model = await asyncio.get_running_loop().run_in_executor(None, self._load, "cpu")
                try:
                    return await asyncio.get_running_loop().run_in_executor(None, _run)
                except Exception as e2:  # noqa: BLE001
                    log.error("whisper failed on CPU too: %s", e2)
                    raise ProviderError(f"whisper failed: {e2}", user_message=f"I couldn't transcribe the audio ({e2.__class__.__name__}).") from e2
            log.error("whisper failed: %s", e)
            raise ProviderError(f"whisper failed: {e}", user_message=f"I couldn't transcribe the audio ({str(e)[:80]}).") from e

    async def health(self) -> Dict[str, Any]:
        try:
            import faster_whisper  # noqa: F401

            return {"ok": True, "detail": f"faster-whisper ({self.model_size}, {self.active_device or self.device})"}
        except ImportError:
            return {"ok": False, "detail": "faster-whisper not installed"}


def _ext(mime: str) -> str:
    return {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav"}.get(mime.split(";")[0], ".webm")


class OpenAICompatibleSTT(STTProvider):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str = "whisper-1", language: str = ""):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._api_key = api_key
        self.model = model
        self.language = language or None

    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        files = {"file": ("audio" + _ext(mime), audio, mime.split(";")[0])}
        data = {"model": self.model}
        if language or self.language:
            data["language"] = language or self.language
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(f"{self.base_url}/audio/transcriptions", headers=headers, files=files, data=data)
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="The transcription service could not be reached.") from e
        if r.status_code >= 400:
            raise ProviderError(r.text[:300], user_message=f"The transcription service returned an error ({r.status_code}).")
        return (r.json().get("text") or "").strip()

    async def health(self) -> Dict[str, Any]:
        return {"ok": bool(self._api_key) or "localhost" in self.base_url, "detail": f"{self.base_url} ({self.model})"}


class ElevenLabsSTT(STTProvider):
    """ElevenLabs Scribe speech-to-text (`POST /v1/speech-to-text`, model scribe_v1).

    Accepts browser audio (webm/ogg/mp4/wav) directly, so ffmpeg is not needed.  Requires an
    API key with the *Speech to Text* permission.  Language is auto-detected unless set.
    """

    name = "elevenlabs"

    def __init__(self, api_key: str, model: str = "scribe_v1", language: str = ""):
        self._api_key = api_key
        self.model = model if model and model not in ("base", "small", "tiny", "medium", "large-v3", "whisper-1") else "scribe_v1"
        self.language = language or None

    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str:
        if not self._api_key:
            raise ConfigurationError("ELEVENLABS_API_KEY is not set.")
        files = {"file": ("audio" + _ext(mime), audio, mime.split(";")[0])}
        data: Dict[str, Any] = {"model_id": self.model, "tag_audio_events": "false", "diarize": "false"}
        lang = language or self.language
        if lang:
            data["language_code"] = lang
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": self._api_key}, files=files, data=data)
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="ElevenLabs speech-to-text could not be reached.") from e
        if r.status_code == 401:
            raise ProviderError(r.text[:300], user_message="ElevenLabs rejected the API key for speech-to-text (the key needs the Speech to Text permission).")
        if r.status_code >= 400:
            raise ProviderError(r.text[:300], user_message=f"ElevenLabs speech-to-text returned an error ({r.status_code}).")
        return (r.json().get("text") or "").strip()

    async def health(self) -> Dict[str, Any]:
        return {"ok": bool(self._api_key), "detail": f"ElevenLabs Scribe ({self.model})" if self._api_key else "missing API key"}


def build_stt_provider(settings: Settings) -> STTProvider:
    p = settings.stt_provider
    if p == STTProviderName.WHISPER:
        return WhisperSTT(settings.stt_model or "base", settings.stt_language, settings.stt_device,
                          languages=settings.stt_languages)
    if p == STTProviderName.ELEVENLABS:
        return ElevenLabsSTT(settings.stt_api_key or settings.elevenlabs_api_key, settings.stt_model, settings.stt_language)
    if p == STTProviderName.OPENAI:
        return OpenAICompatibleSTT(settings.stt_base_url or settings.llm_base_url, settings.stt_api_key or settings.openai_api_key or settings.llm_api_key,
                                   settings.stt_model if settings.stt_model not in ("base", "") else "whisper-1", settings.stt_language)
    return DisabledSTT()
