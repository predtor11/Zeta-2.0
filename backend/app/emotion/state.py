"""The shared emotion vocabulary: a state, its dimensions, and how labels map to them.

Zeta models feeling on two axes (Russell's circumplex):
    valence  -1 (distressed / negative) .. +1 (positive)
    arousal   0 (flat, tired, calm)     ..  1 (activated, agitated, excited)

A label is a readable name for a region of that space.  Every state carries the
`cues` that produced it, so the UI and the logs can always explain *why* Zeta
thinks you feel a certain way - never a black box.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# label -> (valence, arousal, short description used in the prompt)
LABELS: Dict[str, tuple] = {
    "neutral":     (0.0, 0.35, "neutral, matter-of-fact"),
    "content":     (0.45, 0.30, "quietly content"),
    "happy":       (0.75, 0.60, "happy, in good spirits"),
    "excited":     (0.70, 0.90, "excited, energised"),
    "grateful":    (0.70, 0.40, "warm and appreciative"),
    "hopeful":     (0.50, 0.50, "hopeful, looking forward"),
    "relieved":    (0.55, 0.25, "relieved, tension easing"),
    "calm":        (0.30, 0.15, "calm and settled"),
    "tired":       (-0.20, 0.10, "tired, low energy"),
    "bored":       (-0.25, 0.20, "bored or disengaged"),
    "sad":         (-0.65, 0.25, "sad, low"),
    "lonely":      (-0.60, 0.30, "lonely, wanting connection"),
    "hurt":        (-0.65, 0.45, "hurt, emotionally wounded"),
    "disappointed": (-0.50, 0.35, "disappointed"),
    "anxious":     (-0.55, 0.75, "anxious, worried"),
    "overwhelmed": (-0.60, 0.80, "overwhelmed, too much at once"),
    "stressed":    (-0.50, 0.75, "under pressure"),
    "frustrated":  (-0.55, 0.70, "frustrated, blocked"),
    "angry":       (-0.70, 0.85, "angry"),
    "afraid":      (-0.70, 0.80, "afraid"),
    "confused":    (-0.25, 0.50, "confused, unsure"),
    "distressed":  (-0.85, 0.75, "in real distress"),
}

# Labels that mean "this person could use support rather than efficiency".
SUPPORT_LABELS = {"sad", "lonely", "hurt", "disappointed", "anxious", "overwhelmed", "stressed",
                  "frustrated", "angry", "afraid", "distressed", "tired"}

POSITIVE_LABELS = {"content", "happy", "excited", "grateful", "hopeful", "relieved", "calm"}


def nearest_label(valence: float, arousal: float, exclude_neutral: bool = False) -> str:
    """Closest label to a point on the circumplex (valence weighted higher than arousal)."""
    best, best_d = "neutral", 1e9
    for name, (v, a, _) in LABELS.items():
        if exclude_neutral and name == "neutral":
            continue
        d = ((v - valence) * 1.35) ** 2 + (a - arousal) ** 2
        if d < best_d:
            best, best_d = name, d
    return best


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class EmotionState:
    """What Zeta believes the person is feeling right now, and why."""

    label: str = "neutral"
    valence: float = 0.0
    arousal: float = 0.35
    confidence: float = 0.0          # 0 = no evidence, 1 = strong agreement between channels
    intensity: float = 0.0           # how far from neutral (0..1)
    cues: List[str] = field(default_factory=list)          # human-readable evidence
    sources: Dict[str, Any] = field(default_factory=dict)  # per-channel detail (text / voice / events)
    crisis: Optional[Dict[str, Any]] = None                # set by crisis.py when support is needed
    text: str = ""                                         # what was said (trimmed)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def needs_support(self) -> bool:
        return bool(self.crisis) or (self.label in SUPPORT_LABELS and self.confidence >= 0.3 and self.intensity >= 0.25)

    @property
    def is_positive(self) -> bool:
        return self.label in POSITIVE_LABELS

    def describe(self) -> str:
        """One line for the prompt / activity log."""
        desc = LABELS.get(self.label, (0, 0, self.label))[2]
        conf = "clearly" if self.confidence >= 0.65 else "probably" if self.confidence >= 0.4 else "possibly"
        line = f"{conf} {desc}"
        if self.cues:
            line += f" ({', '.join(self.cues[:4])})"
        return line

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label, "valence": round(self.valence, 3), "arousal": round(self.arousal, 3),
            "confidence": round(self.confidence, 3), "intensity": round(self.intensity, 3),
            "cues": self.cues, "sources": self.sources, "crisis": self.crisis,
            "needs_support": self.needs_support, "description": self.describe(),
            "text": self.text[:200], "at": self.at.isoformat(),
        }

    @classmethod
    def from_dimensions(cls, valence: float, arousal: float, confidence: float, cues: List[str],
                        sources: Optional[Dict[str, Any]] = None, label: str = "") -> "EmotionState":
        valence, arousal = clamp(valence), clamp(arousal, 0.0, 1.0)
        intensity = clamp(math.sqrt(valence * valence + (arousal - 0.35) ** 2) / 1.05, 0.0, 1.0)
        return cls(label=label or nearest_label(valence, arousal), valence=valence, arousal=arousal,
                   confidence=clamp(confidence, 0.0, 1.0), intensity=intensity, cues=cues, sources=sources or {})


NEUTRAL = EmotionState()
