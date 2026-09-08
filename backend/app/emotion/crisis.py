"""Recognising when someone is in real trouble, and responding like a decent human.

Zeta is not a therapist and must never pretend to be one.  What it *can* do is
notice serious distress, stay warm and present instead of switching into task mode,
and make sure the person knows where real help is - once, gently, without lecturing.

Design rules encoded here:
* Precision over recall for the loudest tier: idioms ("this deadline is killing me",
  "dying of laughter") must not trigger a crisis response, or the feature becomes
  noise and people stop talking to it.
* Tiered: `concern` (heavy but not dangerous) only softens the tone; `crisis`
  (self-harm / suicidal intent) adds resources; `emergency` (immediate danger)
  says plainly to call emergency services.
* Resources are configurable per region because a wrong phone number is worse
  than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# --- patterns ---------------------------------------------------------------
# Explicit intent. Kept narrow on purpose.
_CRISIS = [
    r"\bkill(ing)?\s+my\s?self\b", r"\bkms\b", r"\bend(ing)?\s+(my|it)\s+(life|all)\b",
    r"\bsuicid(e|al)\b", r"\btake\s+my\s+own\s+life\b", r"\bdon'?t\s+want\s+to\s+(live|be here|exist|wake up)\b",
    r"\b(want|wish)\s+(to\s+)?(be\s+)?(die|dead|disappear forever)\b", r"\bwish\s+i\s+(was|were)\s+dead\b",
    r"\bbetter\s+off\s+(without\s+me|dead)\b", r"\bno\s+reason\s+to\s+(live|go on)\b",
    r"\bcut(ting)?\s+my\s?self\b", r"\bself[\s-]?harm\b", r"\bhurt(ing)?\s+my\s?self\b",
    r"\boverdos(e|ing)\b", r"\bend\s+the\s+pain\s+(forever|permanently)\b",
]
_EMERGENCY = [
    r"\b(i|i'?m)\s+(am\s+)?going\s+to\s+(kill|end)\s+my\s?self\b", r"\btonight\s+i('?ll| will)\s+end\b",
    r"\bi\s+(have|took)\s+(taken\s+)?(the\s+)?pills\b", r"\bi'?m\s+about\s+to\s+(jump|do it)\b",
    r"\bwrote\s+(a\s+)?(suicide\s+)?note\b", r"\bsaying\s+goodbye\s+forever\b",
]
# Serious but not self-harm: still deserves a gentler, slower Zeta.
_CONCERN = [
    r"\bcan'?t\s+(do|take|cope with)\s+(this|it)\s+(any\s?more|anymore)\b", r"\bbreaking\s+down\b",
    r"\bfalling\s+apart\b", r"\bnothing\s+matters\b", r"\bno\s+one\s+(cares|would notice)\b",
    r"\bhate\s+my\s?self\b", r"\bworthless\b", r"\bhopeless\b", r"\bgive\s+up\s+on\s+everything\b",
    r"\bpanic\s+attack\b", r"\bcan'?t\s+stop\s+crying\b", r"\bcompletely\s+alone\b",
    r"\bbeing\s+(abused|hit|threatened)\b", r"\bafraid\s+(of|for)\s+my\s+(life|safety)\b",
]
# Figures of speech that must never count.
_IDIOM = [
    r"\b(deadline|homework|exam|boss|traffic|heat|workout|assignment|meeting)s?\s+(is|are)\s+killing\s+me\b",
    r"\bkilling\s+me\b(?!\s*$)", r"\bdying\s+(of|to)\s+(laugh|laughter|know|see|hear|try)\b",
    r"\bdead\s+(tired|serious|line|body|end)\b", r"\bkill(ed|ing)?\s+(it|the game|time|the vibe)\b",
    r"\bi'?m\s+dead\b\s*(😂|🤣|lol|lmao)", r"\bkill\s+(the|that)\s+(process|server|app|task|switch)\b",
    r"\bsuicide\s+(squad|mission|prevention (hotline|line|helpline))\b",
]

_CRISIS_RE = [re.compile(p, re.I) for p in _CRISIS]
_EMERGENCY_RE = [re.compile(p, re.I) for p in _EMERGENCY]
_CONCERN_RE = [re.compile(p, re.I) for p in _CONCERN]
_IDIOM_RE = [re.compile(p, re.I) for p in _IDIOM]

# --- resources --------------------------------------------------------------
RESOURCES: Dict[str, List[Dict[str, str]]] = {
    "in": [
        {"name": "Tele-MANAS (Government of India, 24x7, many languages)", "contact": "14416 or 1-800-891-4416"},
        {"name": "AASRA (24x7)", "contact": "+91 98204 66726"},
        {"name": "Vandrevala Foundation (24x7)", "contact": "+91 99996 66555"},
        {"name": "Emergency services", "contact": "112"},
    ],
    "us": [
        {"name": "988 Suicide & Crisis Lifeline (24x7)", "contact": "call or text 988"},
        {"name": "Crisis Text Line", "contact": "text HOME to 741741"},
        {"name": "Emergency services", "contact": "911"},
    ],
    "uk": [
        {"name": "Samaritans (24x7)", "contact": "116 123"},
        {"name": "Shout", "contact": "text SHOUT to 85258"},
        {"name": "Emergency services", "contact": "999"},
    ],
    "intl": [
        {"name": "Find a helpline in your country", "contact": "findahelpline.com"},
        {"name": "International Association for Suicide Prevention", "contact": "iasp.info/resources/Crisis_Centres"},
    ],
}


@dataclass
class CrisisSignal:
    level: str                 # concern | crisis | emergency
    matched: List[str]
    resources: List[Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "matched": self.matched, "resources": self.resources}

    def guidance(self) -> str:
        """Instructions injected into the prompt for this turn."""
        lines = []
        if self.level == "emergency":
            lines += [
                "SAFETY: this person may be in immediate danger.",
                "Respond with calm warmth first. Say clearly and kindly that you want them to be safe and that they should",
                "contact emergency services or a crisis line right now, and reach a person who can be with them.",
                "Do not run tools, do not change the subject, do not give a lecture. Stay with them.",
            ]
        elif self.level == "crisis":
            lines += [
                "SAFETY: this person has expressed thoughts of suicide or self-harm.",
                "Take it seriously and gently. Acknowledge the pain in their own words before anything else.",
                "Do not minimise it, do not be cheerful, do not offer productivity advice, do not ask them to justify it.",
                "Ask, without pressure, whether they are safe right now and whether there is someone they can be with.",
                "Mention the helplines below once, plainly, as an option - not as a way to end the conversation.",
                "You are not a therapist: say you are glad they told you, and that a person trained for this can help more.",
            ]
        else:
            lines += [
                "SAFETY: this person sounds like they are struggling badly.",
                "Slow down. Lead with understanding, not solutions. Ask one gentle, open question and let them talk.",
                "Only suggest practical help if they ask for it or clearly want it.",
            ]
        if self.resources:
            lines.append("Resources you may offer: " + "; ".join(f"{r['name']} - {r['contact']}" for r in self.resources))
        return "\n".join(lines)


def detect(text: str, region: str = "in") -> Optional[CrisisSignal]:
    """Return a CrisisSignal when a message needs a supportive (not task-focused) response."""
    if not text or not text.strip():
        return None
    for rx in _IDIOM_RE:
        if rx.search(text):
            # An idiom cancels the loud tiers, but genuine explicit intent elsewhere still counts.
            if not any(rx2.search(text) for rx2 in _EMERGENCY_RE + _CRISIS_RE if "kill" not in rx2.pattern):
                text = rx.sub(" ", text)

    matched = [rx.pattern for rx in _EMERGENCY_RE if rx.search(text)]
    if matched:
        return CrisisSignal("emergency", matched, resources(region))
    matched = [rx.pattern for rx in _CRISIS_RE if rx.search(text)]
    if matched:
        return CrisisSignal("crisis", matched, resources(region))
    matched = [rx.pattern for rx in _CONCERN_RE if rx.search(text)]
    if matched:
        return CrisisSignal("concern", matched, [])
    return None


def resources(region: str = "in") -> List[Dict[str, str]]:
    region = (region or "in").strip().lower()
    return RESOURCES.get(region, []) + RESOURCES["intl"]
