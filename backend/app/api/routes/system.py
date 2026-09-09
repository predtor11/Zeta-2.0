"""System status, hardware monitoring, activity log, tools, permissions and first-run setup."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_auth, services
from app.core import database, metrics
from app.core.config import BACKEND_DIR, PROJECT_DIR
from app.security import audit as audit_log
from app.core.events import event_bus
from app.models.schemas import SetupRequest
from app.services import ZetaServices

router = APIRouter(prefix="/api", tags=["system"], dependencies=[Depends(require_auth)])


@router.get("/system/status")
async def system_status(svc: ZetaServices = Depends(services)):
    return await svc.status()


@router.get("/system/health")
async def health(svc: ZetaServices = Depends(services)):
    return {"ok": True, "plugins": await svc.plugins.health()}


@router.get("/activity")
async def activity(limit: int = 200, svc: ZetaServices = Depends(services)):
    return event_bus.recent(limit)


@router.delete("/activity")
async def clear_activity():
    event_bus.clear()
    return {"cleared": True}


@router.get("/audit")
async def audit_entries(limit: int = 100, event: str = "", task_id: str = "", tool: str = ""):
    return await audit_log.query(limit, event=event or None, task_id=task_id or None, tool=tool or None)


@router.get("/system/metrics")
async def system_metrics(svc: ZetaServices = Depends(services)):
    """CPU, memory, GPU, disks, network and battery, plus what Zeta itself is holding.

    Answers from the background sampler, so this never waits on nvidia-smi. Asking starts the
    sampling loop and keeps it alive; it stops on its own once the panel is closed.
    """
    snap = dict(await metrics.sampler.get())
    snap["zeta"] = await _zeta_footprint(svc, snap.get("gpu") or {})
    return snap


_placement: Dict[str, Any] = {"at": 0.0, "value": {}}


async def _placement_cached(placement, ttl: float = 3.0) -> Dict[str, Any]:
    """Where the language model's weights are. Ollama takes ~200 ms to answer; it changes slowly."""
    now = time.monotonic()
    if now - _placement["at"] < ttl:
        return _placement["value"]
    value = await placement()
    _placement.update(at=now, value=value)
    return value


