"""Voice endpoints: transcribe, speak, and full voice round-trip."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.api.deps import require_auth, services
from app.core.events import event_bus
from app.core.exceptions import ZetaError
from app.models.schemas import SpeakRequest
from app.services import ZetaServices

router = APIRouter(prefix="/api/voice", tags=["voice"], dependencies=[Depends(require_auth)])


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
        data, mime = await svc.tts.synthesize(req.text, svc.speech_style(req.text))
    except ZetaError as e:
        raise HTTPException(503, e.user_message)
    return Response(content=data, media_type=mime)


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
            data, mime = await svc.tts.synthesize(task.result[:2000], svc.speech_style(task.result))
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
