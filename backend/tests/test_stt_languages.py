"""Whisper knows 99 languages. This household speaks two.

Left to choose freely, Whisper will return Welsh or Urdu for a two-second "hey, open Chrome" -
the model is not wrong to be uncertain, it is just answering a question nobody asked. Narrowing
the choice to the languages that can actually occur removes that whole class of failure without
pinning a single language, which would break the other one.
"""

from __future__ import annotations

import pytest

from app.providers.stt import WhisperSTT


class _Model:
    """Stands in for faster-whisper: `detect_language` returns every language it knows about."""

    def __init__(self, probs, fail: bool = False):
        self.probs = probs
        self.fail = fail
        self.asked = 0

    def detect_language(self, audio=None, vad_filter=False, **kw):
        self.asked += 1
        if self.fail:
            raise RuntimeError("detection blew up")
        best = max(self.probs, key=lambda kv: kv[1])
        return best[0], best[1], self.probs


def _stt(languages: str, probs, fail: bool = False) -> WhisperSTT:
    s = WhisperSTT("small", languages=languages)
    s._model = _Model(probs, fail)
    return s


PROBS = [("cy", 0.44), ("en", 0.31), ("hi", 0.18), ("ur", 0.07)]


def test_a_language_nobody_speaks_is_not_chosen():
    """Welsh wins on raw probability here, and is exactly the wrong answer."""
    assert _stt("en,hi", PROBS)._detect("x.wav") == "en"


def test_hindi_still_wins_when_hindi_is_what_was_said():
    probs = [("cy", 0.30), ("en", 0.12), ("hi", 0.55)]
    assert _stt("en,hi", probs)._detect("x.wav") == "hi"


def test_no_shortlist_means_whisper_decides_for_itself():
    """Returning None leaves `transcribe(language=None)` to do what it always did."""
    s = _stt("", PROBS)
    assert s._detect("x.wav") is None
    assert s._model.asked == 0            # and it is not even asked


def test_one_language_is_not_worth_detecting():
    s = _stt("hi", PROBS)
    assert s._detect("x.wav") == "hi"
    assert s._model.asked == 0


def test_detection_failing_is_not_fatal():
    """A broken detector must not take the transcription down with it."""
    assert _stt("en,hi", PROBS, fail=True)._detect("x.wav") is None


def test_a_shortlist_nothing_matches_falls_back_to_the_first():
    """Rather than hand back a language the user has said does not occur."""
    assert _stt("en,hi", [("ja", 0.9), ("ko", 0.1)])._detect("x.wav") == "en"


@pytest.mark.parametrize("raw, expected", [
    ("en,hi", ["en", "hi"]),
    (" EN , HI ", ["en", "hi"]),
    ("", []),
    ("en,,hi,", ["en", "hi"]),
])
def test_the_shortlist_is_read_forgivingly(raw, expected):
    assert WhisperSTT("small", languages=raw).languages == expected


def test_pinning_a_language_still_overrides_everything():
    """STT_LANGUAGE is the blunt instrument and must keep working."""
    s = WhisperSTT("small", language="hi", languages="en,hi")
    assert s.language == "hi"
