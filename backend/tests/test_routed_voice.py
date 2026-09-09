"""Two voices, one graphics card: picking the right one per reply.

Measured on an RTX 4070 Laptop and the reason this exists at all: Chatterbox Turbo speaks
English at 0.42x realtime but produces nonsense for Devanagari, while the multilingual model
says Hindi correctly at 0.17x - about 2.4x the cost per second of speech, measured on the same
sentence with each model alone. Routing per reply keeps English fast without giving up Hindi.
"""

from __future__ import annotations

import pytest

from app.providers.tts.chatterbox import ChatterboxTTS
from app.providers.tts.routed import LanguageRoutedTTS

HINDI = "नमस्ते, मैंने क्रोम खोल दिया है।"
ENGLISH = "Done. I have opened Chrome."


def _routed() -> LanguageRoutedTTS:
    return LanguageRoutedTTS(ChatterboxTTS("http://127.0.0.1:8766", model="turbo"),
                             ChatterboxTTS("http://127.0.0.1:8767", model="multilingual"))


def test_english_stays_on_the_fast_voice():
    assert _routed().voice_for(ENGLISH).model == "turbo"


def test_hindi_goes_to_the_voice_that_can_say_it():
    assert _routed().voice_for(HINDI).model == "multilingual"


def test_romanised_hinglish_is_not_sent_to_the_slow_voice():
    """"kya haal hai" is indistinguishable from English by script, and turbo handles it
    passably - in about half the time the multilingual model would take."""
    assert _routed().voice_for("kya haal hai, sab theek?").model == "turbo"


def test_a_second_voice_needs_its_own_port():
    """Both servers default to 8766; without this the second one never starts."""
    r = _routed()
    assert r.english.port == 8766 and r.other.port == 8767


def test_the_vram_budget_assumes_the_bigger_of_the_two():
    r = _routed()
    assert r.english.vram_mb == 2400 and r.other.vram_mb == 3300
    assert r.vram_mb == 3300


def test_the_prompt_describes_the_everyday_voice():
    """Built once at startup, so it cannot depend on a reply that has not happened yet. Each
    voice still filters tags against its own model before speaking."""
    assert _routed().speech_model == "chatterbox"


@pytest.mark.asyncio
async def test_speaking_hindi_parks_the_english_voice(monkeypatch):
    """Both models do not fit on an 8 GB card, so whichever is not speaking gives the card back."""
    r = _routed()
    parked = []

    async def park_english():
        parked.append("english")
        return {"free": True, "busy": False, "parked": True}

    async def speak(text, style=None, lead=True):
        return b"RIFF", "audio/wav"

    monkeypatch.setattr(r.english, "release_gpu", park_english)
    monkeypatch.setattr(r.other, "synthesize", speak)
    await r.synthesize(HINDI)
    assert parked == ["english"]


@pytest.mark.asyncio
async def test_speaking_english_parks_the_multilingual_voice(monkeypatch):
    r = _routed()
    parked = []

    async def park_other():
        parked.append("multilingual")
        return {"free": True, "busy": False, "parked": True}

    async def speak(text, style=None, lead=True):
        return b"RIFF", "audio/wav"

    monkeypatch.setattr(r.other, "release_gpu", park_other)
    monkeypatch.setattr(r.english, "synthesize", speak)
    await r.synthesize(ENGLISH)
    assert parked == ["multilingual"]


@pytest.mark.asyncio
async def test_a_turn_takes_the_card_back_from_both_voices(monkeypatch):
    """`balance_gpu("llm")` must not free one voice and leave the other holding 3 GB."""
    r = _routed()
    released = []

    def freeing(name):
        async def go():
            released.append(name)
            return {"free": True, "busy": False, "parked": True}

        return go

    monkeypatch.setattr(r.english, "release_gpu", freeing("english"))
    monkeypatch.setattr(r.other, "release_gpu", freeing("multilingual"))
    out = await r.release_gpu()
    assert sorted(released) == ["english", "multilingual"]
    assert out["free"] is True


@pytest.mark.asyncio
async def test_the_card_is_not_declared_free_while_one_voice_is_still_speaking(monkeypatch):
    r = _routed()

    async def busy():
        return {"free": False, "busy": True, "parked": False}

    async def free():
        return {"free": True, "busy": False, "parked": True}

    monkeypatch.setattr(r.english, "release_gpu", busy)
    monkeypatch.setattr(r.other, "release_gpu", free)
    out = await r.release_gpu()
    assert out["free"] is False and out["busy"] is True


@pytest.mark.asyncio
async def test_a_second_voice_that_has_never_started_is_not_a_fault(monkeypatch):
    """It starts the first time something non-English is said. Until then "not running" is
    the normal state and must not show up as broken speech."""
    r = _routed()

    async def ok():
        return {"ok": True, "detail": "turbo on cuda, voice built-in"}

    async def down():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(r.english, "health", ok)
    monkeypatch.setattr(r.other, "health", down)
    h = await r.health()
    assert h["ok"] is True
    assert "not started yet" in h["detail"]


@pytest.mark.asyncio
async def test_zeta_waits_rather_than_loading_into_a_full_card(monkeypatch):
    """The failure this prevents, measured rather than imagined: with both models resident the
    8 GB card had 84 MB free, Windows moved the overflow into shared system RAM, and turbo fell
    from 9 iterations a second to one every 106 seconds."""
    monkeypatch.setattr("app.providers.tts.routed.PARK_RETRY_SECONDS", 0.0)
    r = _routed()
    calls = []

    async def release():
        calls.append(1)
        return ({"free": False, "busy": True, "parked": False} if len(calls) < 3
                else {"free": True, "busy": False, "parked": True})

    async def speak(text, style=None, lead=True):
        return b"RIFF", "audio/wav"

    monkeypatch.setattr(r.english, "release_gpu", release)
    monkeypatch.setattr(r.other, "synthesize", speak)
    await r.synthesize(HINDI)
    assert len(calls) == 3            # waited out two busy sentences instead of barging in


@pytest.mark.asyncio
async def test_a_voice_that_never_frees_the_card_does_not_block_speech_forever(monkeypatch):
    """Waiting is right; waiting indefinitely is not. Speech still happens, with a warning."""
    monkeypatch.setattr("app.providers.tts.routed.PARK_RETRY_SECONDS", 0.0)
    r = _routed()
    spoke = []

    async def stuck():
        return {"free": False, "busy": True, "parked": False}

    async def speak(text, style=None, lead=True):
        spoke.append(text)
        return b"RIFF", "audio/wav"

    monkeypatch.setattr(r.english, "release_gpu", stuck)
    monkeypatch.setattr(r.other, "synthesize", speak)
    await r.synthesize(HINDI)
    assert spoke == [HINDI]


@pytest.mark.asyncio
async def test_a_voice_server_that_is_not_running_holds_nothing(monkeypatch):
    """The second voice starts lazily, so "connection refused" means the card is already free."""
    monkeypatch.setattr("app.providers.tts.routed.PARK_RETRY_SECONDS", 0.0)
    r = _routed()

    async def refused():
        raise OSError("connection refused")

    async def speak(text, style=None, lead=True):
        return b"RIFF", "audio/wav"

    monkeypatch.setattr(r.other, "release_gpu", refused)
    monkeypatch.setattr(r.english, "synthesize", speak)
    assert await r.synthesize(ENGLISH) == (b"RIFF", "audio/wav")
