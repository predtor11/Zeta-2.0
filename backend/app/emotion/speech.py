"""Choosing *how* Zeta says something, and what exactly the voice is handed.

Three mechanisms, all optional and all degrading cleanly:

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
   a smaller set of *sounds* (`[laugh]`, `[sigh]`, `[chuckle]`, `[gasp]`), so tag names
   are translated for it and the ones it cannot do are dropped.

Everything a voice speaks goes through `prepare()`, which is also where Markdown is
removed.  A model that writes "**Done.** I've opened `chrome.exe`" must not be read
out as "star star Done star star" - that was the single most robotic thing about
Zeta's voice.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.emotion.state import EmotionState

# Tags a v3 voice can actually perform. Anything else is stripped rather than spoken.
ALLOWED_TAGS = {
    "laughs", "laughs harder", "giggles", "chuckles", "sighs", "exhales", "whispers", "softly",
    "gently", "warmly", "excited", "happy", "sad", "curious", "thoughtful", "reassuring",
    "sarcastic", "hesitates", "short pause", "long pause", "clears throat", "breathes", "gasps",
}
_TAG_RE = re.compile(r"\[([a-z][a-z ']{1,24})\]", re.I)
TAG_MODELS = ("eleven_v3", "eleven_v3_alpha", "chatterbox")

# Chatterbox speaks a smaller dialect: real sounds, not moods. Translate what maps, drop the rest.
# The right-hand side must stay inside the TAGS set in scripts/chatterbox_server.py.
# Which of those it *performs* was measured, not assumed: synthesize "[tag] That is a shame..."
# and transcribe the clip back with Whisper. sigh, chuckle, laugh, gasp, sniff and cough come
# back as sound; breath, giggle, whisper and "clears throat" come back with the tag read out as
# a word ("Breath. That is a shame..."). So those four are mapped away, not passed through.
CHATTERBOX_TAGS = {
    "laughs": "laugh", "laughs harder": "laugh", "chuckles": "chuckle", "giggles": "chuckle",
    "sighs": "sigh", "exhales": "sigh", "breathes": "sigh", "hesitates": "sigh", "gasps": "gasp",
}

# ...and which of them each Chatterbox model performs rather than pronounces. Turbo and
# multilingual genuinely differ - measured the same way on both, multilingual says "Laugh."
# and "Chuckle," out loud where turbo performs them - so this is keyed by model, and a sound
# missing from the set is dropped rather than spoken.
CHATTERBOX_SOUNDS = {
    "chatterbox": {"sigh", "chuckle", "laugh", "gasp", "sniff", "cough"},
    "chatterbox-multilingual": {"sigh", "gasp"},
}


def chatterbox_sounds(model: str) -> set:
    return CHATTERBOX_SOUNDS.get(model, CHATTERBOX_SOUNDS["chatterbox"])

# Sounds are meant to be occasional. Zeta doing the same little sigh before every gentle
# reply is more obviously synthetic than no sound at all, so each one is a coin flip.
_rng = random.Random()


@dataclass
class SpeechStyle:
    name: str = "neutral"
    settings: Dict[str, Any] = field(default_factory=dict)      # ElevenLabs voice_settings
    lead_tag: str = ""            # delivery tag placed at the start (tag-capable models only)
    description: str = ""
    chatterbox: Dict[str, Any] = field(default_factory=dict)    # Chatterbox generation knobs
    sound: str = ""               # Chatterbox sound to open with, e.g. "sigh"; "" = none
    sound_chance: float = 0.0     # how often to actually use it (0 = never, 1 = every reply)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "settings": self.settings, "lead_tag": self.lead_tag,
                "description": self.description, "chatterbox": self.chatterbox,
                "sound": self.sound, "sound_chance": self.sound_chance}


def _vs(stability: float, style: float, boost: bool = True, similarity: float = 0.8) -> Dict[str, Any]:
    return {"stability": stability, "similarity_boost": similarity, "style": style, "use_speaker_boost": boost}


def _cb(exaggeration: float, cfg_weight: float, temperature: float = 0.8) -> Dict[str, Any]:
    """Chatterbox: exaggeration is how much emotion to act, cfg_weight is pacing (lower = slower)."""
    return {"exaggeration": exaggeration, "cfg_weight": cfg_weight, "temperature": temperature}


STYLES: Dict[str, SpeechStyle] = {
    "neutral":     SpeechStyle("neutral", _vs(0.55, 0.25), "", "even and clear", _cb(0.45, 0.50)),
    "warm":        SpeechStyle("warm", _vs(0.62, 0.40), "warmly", "warm and friendly", _cb(0.55, 0.45)),
    "gentle":      SpeechStyle("gentle", _vs(0.80, 0.20), "gently", "soft, slow, unhurried", _cb(0.35, 0.30, 0.7),
                               sound="sigh", sound_chance=0.30),
    "steady":      SpeechStyle("steady", _vs(0.88, 0.10), "softly", "calm and grounding", _cb(0.30, 0.25, 0.6),
                               sound="sigh", sound_chance=0.25),
    "bright":      SpeechStyle("bright", _vs(0.40, 0.65), "happy", "upbeat, lively", _cb(0.70, 0.50)),
    "delighted":   SpeechStyle("delighted", _vs(0.32, 0.80), "excited", "energised, celebratory", _cb(0.85, 0.55, 0.9),
                               sound="laugh", sound_chance=0.40),
    "playful":     SpeechStyle("playful", _vs(0.35, 0.70), "laughs", "amused", _cb(0.75, 0.45, 0.9),
                               sound="chuckle", sound_chance=0.60),
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


# --------------------------------------------------------------------------- Markdown
# Zeta's replies are Markdown because the chat panel renders them. The voice must not read
# the punctuation out. Everything here turns writing into speech, never the other way round.
_FENCED = re.compile(r"```[^\n]*\n?.*?(?:```|\Z)", re.S)
_CODE_SPOKEN = "The code is on screen."
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"<?\b(?:https?://|www\.)\S+>?")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
_BULLET = re.compile(r"^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+", re.M)
_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t:|-]*-[-\t :|]*\|?[ \t]*$", re.M)
_TABLE_ROW = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.M)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_INLINE_CODE = re.compile(r"`+([^`\n]+)`+")
_EM_STAR = re.compile(r"\*{1,3}(?=[^\s*])(.+?)(?<=[^\s*])\*{1,3}", re.S)
_EM_US = re.compile(r"(?<![\w\\])_{1,3}(?=[^\s_])(.+?)(?<=[^\s_])_{1,3}(?!\w)", re.S)
# Pictographs, dingbats, flags and the variation selectors / joiners that glue them together.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002190-\U000021FF\U00002300-\U000027BF"
                    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF\U0000200D]")
_ENDS_CLEAN = ".!?…:;,।\"')"


def _punctuate(line: str) -> str:
    """A heading or a bullet with no full stop runs into the next one. Give the voice a breath."""
    stripped = line.rstrip()
    if not stripped or stripped[-1] in _ENDS_CLEAN:
        return line
    return f"{stripped}."


def strip_markdown(text: str) -> str:
    """Markdown in, speakable prose out.

    Asterisks, backticks, heading hashes, bullets, table pipes and link URLs are all
    written conventions - read aloud they are noise. Code blocks are replaced with one
    short sentence rather than dictated character by character, and bare URLs become
    "the link", because nobody wants to hear "h t t p s colon slash slash".
    """
    if not text:
        return ""
    out = _FENCED.sub(lambda _: f" {_CODE_SPOKEN} ", text)
    out = _IMAGE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _BARE_URL.sub("the link", out)
    out = _TABLE_SEP.sub("", out)
    out = _TABLE_ROW.sub(lambda m: _punctuate(", ".join(c.strip() for c in m.group(1).split("|") if c.strip())), out)
    out = _RULE.sub("", out)
    out = _QUOTE.sub("", out)
    # A heading or a bullet is a line with no full stop on the end. Read straight through they
    # run into whatever follows, so the marker becomes the sentence break instead.
    out = "\n".join(_punctuate(_HEADING.sub("", line)) if _HEADING.match(line)
                    else _punctuate(_BULLET.sub("", line)) if _BULLET.match(line)
                    else line
                    for line in out.splitlines())
    out = _INLINE_CODE.sub(r"\1", out)
    out = _STRIKE.sub(r"\1", out)
    out = _EM_STAR.sub(r"\1", out)
    out = _EM_US.sub(r"\1", out)
    out = out.replace("*", "").replace("`", "")      # anything unbalanced that survived
    out = _EMOJI.sub(" ", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return "\n".join(line.strip() for line in out.splitlines() if line.strip()).strip()


# --------------------------------------------------------------------------- language
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def language_of(text: str) -> str:
    """The language code a multilingual voice should speak this in, or "" for the default.

    Only script-based detection, which is honest about what it can do: Devanagari means
    Hindi. Romanised Hinglish ("kya haal hai") is indistinguishable from English here and
    is left to the default voice, which handles it passably.
    """
    return "hi" if _DEVANAGARI.search(text or "") else ""


# --------------------------------------------------------------------------- splitting
_SENT_END = re.compile(r"(?<=[.!?…।])[\s\n]+")


def segments(text: str, first_limit: int = 160, limit: int = 300) -> List[str]:
    """Split a reply into pieces that can be spoken one after another.

    The voice generates a whole clip before returning anything, so a long answer means a
    long silence. Splitting lets playback start after the first sentence while the rest is
    still being made. The first piece is deliberately short - it is the one the person is
    waiting on - and later pieces are longer, because fewer joins means better prosody.
    """
    words = _SENT_END.split(text.strip()) if text.strip() else []
    out: List[str] = []
    current = ""
    for sentence in words:
        sentence = " ".join(sentence.split())
        if not sentence:
            continue
        cap = first_limit if not out else limit
        while len(sentence) > limit:                       # one enormous sentence
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit * 0.6 else limit
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
            cap = limit
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= cap:
            current = f"{current} {sentence}"
        else:
            out.append(current)
            current = sentence
    if current:
        out.append(current)
    return out


# --------------------------------------------------------------------------- delivery
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
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), found


def for_display(text: str) -> str:
    """What the user reads: never contains delivery tags. Markdown is kept - the UI renders it."""
    return split_tags(text)[0]


def for_voice(text: str) -> str:
    """What a voice with no delivery tags of its own receives: prose, no Markdown, no tags."""
    return for_display(strip_markdown(text))


def prepare(text: str, style: SpeechStyle, model: str, lead: bool = True) -> str:
    """What the voice receives: no Markdown, tags translated per engine or stripped.

    `lead` is False for every piece of a reply after the first, so a streamed answer opens
    with one breath rather than sighing at the start of each sentence.
    """
    spoken = strip_markdown(text)
    supports_tags = any(model.startswith(m) for m in TAG_MODELS)
    if not supports_tags:
        return for_display(spoken)
    if model.startswith("chatterbox"):
        return _for_chatterbox(spoken, style, lead, model)
    cleaned, _ = split_tags(spoken)
    if lead and style.lead_tag and style.lead_tag in ALLOWED_TAGS:
        return f"[{style.lead_tag}] {cleaned}"
    return cleaned


def _for_chatterbox(text: str, style: SpeechStyle, lead: bool = True, model: str = "chatterbox") -> str:
    """Chatterbox performs sounds, not moods: keep [laugh]/[sigh], drop [warmly] and friends.

    The mood is carried by `style.chatterbox` (exaggeration and pacing) instead, so nothing
    is lost. The style's own sound is only used sometimes - see `_rng` - and only when this
    particular model performs it rather than reading it out.
    """
    performs = chatterbox_sounds(model)

    def take(m: re.Match) -> str:
        tag = m.group(1).strip().lower()
        mapped = CHATTERBOX_TAGS.get(tag, tag if tag in CHATTERBOX_TAGS.values() else "")
        return f"[{mapped}]" if mapped in performs else " "

    cleaned = re.sub(r"[ \t]{2,}", " ", _TAG_RE.sub(take, text)).strip()
    # One cue per reply: a tag the model already wrote wins over the style's default.
    if not lead or style.sound not in performs or _TAG_RE.search(cleaned):
        return cleaned
    return f"[{style.sound}] {cleaned}" if _rng.random() < style.sound_chance else cleaned


def prompt_note(model: str) -> str:
    """Tells the model it may write delivery tags - only when the voice can perform them."""
    if not any(model.startswith(m) for m in TAG_MODELS):
        return ""
    if model.startswith("chatterbox"):
        performs = chatterbox_sounds(model)
        names = {"sigh": "[sighs]", "laugh": "[laughs]", "chuckle": "[chuckles]", "gasp": "[gasps]"}
        cues = ", ".join(v for k, v in names.items() if k in performs)
    else:
        cues = "[laughs], [giggles], [sighs], [gently], [warmly], [excited]"
    return ("How you sound: your reply is spoken aloud as well as written, so write it the way you would say it. "
            "Contractions, ordinary words, short sentences. No bullet lists or headings when a sentence will do.\n"
            f"You may add at most one delivery cue in square brackets where a person would naturally make that sound, "
            f"e.g. {cues}. They are performed by the voice and hidden from the written transcript. Never use them to "
            "fake an emotion you were not asked for, and never more than one per reply.")
