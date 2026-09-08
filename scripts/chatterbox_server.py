"""Zeta's local voice: a small HTTP server around Resemble AI's Chatterbox TTS.

Why a separate process (and a separate virtualenv):

* Chatterbox needs PyTorch + CUDA - about 3 GB of wheels that the Zeta backend
  does not otherwise want, and a model that takes several seconds to load. Keeping
  it here means the backend still starts in a second and stays swappable.
* The model must stay resident to be fast, and the GPU can only do one generation
  at a time, so one long-lived process with a lock is exactly right.
* Zeta talks to it over plain HTTP, so the same interface works if you later move
  it to another machine, or swap Chatterbox for something else.

Run it with the TTS virtualenv, not the backend one:

    .venv-tts\\Scripts\\python scripts\\chatterbox_server.py            # 127.0.0.1:8766
    .venv-tts\\Scripts\\python scripts\\chatterbox_server.py --device cpu --model multilingual

Endpoints:

    GET  /health                 model, device, sample rate, whether it is loaded
    GET  /voices                 the .wav reference clips in voice/
    POST /tts   {"text": ...}    -> audio/wav
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "voice"

log = logging.getLogger("chatterbox-server")

# Paralinguistic tags Chatterbox Turbo performs. Anything else is stripped before
# generation so a stray "[warmly]" is never read out as a word.
TAGS = {"laugh", "laughs", "chuckle", "chuckles", "giggle", "giggles", "sigh", "sighs",
        "gasp", "gasps", "cough", "coughs", "sniff", "clears throat", "breath", "whisper"}
_TAG_RE = re.compile(r"\[([a-z][a-z ']{1,24})\]", re.I)
_SENT_RE = re.compile(r"(?<=[.!?…])\s+")
MAX_CHARS = 280          # Chatterbox degrades on very long inputs; split on sentences instead


def clean_text(text: str) -> str:
    """Keep the tags Chatterbox knows, drop the ones it would pronounce."""
    def take(m: "re.Match[str]") -> str:
        return m.group(0) if m.group(1).strip().lower() in TAGS else " "

    return re.sub(r"\s{2,}", " ", _TAG_RE.sub(take, text)).strip()


def chunks(text: str, limit: int = MAX_CHARS) -> List[str]:
    """Sentence-aware splitting so long replies keep their prosody and never truncate."""
    out: List[str] = []
    current = ""
    for sentence in _SENT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:                      # a single monster sentence
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit * 0.6 else limit
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= limit:
            current = f"{current} {sentence}"
        else:
            out.append(current)
            current = sentence
    if current:
        out.append(current)
    return out or [text]


class Engine:
    """Loads Chatterbox once and serialises generation (one GPU, one job at a time)."""

    def __init__(self, model: str = "turbo", device: str = "auto", voice: str = ""):
        self.model_name = model
        self.requested_device = device
        self.voice = voice
        self.model: Any = None
        self.device = ""
        self.sr = 24000
        self.error = ""
        self.load_seconds = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loading
    def _pick_device(self) -> str:
        import torch

        if self.requested_device in ("cuda", "cpu"):
            return self.requested_device
        return "cuda" if torch.cuda.is_available() else "cpu"

    def load(self) -> None:
        t0 = time.time()
        device = self._pick_device()
        try:
            self.model = self._load_weights(device)
            self.device = device
        except Exception as e:  # noqa: BLE001
            # Out of VRAM (a local LLM is probably holding it) - CPU still works, just slower.
            if device == "cuda":
                log.warning("CUDA load failed (%s: %s); falling back to CPU", type(e).__name__, str(e)[:160])
                try:
                    self.model = self._load_weights("cpu")
                    self.device = "cpu"
                except Exception as e2:  # noqa: BLE001
                    self.error = f"{type(e2).__name__}: {e2}"
                    log.error("Chatterbox failed to load: %s", self.error)
                    return
            else:
                self.error = f"{type(e).__name__}: {e}"
                log.error("Chatterbox failed to load: %s", self.error)
                return
        self.sr = int(getattr(self.model, "sr", 24000))
        try:
            # The first generation compiles kernels and allocates buffers; do it now, not
            # in front of the user.
            self.generate("Ready.", exaggeration=0.5, cfg_weight=0.5)
        except Exception as e:  # noqa: BLE001
            log.warning("warm-up generation failed: %s", str(e)[:160])
        self.load_seconds = time.time() - t0
        log.info("Chatterbox %s ready on %s in %.1fs (%d Hz)", self.model_name, self.device, self.load_seconds, self.sr)

    def _load_weights(self, device: str) -> Any:
        """Load from the Hub, or from the local cache when the Hub is unreachable.

        huggingface.co is blocked on some networks; once the weights are cached there is no reason
        to fail because a metadata call cannot get through. (`HF_ENDPOINT=https://hf-mirror.com`
        works for the first download in that situation.)
        """
        try:
            return self._from_pretrained(device)
        except Exception as e:  # noqa: BLE001
            if "connect" not in f"{type(e).__name__}{e}".lower() and "LocalEntryNotFound" not in type(e).__name__:
                raise
            log.warning("Hub unreachable (%s); using the cached model.", type(e).__name__)
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                return self._from_pretrained(device)
            finally:
                os.environ.pop("HF_HUB_OFFLINE", None)

    def _from_pretrained(self, device: str) -> Any:
        """Turbo if the installed package has it, otherwise the standard model."""
        if self.model_name == "multilingual":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            return ChatterboxMultilingualTTS.from_pretrained(device=device)
        if self.model_name == "turbo":
            try:
                from chatterbox.tts_turbo import ChatterboxTurboTTS

                return ChatterboxTurboTTS.from_pretrained(device=device)
            except ImportError:
                log.warning("This chatterbox-tts build has no Turbo model; using the standard one.")
                self.model_name = "base"
        from chatterbox.tts import ChatterboxTTS

        return ChatterboxTTS.from_pretrained(device=device)

    # ------------------------------------------------------------------ speaking
    def generate(self, text: str, voice: str = "", exaggeration: float = 0.5,
                 cfg_weight: float = 0.5, temperature: float = 0.8, language: str = "") -> bytes:
        if self.model is None:
            raise RuntimeError(self.error or "model is still loading")
        import numpy as np
        import torch

        prompt = (voice or self.voice or "").strip()
        if prompt and not Path(prompt).exists():
            candidate = VOICE_DIR / prompt
            if candidate.exists():
                prompt = str(candidate)
            else:
                log.warning("Reference clip %r not found; using the built-in voice.", prompt)
                prompt = ""

        pieces: List["np.ndarray"] = []
        with self._lock:
            for part in chunks(clean_text(text)):
                kwargs: Dict[str, Any] = {"temperature": temperature}
                if self.model_name == "turbo":
                    # Turbo has no CFG or exaggeration control; more emotion becomes more
                    # variation in delivery, which is the knob it does have.
                    kwargs["temperature"] = round(min(1.1, max(0.5, temperature + (exaggeration - 0.5) * 0.3)), 3)
                else:
                    kwargs["exaggeration"] = exaggeration
                    kwargs["cfg_weight"] = cfg_weight
                if prompt:
                    kwargs["audio_prompt_path"] = prompt
                if language and self.model_name == "multilingual":
                    kwargs["language_id"] = language
                wav = self.model.generate(part, **self._supported(kwargs))
                audio = wav.detach().cpu().numpy() if isinstance(wav, torch.Tensor) else np.asarray(wav)
                pieces.append(audio.reshape(-1))
                pieces.append(np.zeros(int(self.sr * 0.12), dtype=audio.dtype))   # breath between sentences
        joined = np.concatenate(pieces[:-1]) if len(pieces) > 1 else pieces[0]
        return to_wav(joined, self.sr)

    def _supported(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Only pass arguments this build's generate() actually accepts."""
        import inspect

        try:
            allowed = set(inspect.signature(self.model.generate).parameters)
        except (TypeError, ValueError):
            return kwargs
        return {k: v for k, v in kwargs.items() if k in allowed}

    def status(self) -> Dict[str, Any]:
        return {"ok": self.model is not None, "model": self.model_name, "device": self.device or self.requested_device,
                "sample_rate": self.sr, "voice": self.voice or "built-in", "load_seconds": round(self.load_seconds, 1),
                "error": self.error}


