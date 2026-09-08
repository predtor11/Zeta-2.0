"""Wake word listener ("Hey Zeta").

Runs entirely on the host: a background thread captures the default microphone
with `sounddevice` (16 kHz mono) and feeds 80 ms frames to an engine.

Engines
* `whisper`      - default. Energy-gated: when speech is heard, the last ~2.5 s
                   are transcribed with a tiny faster-whisper model (CPU, int8)
                   and fuzzy-matched against the phrase.  Works for any phrase
                   ("hey zeta") with no training, at the cost of some CPU while
                   someone is talking.
* `openwakeword` - optional, cheaper: `pip install openwakeword`.  Uses a
                   pretrained model (e.g. `hey_jarvis`) or a custom .onnx/.tflite
                   trained for your phrase (`WAKE_WORD_MODEL`).

On detection the service beeps and calls `on_wake(engine, text)`; ZetaServices
publishes a `wake_word` event so the UI starts a voice capture.  Nothing is
sent anywhere: audio never leaves the machine.

Optional dependencies: `sounddevice`, `numpy` (+ `faster-whisper` or `openwakeword`).
"""

from __future__ import annotations

import difflib
import logging
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 80
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

_WORD_RE = re.compile(r"[a-z]+")

# Common mis-hearings of "zeta" by small speech models.
_ALIASES = {"zeta": ["zeta", "zita", "seta", "zetta", "zeeta", "zeda", "seda", "theta", "zaida", "zayda", "zeta's", "zetta's"]}

# "the zeta function" scores 0.88 against "hey zeta" on a plain string comparison, because
# "the" and "hey" share two letters. A greeting is never one of these, so the slot in front
# of the name cannot be filled by one.
_FUNCTION_WORDS = {"the", "a", "an", "of", "in", "on", "to", "at", "by", "for", "that", "this",
                   "is", "was", "and", "or", "but", "with", "from", "its", "his", "her", "their"}

# Mis-hearings that are also ordinary words. They count inside the full phrase ("hey theta")
# but never on their own, or a maths lecture would keep waking Zeta up.
_AMBIGUOUS = {"theta", "seta", "seda", "cedar", "cheetah", "data", "beta", "meta"}

# What a small Whisper model writes when it is handed silence or room noise. If that is the
# whole transcript then nobody said anything, whatever the decoder guessed.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching", "bye", "bye bye",
    "please subscribe", "subscribe", "so", "okay", "oh", "hmm", "mm", "yeah", "uh", "the end",
    "music", "applause", "silence", "thank you very much", "i'm sorry",
}


