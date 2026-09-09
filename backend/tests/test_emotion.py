"""Emotional intelligence: reading words and tone, crisis handling, delivery, and the API."""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.emotion import crisis, lexicon, prosody, speech
from app.emotion.engine import EmotionEngine
from app.emotion.state import EmotionState, nearest_label


@pytest_asyncio.fixture()
async def client(svc):
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------ words
def test_lexicon_reads_plain_feeling_words():
    v, a, c, cues, label = lexicon.analyze("i feel really sad today")
    assert v < -0.4 and c > 0.3 and label == "sad"


def test_lexicon_handles_negation():
    v, *_ = lexicon.analyze("i am not happy about this")
    assert v < 0


def test_intensifiers_and_downtoners_move_the_needle():
    mild, *_ = lexicon.analyze("i am a bit annoyed")
    strong, *_ = lexicon.analyze("i am extremely annoyed")
    assert strong < mild < 0


def test_phrases_beat_their_individual_words():
    # "tired of" is frustration, not sleepiness.
    v, a, c, cues, label = lexicon.analyze("i am so tired of this")
    assert label == "frustrated" and a > 0.5
    # good news with no emotional word in it at all
    v2, _, c2, _, label2 = lexicon.analyze("i got the job")
    assert v2 > 0.5 and label2 in ("excited", "happy") and c2 > 0.3


def test_audio_event_tags_are_stripped_and_scored():
    clean, events = lexicon.strip_audio_events("[laughs] that is brilliant")
    assert clean == "that is brilliant" and events == ["laughs"]
    v, *_ = lexicon.analyze("[laughs] that is brilliant")
    assert v > 0.4


def test_no_emotional_content_is_reported_as_no_reading():
    v, a, c, cues, label = lexicon.analyze("open the downloads folder")
    assert c == 0.0 and v == 0.0


# ------------------------------------------------------------------ safety
@pytest.mark.parametrize("text,level", [
    ("i don't want to live anymore", "crisis"),
    ("i have been cutting myself", "crisis"),
    ("i'm going to kill myself tonight", "emergency"),
    ("i can't take this anymore", "concern"),
])
def test_crisis_tiers(text, level):
    sig = crisis.detect(text, "in")
    assert sig is not None and sig.level == level


@pytest.mark.parametrize("text", [
    "this deadline is killing me",
    "i'm dying of laughter",
    "kill the process on port 8000",
    "dead tired after that meeting",
])
def test_idioms_are_not_a_crisis(text):
    assert crisis.detect(text, "in") is None


def test_crisis_guidance_is_supportive_not_clinical():
    sig = crisis.detect("i want to die", "in")
    g = sig.guidance().lower()
    assert "14416" in g or "tele-manas" in g
    assert "not a therapist" in g or "therapist" in g
    assert "valence" not in g


def test_region_selects_the_right_helplines():
    assert any("988" in r["contact"] for r in crisis.resources("us"))
    assert any("116 123" in r["contact"] for r in crisis.resources("uk"))
    assert any("14416" in r["contact"] for r in crisis.resources("in"))


# ------------------------------------------------------------------ engine
def test_engine_disabled_reads_nothing():
    e = EmotionEngine(enabled=False)
    st = e.analyze("i am devastated")
    assert st.confidence == 0.0 and e.prompt_note(st) == ""


def test_engine_produces_a_usable_state():
    e = EmotionEngine(enabled=True, use_prosody=False)
    st = e.analyze("i am so anxious about tomorrow")
    assert st.label == "anxious" and st.needs_support and st.confidence > 0.3
    assert "anxious" in st.describe()


def test_prompt_note_never_leaks_the_machinery():
    e = EmotionEngine(enabled=True, use_prosody=False)
    st = e.analyze("everything is falling apart and i can't cope")
    note = e.prompt_note(st).lower()
    assert note and "valence" not in note and "arousal" not in note and "confidence" not in note


def test_staged_voice_reading_is_reused_once():
    e = EmotionEngine(enabled=True, use_prosody=False)
    st = e.analyze("i got the job")
    e.stage("i got the job", st)
    assert e.take_staged("i got the job") is st
    assert e.take_staged("i got the job") is None       # consumed
    e.stage("i got the job", st)
    assert e.take_staged("something else") is None      # only for the matching turn


def test_trend_follows_the_conversation():
    e = EmotionEngine(enabled=True, use_prosody=False)
    for text in ["i feel awful", "everything is terrible", "this is hopeless", "actually that helped", "i feel much better now", "i'm really happy"]:
        e.record(e.analyze(text))
    tr = e.trend()
    assert tr["samples"] >= 3 and tr["direction"] == "lifting"


def test_crisis_overrides_the_reading():
    e = EmotionEngine(enabled=True, use_prosody=False)
    st = e.analyze("i don't want to be here anymore")
    assert st.crisis and st.crisis["level"] == "crisis"
    assert st.label == "distressed" and st.valence <= -0.8