def to_wav(samples: Any, sample_rate: int) -> bytes:
    """float32 [-1, 1] -> 16-bit PCM WAV bytes."""
    import numpy as np

    a = np.asarray(samples, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    if peak > 1.0:
        a = a / peak
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class Speak(BaseModel):
    """The request body. Defined at module level so FastAPI can resolve the annotation."""

    text: str
    voice: str = ""
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    temperature: float = 0.8
    language: str = ""


def build_app(engine: Engine):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response

    @asynccontextmanager
    async def lifespan(_app):
        # Load in the background so /health answers immediately while the model warms up.
        threading.Thread(target=engine.load, name="chatterbox-load", daemon=True).start()
        yield

    app = FastAPI(title="Zeta Chatterbox TTS", docs_url="/docs", lifespan=lifespan)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return engine.status()

    @app.get("/voices")
    def voices() -> Dict[str, Any]:
        files = sorted(p.name for p in VOICE_DIR.glob("*.wav")) if VOICE_DIR.exists() else []
        return {"dir": str(VOICE_DIR), "voices": files, "current": engine.voice or "built-in"}

    @app.post("/tts")
    async def tts(req: Speak) -> Response:
        import anyio

        if not req.text.strip():
            raise HTTPException(400, "empty text")
        if engine.model is None:
            raise HTTPException(503, engine.error or "model is still loading, try again in a moment")
        t0 = time.time()
        try:
            data = await anyio.to_thread.run_sync(
                lambda: engine.generate(req.text, req.voice, req.exaggeration, req.cfg_weight, req.temperature, req.language)
            )
        except Exception as e:  # noqa: BLE001
            log.exception("generation failed")
            raise HTTPException(500, f"{type(e).__name__}: {str(e)[:200]}") from e
        seconds = (len(data) - 44) / 2 / engine.sr
        log.info("spoke %.1fs of audio in %.2fs (%.1fx realtime)", seconds, time.time() - t0, seconds / max(time.time() - t0, 1e-6))
        return Response(content=data, media_type="audio/wav")

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Chatterbox TTS server for Zeta")
    ap.add_argument("--host", default=os.getenv("CHATTERBOX_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("CHATTERBOX_PORT", "8766")))
    ap.add_argument("--model", default=os.getenv("CHATTERBOX_MODEL", "turbo"), choices=["turbo", "base", "multilingual"])
    ap.add_argument("--device", default=os.getenv("CHATTERBOX_DEVICE", "auto"), choices=["auto", "cuda", "cpu"])
    ap.add_argument("--voice", default=os.getenv("CHATTERBOX_VOICE", ""), help="reference .wav for the voice (empty = built-in)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")      # xet transfers fail on some Windows setups
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "5")     # do not stall on a blocked Hub; the cache is enough

    try:
        import uvicorn
    except ImportError:
        print("The TTS virtualenv is missing. Run:  install_tts.bat", file=sys.stderr)
        raise SystemExit(2)

    engine = Engine(model=args.model, device=args.device, voice=args.voice)
    print(f"Chatterbox TTS on http://{args.host}:{args.port}  (model={args.model}, device={args.device}, "
          f"voice={args.voice or 'built-in'})")
    uvicorn.run(build_app(engine), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
