"""Chatterbox: the local neural voice, its client, and the emotion-to-delivery mapping."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_DIR, TTSProviderName
from app.emotion import speech
from app.emotion.engine import EmotionEngine
from app.providers.tts import ChatterboxTTS, build_tts_provider


def _server_module():
    """Load scripts/chatterbox_server.py without needing the TTS virtualenv (torch is imported lazily)."""
    path = PROJECT_DIR / "scripts" / "chatterbox_server.py"
    spec = importlib.util.spec_from_file_location("zeta_chatterbox_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ server helpers
def test_long_replies_are_split_on_sentences():
    m = _server_module()
    text = ("First sentence here. " * 12).strip()
    parts = m.chunks(text, limit=100)
    assert len(parts) > 1
    assert all(len(p) <= 100 for p in parts)
    assert " ".join(parts).replace("  ", " ") == text


def test_a_single_very_long_sentence_still_gets_cut():
    m = _server_module()
    parts = m.chunks("word " * 200, limit=120)
    assert parts and all(len(p) <= 120 for p in parts)


def test_server_keeps_only_the_tags_chatterbox_can_perform():
    m = _server_module()
    assert m.clean_text("[laugh] yes [warmly] indeed") == "[laugh] yes indeed"
    assert m.clean_text("plain text") == "plain text"


def test_wav_writer_produces_a_real_wav():
    import wave

    m = _server_module()
    np = pytest.importorskip("numpy")
    data = m.to_wav(np.zeros(2400, dtype=np.float32), 24000)
    assert data[:4] == b"RIFF"
    with wave.open(__import__("io").BytesIO(data)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getnframes() == 2400


# ------------------------------------------------------------------ delivery mapping
def test_every_style_carries_chatterbox_settings():
    for st in speech.STYLES.values():
        cb = st.chatterbox
        assert 0.0 <= cb["exaggeration"] <= 1.0
        assert 0.0 <= cb["cfg_weight"] <= 1.0


def test_upset_is_spoken_slower_and_calmer_than_delighted():
    calm = speech.STYLES["steady"].chatterbox
    happy = speech.STYLES["delighted"].chatterbox
    assert calm["cfg_weight"] < happy["cfg_weight"]        # lower cfg_weight = slower pacing
    assert calm["exaggeration"] < happy["exaggeration"]


def test_tag_dialect_is_translated_for_chatterbox():
    style = speech.STYLES["warm"]
    assert speech.prepare("[laughs] nice", style, "chatterbox") == "[laugh] nice"
    assert speech.prepare("[warmly] hello", style, "chatterbox") == "hello"      # mood tags are dropped
    assert speech.prepare("[sighs] fine", style, "chatterbox") == "[sigh] fine"


def test_only_one_cue_per_reply():
    playful = speech.STYLES["playful"]                     # its lead tag is "laughs"
    assert speech.prepare("[sighs] oh well", playful, "chatterbox") == "[sigh] oh well"
    assert speech.prepare("oh well", playful, "chatterbox") == "[laugh] oh well"


def test_chatterbox_is_expressive_and_advertises_its_tags():
    assert ChatterboxTTS().expressive is True
    note = speech.prompt_note("chatterbox")
    assert "[laughs]" in note and "[gently]" not in note


# ------------------------------------------------------------------ the client
@pytest.mark.asyncio
async def test_the_emotion_state_reaches_the_generator(monkeypatch):
    tts = ChatterboxTTS(voice="voice/zeta_female.wav")
    sent = {}

    async def fake_post(body):
        sent.update(body)
        return b"RIFF....WAVE"

    monkeypatch.setattr(tts, "_post", fake_post)
    engine = EmotionEngine(enabled=True, use_prosody=False)
    style = speech.style_for(engine.analyze("i feel so alone today"), "I'm here.")
    data, mime = await tts.synthesize("I'm here.", style)

    assert mime == "audio/wav" and data.startswith(b"RIFF")
    assert sent["voice"] == "voice/zeta_female.wav"
    assert sent["exaggeration"] == style.chatterbox["exaggeration"]
    assert sent["cfg_weight"] == style.chatterbox["cfg_weight"]
    assert sent["cfg_weight"] <= 0.35                       # someone lonely gets a slower voice


@pytest.mark.asyncio
async def test_defaults_are_used_when_no_emotion_is_known(monkeypatch):
    tts = ChatterboxTTS(exaggeration=0.42, cfg_weight=0.44)
    sent = {}

    async def fake_post(body):
        sent.update(body)
        return b"RIFF"

    monkeypatch.setattr(tts, "_post", fake_post)
    await tts.synthesize("hello")
    assert sent == {"text": "hello", "voice": "", "exaggeration": 0.42, "cfg_weight": 0.44, "temperature": 0.8}


@pytest.mark.asyncio
async def test_a_missing_server_explains_itself_instead_of_hanging():
    tts = ChatterboxTTS(base_url="http://127.0.0.1:9", autostart=False)
    h = await tts.health()
    assert h["ok"] is False and "start_tts" in h["detail"]

    from app.core.exceptions import ZetaError

    with pytest.raises(ZetaError) as e:
        await tts.synthesize("hello")
    assert "not running" in e.value.user_message


def test_provider_factory_builds_chatterbox_from_settings(svc, monkeypatch):
    s = svc.settings
    monkeypatch.setattr(s, "tts_provider", TTSProviderName.CHATTERBOX)
    monkeypatch.setattr(s, "chatterbox_voice", "voice/zeta_female.wav")
    p = build_tts_provider(s)
    assert isinstance(p, ChatterboxTTS)
    assert p.voice == "voice/zeta_female.wav" and p.model == s.chatterbox_model
