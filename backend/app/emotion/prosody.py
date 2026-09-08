"""How something was said: pitch, energy, rhythm and pauses from the raw microphone audio.

Words carry content; the voice carries state.  "I'm fine" said flatly, slowly and
quietly is not the same sentence as "I'm fine!" said fast and bright, and only the
audio can tell the difference.

Everything here is local and dependency-light: PyAV (already required by
faster-whisper) decodes the browser's webm/opus to PCM, NumPy does the rest.
If either is missing, analysis degrades to "no voice evidence" rather than failing.

Because absolute pitch and loudness differ hugely between people and microphones,
features are scored against a per-user rolling baseline (see `Baseline`), so the
signal is "louder/faster/higher than usual for you", not an absolute threshold.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 400          # 25 ms
HOP = 160            # 10 ms
MIN_F0, MAX_F0 = 70, 400


def _np():
    import numpy as np  # imported lazily so the backend runs without numpy

    return np


def decode_pcm(audio: bytes, mime: str = "audio/webm") -> Optional["Any"]:
    """Decode arbitrary browser audio to mono float32 @16 kHz. None if it cannot be decoded."""
    try:
        from faster_whisper.audio import decode_audio

        return decode_audio(io.BytesIO(audio), sampling_rate=SAMPLE_RATE)
    except Exception as e:  # noqa: BLE001
        log.debug("prosody: decode failed (%s: %s)", e.__class__.__name__, str(e)[:120])
        return None


@dataclass
class ProsodyFeatures:
    duration_s: float = 0.0
    voiced_ratio: float = 0.0      # share of frames with pitch (speech vs silence)
    energy_mean: float = 0.0       # RMS of voiced frames
    energy_var: float = 0.0        # loudness dynamics (monotone vs animated)
    pitch_mean: float = 0.0        # Hz
    pitch_var: float = 0.0         # Hz std over voiced frames (expressiveness)
    pitch_range: float = 0.0       # Hz p90-p10
    speech_rate: float = 0.0       # energy onsets per second ~ syllable rate
    pause_ratio: float = 0.0       # share of time in pauses >300 ms
    longest_pause: float = 0.0     # seconds
    tremor: float = 0.0            # frame-to-frame pitch jitter (distress / shakiness)
    ok: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def extract(audio: bytes, mime: str = "audio/webm") -> ProsodyFeatures:
    """Acoustic features from one utterance. Never raises."""
    f = ProsodyFeatures()
    try:
        np = _np()
    except ImportError:
        return f
    pcm = decode_pcm(audio, mime)
    if pcm is None or len(pcm) < SAMPLE_RATE // 4:
        return f
    pcm = np.asarray(pcm, dtype=np.float32)
    peak = float(np.max(np.abs(pcm))) or 1.0
    pcm = pcm / peak  # normalise recording gain away; dynamics within the clip still matter
    f.duration_s = len(pcm) / SAMPLE_RATE

    n_frames = max(1, 1 + (len(pcm) - FRAME) // HOP)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    idx = idx[idx[:, -1] < len(pcm)]
    if len(idx) < 3:
        return f
    frames = pcm[idx]
    window = np.hanning(FRAME).astype(np.float32)
    rms = np.sqrt(np.mean((frames * window) ** 2, axis=1) + 1e-12)

    # Relative floor (silence is quiet compared with the speech in the same clip),
    # capped so a clip with little dynamic range still counts as speech rather than silence.
    speech_floor = max(float(np.percentile(rms, 20)) * 1.6, float(np.max(rms)) * 0.08)
    speech_floor = min(speech_floor, float(np.max(rms)) * 0.4)
    voiced_mask = rms > speech_floor
    f.voiced_ratio = float(np.mean(voiced_mask))
    if f.voiced_ratio < 0.04:
        return f

    v_rms = rms[voiced_mask]
    f.energy_mean = float(np.mean(v_rms))
    f.energy_var = float(np.std(v_rms) / (np.mean(v_rms) + 1e-9))

    # ---- pitch: autocorrelation per voiced frame -------------------------
    pitches: List[float] = []
    lo, hi = SAMPLE_RATE // MAX_F0, SAMPLE_RATE // MIN_F0
    for fr in frames[voiced_mask][:600]:
        x = (fr - fr.mean()) * window
        corr = np.correlate(x, x, mode="full")[FRAME - 1:]
        if corr[0] <= 1e-9:
            continue
        seg = corr[lo:hi]
        if seg.size < 3:
            continue
        k = int(np.argmax(seg))
        # a real pitch peak should be a decent fraction of the zero-lag energy
        if seg[k] / corr[0] < 0.3:
            continue
        pitches.append(SAMPLE_RATE / float(k + lo))
    if len(pitches) >= 5:
        p = np.array(pitches, dtype=np.float32)
        p = p[(p > MIN_F0) & (p < MAX_F0)]
        if p.size >= 5:
            f.pitch_mean = float(np.median(p))
            f.pitch_var = float(np.std(p))
            f.pitch_range = float(np.percentile(p, 90) - np.percentile(p, 10))
            f.tremor = float(np.mean(np.abs(np.diff(p))) / (np.median(p) + 1e-9))

    # ---- rhythm: onsets and pauses ---------------------------------------
    smooth = np.convolve(rms, np.ones(5, dtype=np.float32) / 5, mode="same")
    d = np.diff(smooth)
    # Syllable nuclei: peaks of the loudness envelope, plus starts of each run of speech.
    # (A rise out of a pause happens *below* the floor, so peaks alone would miss the
    # first syllable of every phrase and crossings alone miss syllables inside a phrase.)
    peaks = int(np.sum((d[:-1] > 0) & (d[1:] <= 0) & (smooth[1:-1] > speech_floor * 1.3)))
    above = smooth > speech_floor
    crossings = int(np.sum(above[1:] & ~above[:-1]))
    f.speech_rate = max(peaks, crossings) / max(f.duration_s, 0.4)

    silent = ~voiced_mask
    runs, cur = [], 0
    for s in silent:
        if s:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    long_runs = [r for r in runs if r * HOP / SAMPLE_RATE > 0.30]
    f.pause_ratio = float(sum(long_runs) * HOP / SAMPLE_RATE / max(f.duration_s, 0.4))
    f.longest_pause = float(max(long_runs, default=0) * HOP / SAMPLE_RATE)
    f.ok = True
    return f


@dataclass
class Baseline:
    """Rolling per-user norm, so features mean 'compared to how you usually sound'."""

    pitch: float = 0.0
    energy: float = 0.0
    rate: float = 0.0
    pitch_var: float = 0.0
    n: int = 0
    alpha: float = 0.15

    def update(self, f: ProsodyFeatures) -> None:
        if not f.ok:
            return
        a = 1.0 if self.n == 0 else self.alpha
        if f.pitch_mean:
            self.pitch = (1 - a) * self.pitch + a * f.pitch_mean if self.pitch else f.pitch_mean
            self.pitch_var = (1 - a) * self.pitch_var + a * f.pitch_var if self.pitch_var else f.pitch_var
        self.energy = (1 - a) * self.energy + a * f.energy_mean if self.energy else f.energy_mean
        self.rate = (1 - a) * self.rate + a * f.speech_rate if self.rate else f.speech_rate
        self.n += 1

    @property
    def ready(self) -> bool:
        return self.n >= 3

    def to_dict(self) -> Dict[str, Any]:
        return {"pitch": round(self.pitch, 1), "energy": round(self.energy, 4), "rate": round(self.rate, 2), "samples": self.n}


def score(f: ProsodyFeatures, base: Baseline) -> Tuple[float, float, float, List[str]]:
    """Map features to (valence, arousal, confidence, cues).

    Voice is a strong signal for arousal (how activated someone is) and a weak,
    noisy signal for valence, so it is weighted accordingly by the analyzer.
    """
    if not f.ok:
        return 0.0, 0.35, 0.0, []
    cues: List[str] = []
    arousal, valence = 0.35, 0.0

    def rel(value: float, ref: float) -> float:
        return (value - ref) / ref if ref > 1e-6 else 0.0

    if base.ready:
        e, r = rel(f.energy_mean, base.energy), rel(f.speech_rate, base.rate)
        p = rel(f.pitch_mean, base.pitch) if f.pitch_mean and base.pitch else 0.0
        pv = rel(f.pitch_var, base.pitch_var) if f.pitch_var and base.pitch_var else 0.0
        arousal += 0.55 * max(-1.0, min(1.0, e)) + 0.5 * max(-1.0, min(1.0, r)) + 0.45 * max(-1.0, min(1.0, p))
        if e > 0.28:
            cues.append("louder than usual")
        elif e < -0.28:
            cues.append("quieter than usual")
        if r > 0.25:
            cues.append("speaking faster")
        elif r < -0.25:
            cues.append("speaking slowly")
        if p > 0.12:
            cues.append("higher pitch")
        elif p < -0.12:
            cues.append("lower pitch")
        if pv < -0.35:
            valence -= 0.22
            cues.append("flat, monotone delivery")
        elif pv > 0.4:
            arousal += 0.08
            cues.append("animated delivery")
    else:
        # No baseline yet: use absolute-ish heuristics, and say so via low confidence.
        arousal += 0.5 * max(-1.0, min(1.0, (f.speech_rate - 3.6) / 2.2))
        arousal += 0.3 * max(-1.0, min(1.0, (f.energy_mean - 0.09) / 0.09))
        if f.speech_rate > 5.2:
            cues.append("fast speech")
        if f.pitch_var and f.pitch_var < 12:
            valence -= 0.15
            cues.append("monotone")

    if f.pause_ratio > 0.30 or f.longest_pause > 1.2:
        valence -= 0.22
        arousal -= 0.12
        cues.append("hesitant, long pauses")
    if f.energy_var > 0.85:
        arousal += 0.10
        cues.append("uneven volume")
    if f.tremor > 0.13:
        valence -= 0.18
        arousal += 0.15
        cues.append("unsteady voice")

    confidence = 0.5 if base.ready else 0.28
    if f.duration_s < 1.2:
        confidence *= 0.6          # a two-word answer says little
    if not f.pitch_mean:
        confidence *= 0.7
    return max(-1.0, min(1.0, valence)), max(0.0, min(1.0, arousal)), confidence, cues