def test_nearest_label_covers_the_circumplex():
    assert nearest_label(0.8, 0.8) in ("excited", "happy")
    assert nearest_label(-0.8, 0.2) in ("sad", "lonely", "tired", "distressed")


# ------------------------------------------------------------------ tone of voice
def _wav(seconds: float = 2.0, freq: float = 140.0, syllables_per_s: float = 3.0, rate: int = 16000) -> bytes:
    """Synthetic speech-like audio: a buzzy tone chopped into syllables with gaps."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            t = i / rate
            # syllable envelope: raised cosine, silent between syllables
            env = max(0.0, math.sin(math.pi * ((t * syllables_per_s) % 1.0)) - 0.25) / 0.75
            s = env * sum(0.5 / (k + 1) * math.sin(2 * math.pi * freq * (k + 1) * t) for k in range(4))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def test_prosody_extracts_features_from_audio():
    pytest.importorskip("av")
    f = prosody.extract(_wav(), "audio/wav")
    assert f.ok
    assert 1.9 < f.duration_s < 2.2
    assert f.voiced_ratio > 0.5
    assert 100 < f.pitch_mean < 200          # tracks the 140 Hz tone
    assert f.speech_rate > 1.0               # hears the syllable onsets


def test_prosody_scores_against_your_own_baseline():
    pytest.importorskip("av")
    calm = prosody.extract(_wav(freq=130, syllables_per_s=2.5), "audio/wav")
    base = prosody.Baseline()
    assert not base.ready
    for _ in range(4):
        base.update(calm)
    assert base.ready

    agitated = prosody.extract(_wav(freq=210, syllables_per_s=5.0), "audio/wav")
    v, a, c, cues = prosody.score(agitated, base)
    assert c > 0.0
    assert any("faster" in x for x in cues) or any("higher pitch" in x for x in cues)
    # the same voice as the baseline should not look activated
    _, a_same, _, _ = prosody.score(calm, base)
    assert a > a_same


def test_prosody_survives_rubbish_input():
    f = prosody.extract(b"not audio at all", "audio/webm")
    assert f.ok is False
    e = EmotionEngine(enabled=True, use_prosody=True)
    st = e.analyze("i am fine", b"not audio at all", "audio/webm")   # must not raise
    assert isinstance(st, EmotionState)


# ------------------------------------------------------------------ delivery
def test_style_matches_the_persons_state():
    e = EmotionEngine(enabled=True, use_prosody=False)
    assert speech.style_for(e.analyze("i feel so alone"), "").name in ("gentle", "steady")
    assert speech.style_for(e.analyze("i got the job!!"), "").name in ("bright", "delighted")
    assert speech.style_for(None, "").name == "neutral"
    assert speech.style_for(e.analyze("i feel so alone"), "", enabled=False).name == "neutral"


def test_crisis_is_spoken_steadily():
    e = EmotionEngine(enabled=True, use_prosody=False)
    assert speech.style_for(e.analyze("i want to end my life"), "").name == "steady"


def test_tags_only_go_to_a_model_that_performs_them():
    style = speech.STYLES["warm"]
    assert speech.prepare("[laughs] nice one", style, "eleven_v3").startswith("[warmly]")
    assert "[laughs]" not in speech.prepare("[laughs] nice one", style, "eleven_multilingual_v2")
    assert speech.prepare("[laughs] nice one", style, "eleven_flash_v2_5") == "nice one"


def test_written_text_never_shows_delivery_tags():
    assert speech.for_display("[giggles] that's funny [sighs]") == "that's funny"


def test_prompt_note_offered_only_for_v3():
    assert speech.prompt_note("eleven_v3")
    assert speech.prompt_note("eleven_multilingual_v2") == ""


def test_voice_settings_are_valid_for_elevenlabs():
    for st in speech.STYLES.values():
        s = st.settings
        assert 0.0 <= s["stability"] <= 1.0 and 0.0 <= s["style"] <= 1.0 and 0.0 <= s["similarity_boost"] <= 1.0


# ------------------------------------------------------------------ wiring
@pytest.mark.asyncio
async def test_services_expose_the_engine(svc):
    assert svc.emotion.enabled is True
    assert svc.orchestrator.emotion is svc.emotion
    st = await svc.status()
    assert "emotion" in st and st["emotion"]["enabled"] is True


@pytest.mark.asyncio
async def test_orchestrator_reads_the_user_and_logs_it(svc):
    from app.core.events import event_bus

    q = event_bus.subscribe()
    try:
        task = await svc.submit("i am completely overwhelmed and i can't cope with any of it", wait=True)
        assert task.status.value in ("COMPLETED", "FAILED")
        assert svc.emotion.current.needs_support
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        emo = [e for e in seen if e.get("type") == "emotion"]
        assert emo and emo[0]["emotion"]["label"]
        # the reading is a private hint, not activity-feed noise
        assert not any(e.get("type") == "emotion" for e in event_bus.recent(200))
    finally:
        event_bus.unsubscribe(q)
    rows = await svc.emotion.history_rows(limit=10)
    assert rows and rows[0]["label"]


@pytest.mark.asyncio
async def test_emotion_api(client, svc):
    r = await client.get("/api/emotion")
    assert r.status_code == 200 and r.json()["enabled"] is True

    r = await client.post("/api/emotion/analyze", json={"text": "i am so happy right now"})
    assert r.status_code == 200
    body = r.json()
    assert body["valence"] > 0.3 and body["label"] in ("happy", "excited", "content")

    svc.emotion.record(svc.emotion.analyze("i feel awful"))
    r = await client.get("/api/emotion/history?limit=5")
    assert r.status_code == 200 and isinstance(r.json(), list)

    r = await client.get("/api/emotion/daily?days=3")
    assert r.status_code == 200 and "days" in r.json()


@pytest.mark.asyncio
async def test_emotion_can_be_switched_off(svc, monkeypatch):
    svc.emotion.enabled = False
    try:
        st = svc.emotion.analyze("i am devastated")
        assert st.confidence == 0.0
        assert svc.emotion.prompt_note(st) == ""
    finally:
        svc.emotion.enabled = True


def test_support_mode_off_keeps_zeta_task_first():
    from app.agent.prompts import EMOTIONAL_INTELLIGENCE, build_system_prompt

    on = build_system_prompt(name="Zeta", tool_names=["a"], emotional=True, support_mode=True)
    off = build_system_prompt(name="Zeta", tool_names=["a"], emotional=True, support_mode=False)
    assert "Support beats efficiency" in on and "Support beats efficiency" not in off
    assert "never diagnose" in off          # the rest of the guidance survives

    e = EmotionEngine(enabled=True, use_prosody=False, support_mode=False)
    note = e.prompt_note(e.analyze("i feel quite sad about it"))
    assert "get on with what they asked for" in note
    assert EMOTIONAL_INTELLIGENCE


# --------------------------------------------------------------- mood must not block action
# Real fault: a prosody-only "tired" reading (confidence 0.5, intensity 0.29) put Zeta into
# support mode, and "Open MySQL Workbench" got "I'm sorry you're having trouble. Let me try to
# open it for you." - with no tool call. Emotion changes the wording, never whether Zeta acts.
import pytest as _pytest

from app.emotion.engine import asks_for_action


@_pytest.mark.parametrize("text", [
    "Open MySQL Workbench and open any database on it.",
    "Can you open gtf5 for me, the application in the games folder",
    "please launch whatsapp",
    "hey zeta, open chrome and search for the weather",
    "i need you to find my tax file",
    "take a screenshot",
    "close spotify",
])
def test_requests_to_do_something_are_recognised(text):
    assert asks_for_action(text)


@_pytest.mark.parametrize("text", [
    "work has been really rough today",
    "i am so tired of all of this",
    "i feel like nothing i do ever works",
    "it takes a lot out of me",
    "how are you today?",
])
def test_someone_sharing_how_they_feel_is_not_a_request(text):
    assert not asks_for_action(text)


def _tired():
    from app.emotion.state import EmotionState

    return EmotionState(label="tired", valence=-0.22, arousal=0.145, confidence=0.5, intensity=0.287,
                        cues=["quieter than usual", "hesitant, long pauses"])


def test_a_low_mood_does_not_stop_zeta_opening_an_app():
    from app.emotion.engine import EmotionEngine

    eng = EmotionEngine(enabled=True, use_prosody=True, region="in", support_mode=True)
    note = eng.prompt_note(_tired(), "Open MySQL Workbench and open any database on it.")
    assert "asked you to do something - do it" in note
    assert "Lead with acknowledgement before anything practical" not in note


def test_real_distress_still_gets_the_support_lead():
    from app.emotion.engine import EmotionEngine
    from app.emotion.state import EmotionState

    eng = EmotionEngine(enabled=True, use_prosody=True, region="in", support_mode=True)
    sad = EmotionState(label="sad", valence=-0.7, arousal=0.2, confidence=0.75, intensity=0.62,
                       cues=["flat, quiet delivery"])
    assert "Lead with acknowledgement" in eng.prompt_note(sad, "i feel like nothing i do ever works")


def test_a_faint_reading_no_longer_hijacks_the_reply():
    """confidence 0.5 / intensity 0.29 is a hint, not a reason to lead with sympathy."""
    from app.emotion.engine import EmotionEngine

    eng = EmotionEngine(enabled=True, use_prosody=True, region="in", support_mode=True)
    note = eng.prompt_note(_tired(), "work has been rough")
    assert "Lead with acknowledgement before anything practical" not in note
    assert "get on with what they asked for" in note
