"""Choosing *how* Zeta says something.

Two mechanisms, both optional and both degrade cleanly:

1. `voice_settings` - works on every ElevenLabs model and on the free tier.
   Lower stability + higher style = more expressive; high stability = steady and
   calm, which is what you want when someone is upset.

2. Inline audio tags - `[laughs]`, `[gently]`, `[warmly]`, `[sighs]`.  Verified
   on a free-tier key: `eleven_v3` *performs* them (a round-trip through Scribe
   comes back as a laughter event), while `eleven_multilingual_v2` reads the word
   "laughs" out loud.  So tags are only ever sent when the model is v3, and are
   always stripped from the text shown in the UI.

3. Chatterbox generation knobs - `exaggeration` (how much emotion to act) and
   `cfg_weight` (pacing: lower is slower and more deliberate).  Chatterbox performs
   a smaller set of tags (`[laugh]`, `[sigh]`, `[chuckle]`...), so the tag names are
   translated for it and the ones it cannot do are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.emotion.state import EmotionState

# Tags a v3 voice can actually perform. Anything else is stripped rather than spoken.
ALLOWED_TAGS = {
    "laughs", "laughs harder", "giggles", "chuckles", "sighs", "exhales", "whispers", "softly",
    "gently", "warmly", "excited", "happy", "sad", "curious", "thoughtful", "reassuring",
    "sarcastic", "hesitates", "short pause", "long pause", "clears throat",
}
_TAG_RE = re.compile(r"\[([a-z][a-z ']{1,24})\]", re.I)
TAG_MODELS = ("eleven_v3", "eleven_v3_alpha", "chatterbox")

# Chatterbox speaks a smaller dialect of tags; translate what maps and drop the rest.
CHATTERBOX_TAGS = {
    "laughs": "laugh", "laughs harder": "laugh", "giggles": "giggle", "chuckles": "chuckle",
    "sighs": "sigh", "exhales": "sigh", "whispers": "whisper", "clears throat": "clears throat",
}


@dataclass
class SpeechStyle:
    name: str = "neutral"
    settings: Dict[str, Any] = field(default_factory=dict)      # ElevenLabs voice_settings
    lead_tag: str = ""            # delivery tag placed at the start (tag-capable models only)
    description: str = ""
    chatterbox: Dict[str, Any] = field(default_factory=dict)    # Chatterbox generation knobs

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "settings": self.settings, "lead_tag": self.lead_tag,
                "description": self.description, "chatterbox": self.chatterbox}


def _vs(stability: float, style: float, boost: bool = True, similarity: float = 0.8) -> Dict[str, Any]:
    return {"stability": stability, "similarity_boost": similarity, "style": style, "use_speaker_boost": boost}


def _cb(exaggeration: float, cfg_weight: float, temperature: float = 0.8) -> Dict[str, Any]:
    """Chatterbox: exaggeration is how much emotion to act, cfg_weight is pacing (lower = slower)."""
    return {"exaggeration": exaggeration, "cfg_weight": cfg_weight, "temperature": temperature}


STYLES: Dict[str, SpeechStyle] = {
    "neutral":     SpeechStyle("neutral", _vs(0.55, 0.25), "", "even and clear", _cb(0.45, 0.50)),
    "warm":        SpeechStyle("warm", _vs(0.62, 0.40), "warmly", "warm and friendly", _cb(0.55, 0.45)),
    "gentle":      SpeechStyle("gentle", _vs(0.80, 0.20), "gently", "soft, slow, unhurried", _cb(0.35, 0.30, 0.7)),
    "steady":      SpeechStyle("steady", _vs(0.88, 0.10), "softly", "calm and grounding", _cb(0.30, 0.25, 0.6)),
    "bright":      SpeechStyle("bright", _vs(0.40, 0.65), "happy", "upbeat, lively", _cb(0.70, 0.50)),
    "delighted":   SpeechStyle("delighted", _vs(0.32, 0.80), "excited", "energised, celebratory", _cb(0.85, 0.55, 0.9)),
    "playful":     SpeechStyle("playful", _vs(0.35, 0.70), "laughs", "amused", _cb(0.75, 0.45, 0.9)),
    "thoughtful":  SpeechStyle("thoughtful", _vs(0.70, 0.25), "thoughtful", "measured, considered", _cb(0.40, 0.35, 0.7)),
}


def style_for(state: Optional[EmotionState], reply: str = "", enabled: bool = True) -> SpeechStyle:
    """Pick a delivery for Zeta's reply, given how the *person* seems."""
    if not enabled:
        return STYLES["neutral"]
    if state is None or state.confidence < 0.25:
        return STYLES["warm"] if _sounds_playful(reply) else STYLES["neutral"]
    if state.crisis and state.crisis.get("level") in ("crisis", "emergency"):
        return STYLES["steady"]
    if state.needs_support:
        # High arousal distress needs grounding; low arousal sadness needs gentleness.
        return STYLES["steady"] if state.arousal >= 0.6 else STYLES["gentle"]
    if state.is_positive and state.intensity >= 0.5:
        return STYLES["delighted"] if state.arousal >= 0.7 else STYLES["bright"]
    if _sounds_playful(reply):
        return STYLES["playful"]
    if state.label in ("confused", "bored"):
        return STYLES["thoughtful"]
    return STYLES["warm"] if state.valence >= 0 else STYLES["gentle"]


