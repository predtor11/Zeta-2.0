"""Two models, one 8 GB card: the wake word and the GPU handover.

Both of these were real faults on an RTX 4070 Laptop:

* Zeta woke itself up. The wake listener biased Whisper towards "Hey Zeta" with an
  `initial_prompt`, and a tiny Whisper model given silence writes its prompt back out.
* Ollama timed out. `num_ctx=16384` made qwen3:8b 7450 MB, of which 1456 MB ran on the
  CPU because Chatterbox was holding the rest of the card - and Ollama says nothing when
  that happens, it just gets about ten times slower.
"""

from __future__ import annotations

import pytest

from app.core import gpu
from app.voice.wakeword import WhisperPhraseEngine, looks_hallucinated, phrase_matches


# ----------------------------------------------------------------- false wake words
@pytest.mark.parametrize("text", [
    "you",                                   # what Whisper writes for silence
    "Thanks for watching!",
    "so",
    "Hey Zeta hey Zeta hey Zeta hey Zeta",   # the decoder looping on its own bias
    "",
])
def test_non_speech_is_recognised_as_such(text):
    assert looks_hallucinated(text)


@pytest.mark.parametrize("text", ["Hey Zeta", "Zeta, what time is it", "hey zita open chrome"])
def test_real_wake_utterances_are_not_dismissed(text):
    assert not looks_hallucinated(text)


@pytest.mark.parametrize("text", [
    "the theta wave in an EEG sits around eight hertz",
    "I was reading about beta testing and the data looked fine",
    "check the meta description on that page",
])
def test_ordinary_words_no_longer_wake_zeta(text):
    """"theta", "beta", "data" and "meta" are all within a hair of "zeta". They only count
    when the greeting is there too, and never in the middle of a sentence."""
    assert not phrase_matches(text, "hey zeta")


def test_the_bare_name_still_works_on_its_own():
    assert phrase_matches("Zeta?", "hey zeta")
    assert phrase_matches("zeta open the browser", "hey zeta")


def test_the_bare_name_is_ignored_in_a_long_sentence():
    assert not phrase_matches("I think the zeta function is what he was talking about earlier", "hey zeta")
    assert phrase_matches("hey zeta, what is the zeta function about", "hey zeta")   # greeting present


# ----------------------------------------------------------------- confidence gating
def _pretend_local_model(svc, monkeypatch, unload=None):
    """Make the stub LLM look like Ollama: something that holds VRAM and can be unloaded."""
    async def default_unload():
        return True

    monkeypatch.setattr(svc.llm, "unload", unload or default_unload, raising=False)


class _Segment:
    def __init__(self, text: str, no_speech_prob: float = 0.0, avg_logprob: float = -0.2):
        self.text, self.no_speech_prob, self.avg_logprob = text, no_speech_prob, avg_logprob


def _engine(sensitivity: float = 0.5) -> WhisperPhraseEngine:
    """A WhisperPhraseEngine without the Whisper model: only the rejection rules are under test."""
    e = object.__new__(WhisperPhraseEngine)
    sens = sensitivity
    e.no_speech_max = 0.20 + 0.55 * sens
    e.logprob_min = -0.45 - 1.05 * sens
    return e


def test_whisper_own_doubt_is_believed():
    e = _engine()
    assert e._reject_reason([_Segment("Hey Zeta", no_speech_prob=0.9)], "Hey Zeta").startswith("no_speech_prob")
    assert e._reject_reason([_Segment("Hey Zeta", avg_logprob=-2.5)], "Hey Zeta").startswith("avg_logprob")
    assert e._reject_reason([_Segment("Hey Zeta")], "Hey Zeta") == ""


def test_sensitivity_zero_is_stricter_than_sensitivity_one():
    strict, eager = _engine(0.0), _engine(1.0)
    borderline = [_Segment("Hey Zeta", no_speech_prob=0.5, avg_logprob=-1.2)]
    assert strict._reject_reason(borderline, "Hey Zeta")
    assert not eager._reject_reason(borderline, "Hey Zeta")


# ----------------------------------------------------------------- the GPU arbiter
def test_no_gpu_means_nothing_to_arbitrate(monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: None)
    assert gpu.free_mb() == -1
    assert not gpu.needs_room_for(2400)      # never evict anything on a CPU-only machine


def test_room_is_needed_only_when_it_is_actually_short(monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    assert gpu.needs_room_for(2400)
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (24576, 18000))
    assert not gpu.needs_room_for(2400)


@pytest.mark.asyncio
async def test_zeta_hands_the_card_over_on_a_small_gpu(svc, monkeypatch):
    """A 4070 Laptop: the voice parks before a turn, the model unloads before speaking."""
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    handovers = []

    async def park():
        handovers.append("voice parked")
        return True

    async def unload():
        handovers.append("llm unloaded")
        return True

    monkeypatch.setattr(svc.tts, "release_gpu", park, raising=False)
    _pretend_local_model(svc, monkeypatch, unload)

    await svc.balance_gpu("llm")
    await svc.balance_gpu("voice")
    assert handovers == ["voice parked", "llm unloaded"]


@pytest.mark.asyncio
async def test_a_big_gpu_keeps_both_models_resident(svc, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (24576, 18000))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    called = []
    monkeypatch.setattr(svc.tts, "release_gpu", lambda: called.append("x"), raising=False)

    await svc.balance_gpu("llm")
    assert not called


