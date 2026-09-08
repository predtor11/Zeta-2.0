"""What was said: emotion from the words themselves.

A compact, offline lexicon (no model call, ~1 ms) that handles the things that
actually change meaning in speech: negation ("not great"), intensifiers ("really
tired"), downtoners ("a bit sad"), repetition, emphasis, emoji, and the audio-event
tags ElevenLabs Scribe emits for real laughter or sighs.

It is deliberately conservative: it reports weak confidence unless several signals
agree.  The LLM sees the transcript anyway; this exists so Zeta can *adapt* before
and independently of what the model decides to say.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# word -> (valence, arousal contribution, label hint)
LEX: Dict[str, Tuple[float, float, str]] = {}


def _add(words: str, valence: float, arousal: float, label: str = "") -> None:
    for w in words.split():
        LEX[w] = (valence, arousal, label)


# negative, low arousal
_add("sad unhappy down low blue miserable heartbroken grief grieving mourning", -0.75, 0.25, "sad")
_add("lonely alone isolated abandoned unwanted friendless", -0.7, 0.3, "lonely")
_add("tired exhausted drained sleepy fatigued burntout burnt knackered weary", -0.35, 0.08, "tired")
_add("hopeless pointless meaningless worthless useless empty numb", -0.85, 0.3, "distressed")
_add("disappointed letdown gutted bummed", -0.55, 0.35, "disappointed")
_add("hurt betrayed rejected humiliated ashamed guilty embarrassed", -0.65, 0.45, "hurt")
_add("bored boring dull meh", -0.3, 0.2, "bored")
_add("regret sorry apologise apologize", -0.35, 0.35, "")
# negative, high arousal
_add("anxious anxiety worried worry nervous scared uneasy panicking panic dread", -0.6, 0.8, "anxious")
_add("afraid fear terrified frightened", -0.7, 0.8, "afraid")
_add("overwhelmed swamped drowning suffocating toomuch crushing", -0.65, 0.8, "overwhelmed")
_add("stressed stress pressure deadline overworked", -0.5, 0.75, "stressed")
_add("frustrated frustrating annoyed annoying irritated stuck blocked fedup", -0.55, 0.7, "frustrated")
_add("angry furious mad rage pissed livid hate hateful", -0.75, 0.85, "angry")
_add("confused lost unsure clueless puzzled", -0.25, 0.5, "confused")
_add("crying cry tears sobbing weeping", -0.7, 0.55, "sad")
# positive
_add("happy glad pleased delighted joy joyful cheerful", 0.75, 0.6, "happy")
_add("excited thrilled pumped stoked ecstatic amazing awesome fantastic incredible", 0.8, 0.9, "excited")
_add("grateful thankful thanks appreciate blessed", 0.7, 0.4, "grateful")
_add("hopeful optimistic looking forward promising", 0.55, 0.5, "hopeful")
_add("relieved relief finally phew sorted", 0.55, 0.3, "relieved")
_add("calm peaceful relaxed chill fine okay ok alright", 0.3, 0.15, "calm")
_add("content satisfied good great nice lovely wonderful perfect", 0.6, 0.4, "content")
_add("proud accomplished achieved won success succeeded", 0.7, 0.6, "happy")
_add("love loving adore enjoy enjoying like", 0.65, 0.5, "happy")
_add("brilliant excellent superb outstanding perfect flawless", 0.75, 0.6, "happy")
_add("helped helping helpful sorted fixed solved working", 0.5, 0.4, "relieved")
_add("funny hilarious lol lmao haha hahaha", 0.6, 0.7, "happy")
# generic judgement words: weaker, because they attach to anything
_add("awful terrible horrible dreadful appalling nightmare", -0.7, 0.55, "sad")
_add("bad worse worst rubbish crap crappy lousy sucks suck garbage", -0.5, 0.5, "")
_add("broken failing failed failure mistake wrong", -0.45, 0.5, "")
_add("scary creepy dangerous unsafe", -0.55, 0.7, "afraid")
_add("desperate helpless trapped stuck", -0.7, 0.65, "overwhelmed")

INTENSIFIERS = {"very": 1.5, "really": 1.5, "so": 1.45, "extremely": 1.9, "incredibly": 1.8, "totally": 1.5,
                "completely": 1.7, "absolutely": 1.7, "super": 1.5, "such": 1.35, "too": 1.4, "damn": 1.6,
                "fucking": 1.9, "bloody": 1.5, "always": 1.3, "never": 1.3, "constantly": 1.5}
DOWNTONERS = {"bit": 0.55, "little": 0.55, "slightly": 0.5, "kinda": 0.6, "kind": 0.6, "sort": 0.6,
              "somewhat": 0.6, "maybe": 0.7, "sometimes": 0.7, "abit": 0.55}
NEGATORS = {"not", "no", "never", "cant", "cannot", "dont", "doesnt", "didnt", "wont", "isnt", "arent",
            "wasnt", "werent", "aint", "hardly", "barely", "without", "nothing", "nobody"}

EMOJI = {"🙂": (0.5, 0.4), "😊": (0.7, 0.5), "😀": (0.75, 0.7), "😃": (0.75, 0.75), "😄": (0.8, 0.8), "😁": (0.75, 0.75),
         "😂": (0.7, 0.85), "🤣": (0.75, 0.9), "❤": (0.8, 0.5), "❤️": (0.8, 0.5), "🥰": (0.85, 0.5), "😍": (0.8, 0.7),
         "👍": (0.5, 0.4), "🎉": (0.8, 0.8), "🙏": (0.5, 0.35), "😌": (0.45, 0.2),
         "🙁": (-0.5, 0.35), "☹": (-0.55, 0.35), "😞": (-0.6, 0.3), "😢": (-0.7, 0.45), "😭": (-0.8, 0.6),
         "😰": (-0.65, 0.8), "😨": (-0.7, 0.8), "😡": (-0.75, 0.85), "🤬": (-0.85, 0.9), "😤": (-0.5, 0.7),
         "😩": (-0.6, 0.6), "😫": (-0.6, 0.6), "😴": (-0.2, 0.05), "🥺": (-0.4, 0.5), "💔": (-0.8, 0.5)}

# Audio events: Scribe (tag_audio_events) emits these in brackets/parens for real laughter, sighs...
AUDIO_EVENTS: Dict[str, Tuple[float, float, str, str]] = {
    "laugh": (0.7, 0.75, "happy", "laughter"),
    "laughs": (0.7, 0.75, "happy", "laughter"),
    "laughter": (0.7, 0.75, "happy", "laughter"),
    "giggles": (0.7, 0.7, "happy", "giggling"),
    "chuckles": (0.55, 0.55, "content", "chuckling"),
    "sigh": (-0.45, 0.2, "tired", "a sigh"),
    "sighs": (-0.45, 0.2, "tired", "a sigh"),
    "cries": (-0.8, 0.6, "sad", "crying"),
    "crying": (-0.8, 0.6, "sad", "crying"),
    "sobs": (-0.85, 0.65, "sad", "crying"),
    "sniffles": (-0.6, 0.4, "sad", "sniffling"),
    "gasps": (-0.2, 0.8, "", "a gasp"),
    "groans": (-0.5, 0.5, "frustrated", "a groan"),
    "yawns": (-0.15, 0.05, "tired", "a yawn"),
    "hesitates": (-0.2, 0.45, "", "hesitation"),
}

# --- phrases ----------------------------------------------------------------
# Multi-word expressions carry more meaning than their parts ("tired of" is
# frustration, not sleepiness; "got the job" is joy with no emotional word in it).
# A phrase match is scored, then blanked out so the word pass cannot double-count
# or contradict it.
_PHRASE_SRC: list = [
    # good news
    (r"\bgot (the|a|my) (job|offer|role|internship|promotion|place|admit)\b", 0.85, 0.85, "excited", "shared good news"),
    (r"\b(i|we) (got|made) (it|in)\b", 0.7, 0.8, "excited", "shared good news"),
    (r"\b(i|we) (passed|cleared|aced|won|nailed|cracked)\b", 0.8, 0.8, "happy", "shared good news"),
    (r"\bgood news\b", 0.7, 0.7, "happy", "good news"),
    (r"\b(it|that) (finally )?work(s|ed)\b", 0.6, 0.6, "relieved", "something worked"),
    (r"\b(so|really|very) proud\b", 0.75, 0.6, "happy", "pride"),
    (r"\bbest (day|news|thing)\b", 0.8, 0.7, "happy", "superlative praise"),
    (r"\bcan'?t wait\b", 0.6, 0.7, "excited", "anticipation"),
    (r"\bthank you so much\b", 0.7, 0.5, "grateful", "warm thanks"),
    (r"\bfeeling (better|good|great)\b", 0.6, 0.4, "content", "feeling better"),
    (r"\b(feel|feeling|doing|so|much|lot) (much |a lot |so much )?better\b", 0.6, 0.4, "relieved", "feeling better"),
    (r"\b(that|it|this) (really )?help(s|ed)\b", 0.55, 0.4, "grateful", "something helped"),
    # bad news / frustration
    (r"\b(tired|sick|fed up) of\b", -0.6, 0.65, "frustrated", "worn down by something"),
    (r"\bnothing (i do )?(ever )?work(s|ed)?\b", -0.7, 0.65, "frustrated", "nothing is working"),
    (r"\b(is|are|it'?s) (not|n'?t) work(ing)?\b", -0.45, 0.6, "frustrated", "something is broken"),
    (r"\bwhy is (nothing|this|it) (not )?\w+ing\b", -0.5, 0.75, "frustrated", "exasperated question"),
    (r"\b(messed|screwed|mucked) (it |things )?up\b", -0.5, 0.6, "disappointed", "self-blame"),
    (r"\b(let|letting) (me|us|everyone) down\b", -0.6, 0.5, "disappointed", "being let down"),
    (r"\bcan'?t (do|take|handle|cope with) (this|it)\b", -0.75, 0.7, "overwhelmed", "at their limit"),
    (r"\bno idea what to do\b", -0.45, 0.6, "confused", "feeling lost"),
    (r"\bfalling apart\b", -0.8, 0.6, "distressed", "coming apart"),
    (r"\bworst (day|week|thing)\b", -0.75, 0.6, "sad", "superlative complaint"),
    (r"\bcan'?t sleep\b", -0.45, 0.55, "anxious", "not sleeping"),
    (r"\b(i )?miss (him|her|them|you|my)\b", -0.5, 0.35, "sad", "missing someone"),
    (r"\bwaste of (time|my time|effort)\b", -0.5, 0.5, "frustrated", "wasted effort"),
    (r"\bnot (okay|ok|fine|good|great|alright)\b", -0.55, 0.45, "sad", "says they are not okay"),
    (r"\bgave up\b", -0.5, 0.3, "disappointed", "giving up"),
    (r"\bon edge\b", -0.5, 0.75, "anxious", "on edge"),
    (r"\bhad enough\b", -0.55, 0.6, "frustrated", "had enough"),
]
_PHRASE_RE = [(re.compile(p, re.I), v, a, lb, cue) for p, v, a, lb, cue in _PHRASE_SRC]


def phrases(text: str):
    """Score multi-word expressions and return (hits, cues, text_without_them)."""
    hits, cues = [], []
    out = text
    for rx, v, a, label, cue in _PHRASE_RE:
        m = rx.search(out)
        if not m:
            continue
        # A negator right before the phrase flips it ("i'm not tired of this").
        head = out[max(0, m.start() - 18):m.start()].lower()
        if any(f" {n} " in f" {head} " for n in ("not", "never", "dont", "don't", "no")):
            continue
        hits.append((v, a, label, abs(v) * 1.4))
        cues.append(cue)
        out = out[:m.start()] + " " + out[m.end():]
    return hits, cues, out


_EVENT_RE = re.compile(r"[\[(]\s*([a-z_ ]{3,20})\s*[\])]", re.I)
_WORD_RE = re.compile(r"[a-zA-Z']+")
_REPEAT_RE = re.compile(r"([!?])\1{1,}")


def strip_audio_events(text: str) -> Tuple[str, List[str]]:
    """Remove `[laughs]` style tags from a transcript, returning (clean_text, events)."""
    events: List[str] = []

    def take(m: re.Match) -> str:
        key = m.group(1).strip().lower().replace(" ", "")
        if key in AUDIO_EVENTS:
            events.append(key)
            return " "
        return m.group(0)

    return _EVENT_RE.sub(take, text).strip(), events


def analyze(text: str) -> Tuple[float, float, float, List[str], str]:
    """(valence, arousal, confidence, cues, label_hint) for a piece of speech or typing."""
    clean, events = strip_audio_events(text)
    phrase_hits, cues, remainder = phrases(clean)
    words = [w.lower().replace("'", "") for w in _WORD_RE.findall(remainder)]
    hits: List[Tuple[float, float, str, float]] = list(phrase_hits)

    for i, w in enumerate(words):
        entry = LEX.get(w)
        if not entry:
            continue
        v, a, label = entry
        weight = 1.0
        window = words[max(0, i - 3):i]
        for prev in window:
            if prev in INTENSIFIERS:
                weight *= INTENSIFIERS[prev]
            elif prev in DOWNTONERS:
                weight *= DOWNTONERS[prev]
        negated = any(p in NEGATORS for p in words[max(0, i - 3):i])
        if negated:
            v = -v * 0.75
            a *= 0.8
            label = ""
            cues.append(f"negated '{w}'")
        else:
            cues.append(f"said '{w}'")
        hits.append((v * min(weight, 2.0), a, label, abs(v) * min(weight, 2.0)))

    for ch in clean:
        if ch in EMOJI:
            v, a = EMOJI[ch]
            hits.append((v, a, "", abs(v)))
            cues.append(f"emoji {ch}")

    ev_label = ""
    for key in events:
        v, a, label, human = AUDIO_EVENTS[key]
        hits.append((v, a, label, abs(v) * 1.2))
        cues.append(human)
        ev_label = ev_label or label

    if not hits:
        # No emotional words: still notice shouting / pleading punctuation.
        arousal = 0.35
        cues2: List[str] = []
        if _REPEAT_RE.search(clean) or (clean.isupper() and len(clean) > 6):
            arousal = 0.7
            cues2.append("emphatic punctuation")
        return 0.0, arousal, 0.12 if cues2 else 0.0, cues2, ""

    total_w = sum(h[3] for h in hits) or 1.0
    valence = sum(h[0] * h[3] for h in hits) / total_w
    arousal = sum(h[1] * h[3] for h in hits) / total_w

    if _REPEAT_RE.search(clean):
        arousal = min(1.0, arousal + 0.15)
        cues.append("emphatic punctuation")
    if len(clean) > 6 and clean.isupper():
        arousal = min(1.0, arousal + 0.2)
        cues.append("all caps")

    # Confidence grows with agreement and evidence count, shrinks on mixed signals.
    signs = {1 if h[0] > 0.05 else -1 if h[0] < -0.05 else 0 for h in hits}
    agreement = 0.55 if len(signs - {0}) > 1 else 1.0
    confidence = min(0.85, (0.32 + 0.16 * len(hits)) * agreement)
    if events:
        confidence = min(0.9, confidence + 0.2)

    strongest = max(hits, key=lambda h: h[3])
    label = ev_label or (strongest[2] if strongest[2] else "")
    return max(-1.0, min(1.0, valence)), max(0.0, min(1.0, arousal)), confidence, cues[:6], label
