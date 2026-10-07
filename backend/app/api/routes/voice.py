"""Voice endpoints: transcribe, speak, and full voice round-trip."""

from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.api.deps import require_auth, services
from app.core.events import event_bus
from app.core.exceptions import ZetaError
from app.models.schemas import SpeakRequest
from app.services import ZetaServices

router = APIRouter(prefix="/api/voice", tags=["voice"], dependencies=[Depends(require_auth)])

# Zeta will read out an essay if you let it. A character is about a sixteenth of a second of
# speech - and about 0.13 s of work to make - so this is already several minutes of talking that
# nobody listens to the end of. Long answers are read to here and left on screen.
MAX_SPEECH_CHARS = 4000


def _audio_seconds(data: bytes, mime: str) -> float:
    """How long this clip plays for, read out of the WAV header (0.0 if it is not a WAV)."""
    if "wav" not in mime or len(data) < 44:
        return 0.0
    try:
        import wave

        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _deafen_while_speaking(svc: ZetaServices, data: bytes, mime: str, text: str) -> None:
    """Stop the wake word listener hearing Zeta's own voice through the speakers.

    The browser pauses it too, but only once playback starts; this covers the gap and the
    case where nothing is watching the microphone from the UI side. When the clip length is
    unknown, fall back to a speaking-rate estimate (about 14 characters a second).
    """
    seconds = _audio_seconds(data, mime) or len(text) / 14.0
    svc.wake.pause(min(120.0, seconds + 2.5))


async def _analyse_voice(svc: ZetaServices, audio: bytes, mime: str, text: str):
    """Read tone of voice + words, stage the result for the chat turn that follows."""
    if not (svc.settings.emotion_enabled and text.strip()):
        return None
    try:
        state = svc.emotion.analyze(text, audio, mime)
        svc.emotion.stage(text, state)
        if state.confidence >= 0.3 or state.crisis:
            event_bus.publish("emotion", emotion=state.to_dict(), trend=svc.emotion.trend(), stage="heard")
        return state.to_dict()
    except Exception:  # noqa: BLE001
        return None


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), language: str = Form(""), svc: ZetaServices = Depends(services)):
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")
    mime = file.content_type or "audio/webm"
    try:
        text = await svc.stt.transcribe(audio, mime, language or None)
    except ZetaError as e:
        raise HTTPException(503, e.user_message)
    return {"text": text, "emotion": await _analyse_voice(svc, audio, mime, text)}


@router.post("/speak")
async def speak(req: SpeakRequest, svc: ZetaServices = Depends(services)):
    try:
        data, mime = await svc.synthesize(req.text, lead=req.lead, final=req.final)
    except ZetaError as e:
        raise HTTPException(503, e.user_message)
    _deafen_while_speaking(svc, data, mime, req.text)
    return Response(content=data, media_type=mime)


@router.post("/speak/plan")
async def speak_plan(req: SpeakRequest):
    """Split a reply into pieces that can be spoken one after another.

    The voice generates a whole clip before it returns anything, so a long answer asked for in
    one go is a long silence. Splitting it lets the browser start playing while the rest is
    still being made.

    What it must *not* do is start too early. Generation runs at about 0.42x realtime here, so
    every second of speech costs about 2.4 seconds of work: a player that starts on piece one
    and hopes for the best runs dry at every join, which is what "it keeps pausing mid-sentence"
    was. The browser therefore builds a lead first (see `speak` in useVoice.ts) and schedules the
    pieces end to end. Splitting only makes that possible - it does not decide when to speak.

    Splitting happens here rather than in the browser because it has to agree with what the
    voice actually receives: Markdown removed first, so "**Done.**" does not become a sentence
    boundary in one place and not the other.
    """
    from app.emotion.speech import pause_after, segments, strip_markdown

    spoken = strip_markdown(req.text)
    pieces = segments(spoken[:MAX_SPEECH_CHARS])
    # The silence to leave at each join. The pieces are separate clips, and the voice server drops
    # the gap after its own final sentence, so without this they collide.
    gaps = [pause_after(p) for p in pieces[:-1]] + [0.0] if pieces else []
    return {"segments": pieces, "gaps": gaps, "truncated": len(spoken) > MAX_SPEECH_CHARS}


@router.get("/audio/{audio_id}")
async def audio(audio_id: str, svc: ZetaServices = Depends(services)):
    item = svc.audio_cache.get(audio_id)
    if item is None:
        raise HTTPException(404, "audio expired")
    data, mime = item
    return Response(content=data, media_type=mime)


@router.get("/voices")
async def voices(svc: ZetaServices = Depends(services)):
    return {"provider": svc.tts.name, "voices": await svc.tts.voices()}


@router.post("")
async def voice_roundtrip(file: UploadFile = File(...), conversation_id: str = Form(""), speak: bool = Form(True),
                          svc: ZetaServices = Depends(services)):
    """Audio in -> transcription -> agent -> reply text (+ synthesized audio URL)."""
    audio = await file.read()
    mime_in = file.content_type or "audio/webm"
    try:
        text = await svc.stt.transcribe(audio, mime_in)
    except ZetaError as e:
        raise HTTPException(503, e.user_message)
    if not text.strip():
        return {"text": "", "reply": None, "task_id": None, "conversation_id": conversation_id or None, "audio_url": None}
    emotion = await _analyse_voice(svc, audio, mime_in, text)
    task = await svc.submit(text, conversation_id or None, wait=True)
    audio_url = None
    if speak and task.result:
        try:
            data, mime = await svc.synthesize(task.result[:2000])
            _deafen_while_speaking(svc, data, mime, task.result)
            audio_url = f"/api/voice/audio/{svc.cache_audio(data, mime)}"
        except ZetaError:
            audio_url = None
    return {"text": text, "reply": task.result, "task_id": task.id, "conversation_id": task.conversation_id,
            "status": task.status.value, "audio_url": audio_url, "emotion": emotion}


@router.get("/wake")
async def wake_status(svc: ZetaServices = Depends(services)):
    from app.voice.wakeword import list_input_devices

    return {**svc.wake.status(), "devices": list_input_devices()}


@router.post("/wake/pause")
async def wake_pause(seconds: float = Query(8.0, ge=0, le=120), svc: ZetaServices = Depends(services)):
    """The UI calls this while it is recording so the listener ignores the user's own request."""
    svc.wake.pause(seconds)
    return {"paused_for": seconds}


@router.post("/wake/test")
async def wake_test(svc: ZetaServices = Depends(services)):
    """Simulate a detection (useful to check the UI wiring without a microphone)."""
    svc.on_wake_word("test", "manual test")
    return {"ok": True}