def _sounds_playful(reply: str) -> bool:
    r = (reply or "").lower()
    return any(k in r for k in ("haha", "ha ha", "😄", "😂", "🙂", "funny", "that's cheeky")) or "[laughs]" in r


def split_tags(text: str) -> tuple:
    """Return (text_without_tags, tags_found) - used to keep the UI text clean."""
    found: List[str] = []

    def take(m: re.Match) -> str:
        tag = m.group(1).strip().lower()
        if tag in ALLOWED_TAGS:
            found.append(tag)
            return " "
        return m.group(0)

    cleaned = _TAG_RE.sub(take, text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(), found


def for_display(text: str) -> str:
    """What the user reads: never contains delivery tags."""
    return split_tags(text)[0]


def prepare(text: str, style: SpeechStyle, model: str) -> str:
    """What the voice receives: tags kept (translated per engine) or stripped, plus a lead tag."""
    supports_tags = any(model.startswith(m) for m in TAG_MODELS)
    if not supports_tags:
        return for_display(text)
    if model.startswith("chatterbox"):
        return _for_chatterbox(text, style)
    cleaned, _ = split_tags(text)
    if style.lead_tag and style.lead_tag in ALLOWED_TAGS:
        return f"[{style.lead_tag}] {cleaned}"
    return cleaned


def _for_chatterbox(text: str, style: SpeechStyle) -> str:
    """Chatterbox performs sounds, not moods: keep [laugh]/[sigh], drop [warmly] and friends.

    The mood is carried by `style.chatterbox` (exaggeration and pacing) instead, so nothing is lost.
    """
    def take(m: re.Match) -> str:
        tag = m.group(1).strip().lower()
        mapped = CHATTERBOX_TAGS.get(tag, tag if tag in CHATTERBOX_TAGS.values() else "")
        return f"[{mapped}]" if mapped else " "

    cleaned = re.sub(r"\s{2,}", " ", _TAG_RE.sub(take, text)).strip()
    lead = CHATTERBOX_TAGS.get(style.lead_tag, "")
    # One cue per reply: a tag the model already wrote wins over the style's default.
    return f"[{lead}] {cleaned}" if lead and not _TAG_RE.search(cleaned) else cleaned


def prompt_note(model: str) -> str:
    """Tells the model it may write delivery tags - only when the voice can perform them."""
    if not any(model.startswith(m) for m in TAG_MODELS):
        return ""
    cues = "[laughs], [giggles], [sighs], [chuckles]" if model.startswith("chatterbox") else \
           "[laughs], [giggles], [sighs], [gently], [warmly], [excited]"
    return (f"You may add at most one delivery cue in square brackets when it is genuinely warranted, e.g. {cues}. "
            "They are performed by the voice and hidden from the written transcript. Never use them to fake an "
            "emotion you were not asked for, and never more than one per reply.")