def looks_hallucinated(text: str) -> bool:
    """True for the stock things Whisper emits on non-speech, and for stuck repetitions."""
    stripped = text.strip().lower().strip(" .!?,")
    if not stripped or stripped in _HALLUCINATIONS:
        return True
    words = normalize_words(text)
    # "hey zeta hey zeta hey zeta" is the decoder looping on its own bias, not a person.
    return len(words) >= 6 and len(set(words)) <= max(2, len(words) // 3)


def normalize_words(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower().replace("'", ""))


def phrase_matches(text: str, phrase: str, *, threshold: float = 0.78, bare_name_max_words: int = 4) -> bool:
    """Fuzzy check whether `phrase` ("hey zeta") occurs in `text` (a transcription).

    Accepts the full phrase within a sliding window (difflib ratio >= threshold)
    and, for phrases of the form "<greeting> <name>", the bare name if it is a
    close match - saying just "Zeta" should work too.

    The bare name only counts in a short utterance (`bare_name_max_words`). In the middle of
    a sentence a lone "zeta" is far more often the decoder's own bias leaking through than
    someone calling out, and requiring the greeting there costs nothing.
    """
    words = normalize_words(text)
    target = normalize_words(phrase)
    if not words or not target:
        return False
    n = len(target)
    joined_target = " ".join(target)
    for i in range(0, max(1, len(words) - n + 1)):
        chunk = words[i:i + n]
        if n > 1 and chunk[0] in _FUNCTION_WORDS:
            continue
        window = " ".join(chunk)
        if difflib.SequenceMatcher(None, window, joined_target).ratio() >= threshold:
            return True
    if bare_name_max_words and len(words) > bare_name_max_words:
        return False
    name = target[-1]
    variants = (set(_ALIASES.get(name, [])) | {name}) - _AMBIGUOUS
    for w in words:
        if w in variants:
            return True
        if len(w) >= 4 and w not in _AMBIGUOUS and difflib.SequenceMatcher(None, w, name).ratio() >= 0.88:
            return True
    return False


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
class WakeEngine:
    name = "base"

    def process(self, frame: Any) -> Optional[str]:
        """Feed one int16 frame; return matched text when the wake word was heard."""
        raise NotImplementedError

    def close(self) -> None:
        return None


class WhisperPhraseEngine(WakeEngine):
    name = "whisper"

    def __init__(self, phrase: str, model_size: str = "tiny", window_seconds: float = 2.5, min_speech_ms: int = 400,
                 silence_ms: int = 450, sensitivity: float = 0.5):
        import numpy as np  # noqa: F401  (validated at construction)
        from faster_whisper import WhisperModel

        self.np = np
        self.phrase = phrase
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self.window = int(SAMPLE_RATE * window_seconds)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.buffer = np.zeros(0, dtype=np.int16)
        self.noise = 200.0          # adaptive noise floor (RMS)
        self.speech_frames = 0
        self.silent_frames = 0
        self.in_speech = False
        # One dial, WAKE_WORD_SENSITIVITY: 0 = only an unmistakable "hey zeta", 1 = eager.
        # A tiny Whisper model will confidently write words for a fan or a cough, so each of
        # these is a separate reason to throw a candidate away.
        sens = min(1.0, max(0.0, sensitivity))
        self.energy_floor = 700.0 - 400.0 * sens        # RMS a frame needs before it counts as speech
        self.match_threshold = 0.86 - 0.10 * sens       # how close the words have to be
        self.no_speech_max = 0.20 + 0.55 * sens         # Whisper's own "there was no speech here"
        self.logprob_min = -0.45 - 1.05 * sens          # ...and its confidence in what it wrote
        self.rejected = 0                               # candidates thrown away (shown in status)
        self.last_rejected = ""

    def _rms(self, frame) -> float:
        f = frame.astype(self.np.float32)
        return float(self.np.sqrt(self.np.mean(f * f)) or 0.0)

    def process(self, frame) -> Optional[str]:
        np = self.np
        frame = frame.reshape(-1)
        self.buffer = np.concatenate([self.buffer, frame])[-self.window:]
        rms = self._rms(frame)
        threshold = max(self.energy_floor, self.noise * 3.0)
        if rms > threshold:
            self.in_speech = True
            self.speech_frames += 1
            self.silent_frames = 0
        else:
            self.noise = 0.95 * self.noise + 0.05 * rms
            if self.in_speech:
                self.silent_frames += 1
        # End of an utterance (or a long one): transcribe the window.
        if self.in_speech and (self.silent_frames >= self.silence_frames or self.speech_frames * FRAME_MS >= 2500):
            had = self.speech_frames
            self.in_speech, self.speech_frames, self.silent_frames = False, 0, 0
            if had < self.min_speech_frames:
                return None
            audio = self.buffer.astype(np.float32) / 32768.0
            try:
                # Whisper's own VAD runs first: on room noise it returns no segments at all, which
                # is the cheapest way not to hallucinate a wake word. Decoding is greedy at
                # temperature 0 so it cannot get creative. `hotwords` nudges "Zeta" (small models
                # otherwise write "either" or "theta"). An `initial_prompt` stuffed with the phrase
                # used to live here as well, and that is precisely what made Zeta wake up on its
                # own: given nothing to transcribe, the model repeats its prompt back.
                kwargs: Dict[str, Any] = dict(language="en", beam_size=1, temperature=0.0,
                                              condition_on_previous_text=False, without_timestamps=True,
                                              no_speech_threshold=0.5, log_prob_threshold=-1.0, vad_filter=True,
                                              vad_parameters={"min_speech_duration_ms": 200, "speech_pad_ms": 120})
                try:
                    segments, _ = self.model.transcribe(audio, hotwords=self.phrase.title(), **kwargs)
                except TypeError:  # older faster-whisper without `hotwords`
                    segments, _ = self.model.transcribe(audio, **kwargs)
                segs = list(segments)
            except Exception as e:  # noqa: BLE001
                log.debug("wake transcription failed: %s", e)
                return None
            text = " ".join(seg.text for seg in segs).strip()
            if not text:
                return None
            reason = self._reject_reason(segs, text)
            if reason:
                self.rejected += 1
                self.last_rejected = f"{text} ({reason})"
                log.debug("wake candidate rejected: %r - %s", text, reason)
                return None
            log.debug("wake candidate: %r", text)
            if phrase_matches(text, self.phrase, threshold=self.match_threshold):
                self.buffer = np.zeros(0, dtype=np.int16)
                return text
        return None

    def _reject_reason(self, segs: list, text: str) -> str:
        """Why this transcription should not count. An empty string means it is worth matching."""
        if looks_hallucinated(text):
            return "stock non-speech text"
        no_speech = max((getattr(seg, "no_speech_prob", 0.0) or 0.0) for seg in segs)
        if no_speech > self.no_speech_max:
            return f"no_speech_prob {no_speech:.2f}"
        logprobs = [getattr(seg, "avg_logprob", 0.0) or 0.0 for seg in segs]
        avg = sum(logprobs) / len(logprobs)
        if avg < self.logprob_min:
            return f"avg_logprob {avg:.2f}"
        return ""


class OpenWakeWordEngine(WakeEngine):
    name = "openwakeword"

    def __init__(self, model: str = "", threshold: float = 0.5):
        from openwakeword.model import Model

        kwargs: Dict[str, Any] = {"inference_framework": "onnx"}
        if model:
            kwargs["wakeword_models"] = [model]
        self.model = Model(**kwargs)
        self.threshold = threshold
        self.label = model or "any"

    def process(self, frame) -> Optional[str]:
        scores = self.model.predict(frame.reshape(-1))
        for name, score in scores.items():
            if score >= self.threshold:
                self.model.reset()
                return f"{name} ({score:.2f})"
        return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class WakeWordService:
    def __init__(self, *, enabled: bool, phrase: str = "hey zeta", engine: str = "auto", model: str = "",
                 sensitivity: float = 0.5, device: str = "", on_wake: Optional[Callable[[str, str], None]] = None,
                 cooldown_seconds: float = 4.0, beep: bool = True):
        self.enabled = enabled
        self.phrase = phrase or "hey zeta"
        self.engine_name = (engine or "auto").lower()
        self.model_name = model
        self.sensitivity = sensitivity
        self.device = device
        self.on_wake = on_wake
        self.cooldown = cooldown_seconds
        self.beep = beep
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._engine: Optional[WakeEngine] = None
        self.error = ""
        self.detections = 0
        self.last_detection: Optional[float] = None
        self.last_text = ""
        self.paused_until = 0.0

    # ---- lifecycle -----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="zeta-wakeword", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        self._thread = None

    def pause(self, seconds: float) -> None:
        """Ignore the microphone briefly (e.g. while the UI is already recording or Zeta is speaking)."""
        self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "running": self.running, "engine": self._engine.name if self._engine else self.engine_name,
                "phrase": self.phrase, "error": self.error, "detections": self.detections, "last_text": self.last_text,
                "last_detection": self.last_detection, "sensitivity": self.sensitivity,
                "rejected": getattr(self._engine, "rejected", 0),
                "last_rejected": getattr(self._engine, "last_rejected", ""),
                "muted_for": max(0.0, round(self.paused_until - time.monotonic(), 1))}

    # ---- internals -----------------------------------------------------
    def _build_engine(self) -> WakeEngine:
        want = self.engine_name
        if want in ("auto", "openwakeword"):
            try:
                eng = OpenWakeWordEngine(self.model_name, self.sensitivity)
                if want == "openwakeword" or self.model_name:
                    return eng
                eng.close()
            except Exception as e:  # noqa: BLE001
                if want == "openwakeword":
                    raise RuntimeError(f"openwakeword unavailable: {e}. Run: pip install openwakeword") from e
        try:
            return WhisperPhraseEngine(self.phrase, sensitivity=self.sensitivity)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"wake word needs faster-whisper + numpy: {e}. Run: pip install faster-whisper numpy sounddevice") from e

    def _run(self) -> None:
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            self.error = f"sounddevice not installed ({e.__class__.__name__}). Run: pip install sounddevice numpy"
            log.warning("wake word disabled: %s", self.error)
            return
        try:
            self._engine = self._build_engine()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.warning("wake word disabled: %s", self.error)
            return
        frames: "queue.Queue[Any]" = queue.Queue(maxsize=200)

        def _cb(indata, _frames, _time, status):  # noqa: ANN001
            if status:
                log.debug("audio status: %s", status)
            try:
                frames.put_nowait(indata.copy())
            except queue.Full:
                pass

        device: Any = None
        if self.device:
            device = int(self.device) if self.device.isdigit() else self.device
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES, device=device, callback=_cb)
            stream.start()
        except Exception as e:  # noqa: BLE001
            self.error = f"microphone unavailable: {e}"
            log.warning("wake word disabled: %s", self.error)
            return
        log.info("Wake word listener running (engine=%s, phrase=%r)", self._engine.name, self.phrase)
        try:
            while not self._stop.is_set():
                try:
                    frame = frames.get(timeout=0.5)
                except queue.Empty:
                    continue
                if time.monotonic() < self.paused_until:
                    continue
                try:
                    hit = self._engine.process(frame)
                except Exception as e:  # noqa: BLE001
                    log.debug("wake engine error: %s", e)
                    continue
                if hit:
                    self._detected(hit)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._engine.close()
            log.info("Wake word listener stopped")

    def _detected(self, text: str) -> None:
        now = time.monotonic()
        if self.last_detection and now - self.last_detection < self.cooldown:
            return
        self.last_detection = now
        self.detections += 1
        self.last_text = text
        log.info("Wake word detected: %r", text)
        if self.beep:
            try:
                import winsound

                winsound.Beep(880, 120)
            except Exception:  # noqa: BLE001
                pass
        self.paused_until = now + self.cooldown
        if self.on_wake:
            try:
                self.on_wake(self._engine.name if self._engine else self.engine_name, text)
            except Exception as e:  # noqa: BLE001
                log.debug("on_wake failed: %s", e)


def list_input_devices() -> List[Dict[str, Any]]:
    try:
        import sounddevice as sd

        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append({"index": i, "name": d.get("name"), "default": i == sd.default.device[0]})
        return out
    except Exception:  # noqa: BLE001
        return []