@pytest.mark.asyncio
async def test_gpu_share_off_is_respected(svc, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "off")
    _pretend_local_model(svc, monkeypatch)
    called = []
    monkeypatch.setattr(svc.tts, "release_gpu", lambda: called.append("x"), raising=False)

    await svc.balance_gpu("llm")
    assert not called


@pytest.mark.asyncio
async def test_a_model_that_fits_alongside_the_voice_turns_the_handover_off(svc, monkeypatch):
    """The best fix for a small card is a smaller model, and Zeta should notice by itself.

    qwen3:8b measures 5900 MB, which leaves no room for the voice on an 8 GB card, so the two
    take turns. A 4B model measures around 3300 MB and both stay resident - no handover, and
    no model reload on the turn after Zeta speaks.
    """
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    _pretend_local_model(svc, monkeypatch)

    svc._note_llm_size({"total_mb": 5900})
    assert svc._gpu_shared()

    svc._llm_vram_mb = 0
    svc._note_llm_size({"total_mb": 3300})
    assert not svc._gpu_shared()


@pytest.mark.asyncio
async def test_the_measured_size_only_ever_grows(svc):
    """A reading taken while the model is still loading must not make Zeta over-optimistic."""
    svc._llm_vram_mb = 0
    svc._note_llm_size({"total_mb": 5900})
    svc._note_llm_size({"total_mb": 120})       # caught mid-load
    assert svc._llm_vram_mb == 5900


@pytest.mark.asyncio
async def test_the_model_reloads_while_the_reply_is_being_spoken(svc, monkeypatch):
    """Speaking evicts the model; without this the next question pays 15-20 s for the reload."""
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    _pretend_local_model(svc, monkeypatch)
    svc._note_llm_size({"total_mb": 5900})
    warmed = []

    async def warm():
        warmed.append(True)
        return {"total_mb": 5900, "vram_mb": 5900}

    monkeypatch.setattr(svc.llm, "warm", warm, raising=False)
    monkeypatch.setattr(svc.tts, "synthesize", lambda *a, **k: _wav(), raising=False)

    await svc.synthesize("Tokyo is the capital of Japan.")
    assert svc._rewarm is not None
    await svc._rewarm
    assert warmed


async def _wav():
    return b"RIFF", "audio/wav"


@pytest.mark.asyncio
async def test_no_rewarm_when_both_models_already_fit(svc, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (24576, 18000))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    monkeypatch.setattr(svc.tts, "synthesize", lambda *a, **k: _wav(), raising=False)

    await svc.synthesize("Nothing to hand over here.")
    assert svc._rewarm is None


@pytest.mark.asyncio
async def test_zeta_waits_for_a_sentence_instead_of_loading_into_a_full_card(svc, monkeypatch):
    """The bug this prevents: parking failed because the voice was mid-sentence, Zeta loaded the
    model anyway, part of it landed on the CPU and the turn timed out after 182 s."""
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    monkeypatch.setattr("app.services.PARK_RETRY_SECONDS", 0.0)
    _pretend_local_model(svc, monkeypatch)
    calls = []

    async def release():
        calls.append(1)
        return {"free": False, "busy": True, "parked": False} if len(calls) < 3 \
            else {"free": True, "busy": False, "parked": True}

    monkeypatch.setattr(svc.tts, "release_gpu", release, raising=False)
    assert await svc.balance_gpu("llm") is True
    assert len(calls) == 3          # waited out two busy replies rather than barging in


@pytest.mark.asyncio
async def test_a_voice_that_never_frees_the_card_is_reported_not_ignored(svc, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    monkeypatch.setattr("app.services.PARK_RETRY_SECONDS", 0.0)
    _pretend_local_model(svc, monkeypatch)

    async def release():
        return {"free": False, "busy": True, "parked": False}

    monkeypatch.setattr(svc.tts, "release_gpu", release, raising=False)
    assert await svc.balance_gpu("llm") is False


@pytest.mark.asyncio
async def test_a_voice_that_owns_no_vram_never_blocks_a_turn(svc, monkeypatch):
    """ElevenLabs and the Windows voices have no release_gpu; a turn must not wait on them."""
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    _pretend_local_model(svc, monkeypatch)
    monkeypatch.delattr(type(svc.tts), "release_gpu", raising=False)
    monkeypatch.setattr(svc.tts, "release_gpu", None, raising=False)
    assert await svc.balance_gpu("llm") is True


@pytest.mark.asyncio
async def test_a_cloud_model_never_triggers_the_handover(svc, monkeypatch):
    """OpenRouter and friends hold no VRAM. Parking the voice before every turn would cost a
    4.6 s reload to make room for a model that was never on the card."""
    monkeypatch.setattr(gpu, "gpu_memory", lambda **_: (8188, 218))
    monkeypatch.setattr(svc.settings, "gpu_share", "auto")
    svc._note_llm_size({"total_mb": 5900})

    monkeypatch.delattr(type(svc.llm), "unload", raising=False)
    assert not hasattr(svc.llm, "unload")
    assert not svc._gpu_shared()

    called = []
    monkeypatch.setattr(svc.tts, "release_gpu", lambda: called.append("x"), raising=False)
    assert await svc.balance_gpu("llm") is True
    assert not called