async def _zeta_footprint(svc: ZetaServices, gpu_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Which of Zeta's own models are on the GPU right now, and how much they hold.

    This is the view that actually answers "why is Zeta slow": the language model spilling onto
    the CPU, or the voice holding VRAM the model needed. Nothing here measures the hardware -
    the card's size comes from the snapshot that was already taken.
    """
    out: Dict[str, Any] = {"llm": {"model": svc.settings.llm_model}, "voice": {}, "sharing": False}
    try:
        out["sharing"] = svc.gpu_shared_for(gpu_snapshot.get("memory_total_mb"))
    except Exception:  # noqa: BLE001
        pass
    placement = getattr(svc.llm, "placement", None)
    if placement:
        try:
            where = await _placement_cached(placement)
            out["llm"] = {"model": svc.settings.llm_model, **where}
        except Exception:  # noqa: BLE001
            pass
    try:
        health = await svc.tts.health()
        out["voice"] = {"provider": svc.tts.name, "ok": health.get("ok", False),
                        "parked": health.get("parked"), "device": health.get("holding_device") or health.get("device"),
                        "detail": health.get("detail", "")}
    except Exception:  # noqa: BLE001
        pass
    out["speech"] = {"model": svc.settings.stt_model, "device": getattr(svc.stt, "active_device", "")}
    return out


@router.get("/emotion")
async def emotion_status(svc: ZetaServices = Depends(services)):
    return {**svc.emotion.status(), "expressive_voice": bool(getattr(svc.tts, "expressive", False)),
            "model": svc.settings.elevenlabs_model, "adapt_voice": svc.settings.emotion_adapt_voice}


@router.get("/emotion/history")
async def emotion_history(limit: int = 100, days: int = 14, svc: ZetaServices = Depends(services)):
    return await svc.emotion.history_rows(limit=limit, days=days)


@router.get("/emotion/daily")
async def emotion_daily(days: int = 14, svc: ZetaServices = Depends(services)):
    return {"days": await svc.emotion.daily_summary(days), "trend": svc.emotion.trend()}


@router.post("/emotion/analyze")
async def emotion_analyze(body: Dict[str, str], svc: ZetaServices = Depends(services)):
    """Analyse a piece of text without sending it to the agent (used by the UI and for testing)."""
    return svc.emotion.analyze_text(body.get("text", "")).to_dict()


@router.get("/system/database")
async def database_info(svc: ZetaServices = Depends(services)):
    info = database.describe_database_url(svc.settings.database_url)
    info.update(await database.ping())
    info["file_index"] = str(svc.settings.data_dir / "file_index.db")
    return info


@router.get("/tools")
async def tools(svc: ZetaServices = Depends(services)):
    return [t.info(svc.permissions) for t in svc.registry.all()]


@router.get("/permissions")
async def get_permissions(svc: ZetaServices = Depends(services)):
    return svc.permissions.to_dict()


class PermissionsUpdate(BaseModel):
    permissions: Dict[str, Dict[str, str]] = {}
    enabled: Dict[str, bool] = {}
    disabled_tools: list[str] | None = None


@router.put("/permissions")
async def update_permissions(body: PermissionsUpdate, svc: ZetaServices = Depends(services)):
    svc.permissions.apply({"permissions": body.permissions, "enabled": body.enabled})
    if body.disabled_tools is not None:
        svc.permissions.disabled_tools = set(body.disabled_tools)
    svc.permissions.save(BACKEND_DIR / "config" / "permissions.local.yaml")
    event_bus.activity("Permissions updated")
    return svc.permissions.to_dict()


# ----------------------------------------------------------------- setup
async def _probe(url: str, path: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(url.rstrip("/") + path)
        if r.status_code == 200:
            data = r.json()
            models = [m.get("name") for m in data.get("models", [])] if "models" in data else [m.get("id") for m in data.get("data", [])]
            return {"available": True, "models": models}
    except Exception:  # noqa: BLE001
        pass
    return {"available": False, "models": []}


@router.get("/setup/status")
async def setup_status(svc: ZetaServices = Depends(services)):
    env_exists = (PROJECT_DIR / ".env").exists()
    ollama = await _probe(svc.settings.ollama_base_url, "/api/tags")
    lmstudio = await _probe(svc.settings.lmstudio_base_url, "/models")
    return {"setup_complete": env_exists, "env_path": str(PROJECT_DIR / ".env"), "ollama": ollama, "lmstudio": lmstudio,
            "current": svc.settings.public_summary(), "permissions": svc.permissions.to_dict()}


_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _write_env(updates: Dict[str, str]) -> Path:
    path = PROJECT_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


@router.post("/setup")
async def setup(req: SetupRequest, svc: ZetaServices = Depends(services)):
    provider = req.llm_provider.lower()
    if provider not in ("ollama", "lmstudio", "openai", "openai_compatible", "openrouter", "anthropic", "mock"):
        raise HTTPException(400, "Unknown LLM provider")
    updates: Dict[str, str] = {"ZETA_MODE": "local", "LLM_PROVIDER": provider}
    if req.llm_model:
        updates["LLM_MODEL"] = req.llm_model
    if req.llm_base_url:
        updates["LLM_BASE_URL"] = req.llm_base_url
    if req.llm_api_key:
        if provider == "openai":
            updates["OPENAI_API_KEY"] = req.llm_api_key
        elif provider == "anthropic":
            updates["ANTHROPIC_API_KEY"] = req.llm_api_key
        elif provider == "openrouter":
            updates["OPENROUTER_API_KEY"] = req.llm_api_key
        else:
            updates["LLM_API_KEY"] = req.llm_api_key
    updates["TTS_PROVIDER"] = req.tts_provider or "disabled"
    updates["STT_PROVIDER"] = req.stt_provider or "disabled"
    if req.elevenlabs_api_key:
        updates["ELEVENLABS_API_KEY"] = req.elevenlabs_api_key
    if req.tts_voice is not None:
        updates["TTS_VOICE"] = req.tts_voice
    if req.wake_word_enabled is not None:
        updates["WAKE_WORD_ENABLED"] = "true" if req.wake_word_enabled else "false"
    if req.wake_word:
        updates["WAKE_WORD"] = req.wake_word
    if req.chatterbox_model:
        updates["CHATTERBOX_MODEL"] = req.chatterbox_model
    if req.chatterbox_voice is not None:
        updates["CHATTERBOX_VOICE"] = req.chatterbox_voice.strip()
    if req.chatterbox_device:
        updates["CHATTERBOX_DEVICE"] = req.chatterbox_device
    if req.emotion_enabled is not None:
        updates["EMOTION_ENABLED"] = "true" if req.emotion_enabled else "false"
    if req.emotion_adapt_voice is not None:
        updates["EMOTION_ADAPT_VOICE"] = "true" if req.emotion_adapt_voice else "false"
    if req.emotion_region:
        updates["EMOTION_REGION"] = req.emotion_region
    if req.elevenlabs_model:
        updates["ELEVENLABS_MODEL"] = req.elevenlabs_model
    if req.llm_stream is not None:
        updates["LLM_STREAM"] = "true" if req.llm_stream else "false"
    if req.database_url is not None:
        updates["DATABASE_URL"] = req.database_url.strip()
    if req.api_token is not None:
        updates["API_TOKEN"] = req.api_token.strip()
    if req.allowed_roots:
        roots = [str(Path(r).expanduser()) for r in req.allowed_roots if r.strip()]
        updates["FS_ALLOWED_ROOTS"] = ";".join(roots)
    for k, v in updates.items():
        if not _ENV_KEY_RE.match(k) or "\n" in v:
            raise HTTPException(400, f"invalid value for {k}")
        os.environ[k] = v
    _write_env(updates)
    # Permissions: {"filesystem": true, "browser": false, ...}
    for cat, enabled in req.permissions.items():
        svc.permissions.set_category_enabled(cat, bool(enabled))
    svc.permissions.save(BACKEND_DIR / "config" / "permissions.local.yaml")
    try:
        await svc.reload()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Configuration invalid: {e}")
    return {"ok": True, "config": svc.settings.public_summary(), "permissions": svc.permissions.to_dict()}
