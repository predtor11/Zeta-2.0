"""Whisper knows 99 languages. This household speaks two.

Left to choose freely, Whisper will return Welsh or Urdu for a two-second "hey, open Chrome" -
the model is not wrong to be uncertain, it is just answering a question nobody asked. Narrowing
the choice to the languages that can actually occur removes that whole class of failure without
pinning a single language, which would break the other one.
"""

from __future__ import annotations

import pytest

from app.providers.stt import WhisperSTT


@pytest.fixture()
def wav(tmp_path):
    """A real (silent) 16 kHz file. Detection decodes what it is given, so a made-up path would
    only ever exercise the error branch - which is how the path bug hid in the first place."""
    import wave

    import numpy as np

    path = tmp_path / "utterance.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return str(path)


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


def test_a_language_nobody_speaks_is_not_chosen(wav):
    """Welsh wins on raw probability here, and is exactly the wrong answer."""
    assert _stt("en,hi", PROBS)._detect(wav) == "en"


def test_hindi_still_wins_when_hindi_is_what_was_said(wav):
    probs = [("cy", 0.30), ("en", 0.12), ("hi", 0.55)]
    assert _stt("en,hi", probs)._detect(wav) == "hi"


def test_no_shortlist_means_whisper_decides_for_itself(wav):
    """Returning None leaves `transcribe(language=None)` to do what it always did."""
    s = _stt("", PROBS)
    assert s._detect(wav) is None
    assert s._model.asked == 0            # and it is not even asked


def test_one_language_is_not_worth_detecting(wav):
    s = _stt("hi", PROBS)
    assert s._detect(wav) == "hi"
    assert s._model.asked == 0


def test_detection_failing_is_not_fatal(wav):
    """A broken detector must not take the transcription down with it."""
    assert _stt("en,hi", PROBS, fail=True)._detect(wav) is None


def test_a_shortlist_nothing_matches_falls_back_to_the_first(wav):
    """Rather than hand back a language the user has said does not occur."""
    assert _stt("en,hi", [("ja", 0.9), ("ko", 0.1)])._detect(wav) == "en"


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


# ------------------------------------------------------------------ the bug that got through
def test_detection_is_handed_samples_not_a_path(wav):
    """The mistake this pins down, which reached the user: `detect_language` wants a 1-D float
    array at 16 kHz. Given a path it raises, and the fallback then let Whisper choose from all 99
    languages - it returned a confident Portuguese transcript of Hindi speech."""
    import numpy as np

    seen = {}

    class _Picky:
        def detect_language(self, audio=None, vad_filter=False, **kw):
            seen["type"] = type(audio)
            if not isinstance(audio, np.ndarray):
                raise ValueError("audio must be a 1D float array")
            return "hi", 0.9, [("hi", 0.9), ("en", 0.1)]

    s = WhisperSTT("small", languages="en,hi")
    s._model = _Picky()
    assert s._detect(wav) == "hi"
    assert seen["type"] is np.ndarray


def test_a_broken_detector_says_so_once(caplog, wav):
    """Silence is how the path bug survived: it failed on every utterance and never said a word."""
    s = _stt("en,hi", PROBS, fail=True)
    with caplog.at_level("WARNING"):
        s._detect(wav)
        s._detect(wav)
    warnings = [r for r in caplog.records if "Language detection failed" in r.message]
    assert len(warnings) == 1


# ------------------------------------------------------------------ answering back
def test_zeta_is_told_which_languages_it_may_answer_in():
    from app.agent.prompts import language_rule

    rule = language_rule("en,hi")
    assert "English or Hindi" in rule
    assert "speech-recognition error" in rule       # what to do with a stray Portuguese transcript


def test_no_shortlist_keeps_the_mirror_the_user_rule():
    from app.agent.prompts import language_rule

    assert "the language the user wrote or spoke in" in language_rule("")


def test_the_prompt_carries_the_configured_languages():
    from app.agent.prompts import build_system_prompt

    prompt = build_system_prompt(name="Zeta", tool_names=[], languages="en,hi")
    assert "reply only in English or Hindi" in prompt
