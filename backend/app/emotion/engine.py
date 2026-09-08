"""The emotion engine: fuse the channels, remember the mood, shape the response.

    audio  -> prosody.extract  ->  (valence, arousal) + cues     how it was said
    text   -> lexicon.analyze  ->  (valence, arousal) + label    what was said
    events -> [laughs] [sighs]                                   involuntary signals
    words  -> crisis.detect    ->  safety tier

Fusion is confidence-weighted, with voice trusted mainly for arousal and text for
valence, because that is where each channel is actually reliable.  The result is
one `EmotionState` that the orchestrator injects into the prompt, the UI renders,
and the TTS layer uses to pick a delivery.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.core import database
from app.emotion import crisis as crisis_mod
from app.emotion import lexicon, prosody
from app.emotion.state import LABELS, EmotionState, clamp, nearest_label

log = logging.getLogger(__name__)


class EmotionEngine:
    """Stateful per-user emotion tracking. Cheap, local, and always explainable."""

    def __init__(self, *, enabled: bool = True, use_prosody: bool = True, region: str = "in",
                 support_mode: bool = True, history_size: int = 60, staging_seconds: float = 180.0):
        self.enabled = enabled
        self.use_prosody = use_prosody
        self.region = region
        self.support_mode = support_mode      # may support take priority over getting the task done?
        self.baseline = prosody.Baseline()
        self.history: Deque[EmotionState] = deque(maxlen=history_size)
        self.staging_seconds = staging_seconds
        self._staged: Optional[Tuple[str, EmotionState]] = None
        self.current: EmotionState = EmotionState()
        self._writes: set = set()

    # ------------------------------------------------------------------ analysis
    def analyze(self, text: str, audio: Optional[bytes] = None, mime: str = "audio/webm") -> EmotionState:
        """Full analysis of one user turn. Never raises."""
        if not self.enabled:
            return EmotionState()
        clean, events = lexicon.strip_audio_events(text or "")
        tv, ta, tc, tcues, tlabel = lexicon.analyze(text or "")
        sources: Dict[str, Any] = {"text": {"valence": round(tv, 3), "arousal": round(ta, 3),
                                            "confidence": round(tc, 3), "label": tlabel, "events": events}}
        cues = list(tcues)

        pv, pa, pc = 0.0, 0.35, 0.0
        if audio and self.use_prosody:
            try:
                feats = prosody.extract(audio, mime)
                if feats.ok:
                    pv, pa, pc, pcues = prosody.score(feats, self.baseline)
                    self.baseline.update(feats)
                    cues += pcues
                    sources["voice"] = {"valence": round(pv, 3), "arousal": round(pa, 3), "confidence": round(pc, 3),
                                        "features": feats.to_dict(), "baseline": self.baseline.to_dict()}
            except Exception as e:  # noqa: BLE001
                log.debug("prosody analysis failed: %s", e)

        # Confidence-weighted fusion; voice leads on arousal, text leads on valence.
        vw_t, vw_p = tc * 1.0, pc * 0.45
        aw_t, aw_p = tc * 0.6, pc * 1.0
        valence = (tv * vw_t + pv * vw_p) / (vw_t + vw_p) if (vw_t + vw_p) > 0 else 0.0
        arousal = (ta * aw_t + pa * aw_p) / (aw_t + aw_p) if (aw_t + aw_p) > 0 else 0.35
        confidence = clamp(max(tc, pc) + 0.18 * min(tc, pc), 0.0, 0.95)

        # Agreement between channels raises confidence; contradiction lowers it.
        if tc > 0.2 and pc > 0.2:
            if (tv > 0.15 and pv < -0.15) or (tv < -0.15 and pv > 0.15):
                confidence *= 0.7
                cues.append("words and tone disagree")
            elif abs(tv - pv) < 0.35:
                confidence = clamp(confidence + 0.1, 0.0, 0.95)

        state = EmotionState.from_dimensions(valence, arousal, confidence, cues[:8], sources,
                                             label=tlabel if tlabel and tc >= 0.35 else "")
        state.text = (clean or text or "")[:400]
        sig = crisis_mod.detect(clean or text or "", self.region)
        if sig:
            state.crisis = sig.to_dict()
            if sig.level in ("crisis", "emergency"):
                state.label = "distressed"
                state.valence = min(state.valence, -0.8)
                state.confidence = max(state.confidence, 0.8)
                state.intensity = max(state.intensity, 0.85)
        return state

    def analyze_text(self, text: str) -> EmotionState:
        return self.analyze(text, None)

    # ------------------------------------------------------------------ staging
    def stage(self, text: str, state: EmotionState) -> None:
        """Remember the analysis of a voice turn so the chat submit that follows can pick it up."""
        self._staged = (text.strip(), state)

    def take_staged(self, text: str) -> Optional[EmotionState]:
        if not self._staged:
            return None
        staged_text, state = self._staged
        fresh = (datetime.now(timezone.utc) - state.at) < timedelta(seconds=self.staging_seconds)
        if fresh and staged_text and staged_text == (text or "").strip():
            self._staged = None
            return state
        return None

    # ------------------------------------------------------------------ tracking
    def record(self, state: EmotionState, *, conversation_id: Optional[str] = None, task_id: Optional[str] = None) -> None:
        self.current = state
        self.history.append(state)
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # called from sync code: keep the reading in memory only
        fut = asyncio.ensure_future(self._persist(state, conversation_id, task_id))
        self._writes.add(fut)
        fut.add_done_callback(self._writes.discard)

    async def drain(self) -> None:
        """Wait for in-flight writes - called on shutdown so no reading is lost."""
        if self._writes:
            import asyncio as _asyncio

            await _asyncio.gather(*list(self._writes), return_exceptions=True)

    async def _persist(self, state: EmotionState, conversation_id: Optional[str], task_id: Optional[str]) -> None:
        if not database.is_initialised():
            return
        try:
            from app.models.db import EmotionRecord

            async with database.session_scope() as s:
                s.add(EmotionRecord(
                    conversation_id=conversation_id, task_id=task_id, label=state.label,
                    valence=state.valence, arousal=state.arousal, confidence=state.confidence,
                    intensity=state.intensity, cues=state.cues,
                    crisis_level=(state.crisis or {}).get("level"), snippet=state.text[:300],
                ))
        except Exception as e:  # noqa: BLE001
            log.debug("emotion persist failed: %s", e)

    def trend(self, n: int = 8) -> Dict[str, Any]:
        """Short-term mood direction from the recent turns of this session."""
        recent = [s for s in list(self.history)[-n:] if s.confidence >= 0.25]
        if not recent:
            return {"samples": 0}
        vals = [s.valence for s in recent]
        avg = sum(vals) / len(vals)
        direction = "steady"
        if len(vals) >= 3:
            first, last = sum(vals[: len(vals) // 2]) / max(1, len(vals) // 2), sum(vals[len(vals) // 2:]) / max(1, len(vals) - len(vals) // 2)
            if last - first > 0.25:
                direction = "lifting"
            elif first - last > 0.25:
                direction = "sinking"
        counts: Dict[str, int] = {}
        for s in recent:
            counts[s.label] = counts.get(s.label, 0) + 1
        return {"samples": len(recent), "average_valence": round(avg, 3), "direction": direction,
                "dominant": max(counts, key=counts.get), "labels": counts,
                "mood": nearest_label(avg, sum(s.arousal for s in recent) / len(recent))}

    async def history_rows(self, limit: int = 100, days: int = 14) -> List[Dict[str, Any]]:
        if not database.is_initialised():
            return [s.to_dict() for s in list(self.history)[-limit:]][::-1]
        from sqlalchemy import select

        from app.models.db import EmotionRecord

        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with database.session_scope() as s:
            rows = (await s.execute(select(EmotionRecord).where(EmotionRecord.ts >= since)
                                    .order_by(EmotionRecord.ts.desc()).limit(limit))).scalars().all()
        return [{"id": r.id, "ts": r.ts.isoformat() if r.ts else None, "label": r.label, "valence": r.valence,
                 "arousal": r.arousal, "confidence": r.confidence, "intensity": r.intensity, "cues": r.cues or [],
                 "crisis_level": r.crisis_level, "snippet": r.snippet, "conversation_id": r.conversation_id}
                for r in rows]

    async def daily_summary(self, days: int = 7) -> List[Dict[str, Any]]:
        """Average valence per day - what the Mood panel graphs."""
        rows = await self.history_rows(limit=1000, days=days)
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            if not r["ts"]:
                continue
            buckets.setdefault(r["ts"][:10], []).append(r)
        out = []
        for day in sorted(buckets):
            items = buckets[day]
            v = sum(i["valence"] for i in items) / len(items)
            a = sum(i["arousal"] for i in items) / len(items)
            counts: Dict[str, int] = {}
            for i in items:
                counts[i["label"]] = counts.get(i["label"], 0) + 1
            out.append({"day": day, "samples": len(items), "valence": round(v, 3), "arousal": round(a, 3),
                        "mood": nearest_label(v, a), "dominant": max(counts, key=counts.get)})
        return out

    # ------------------------------------------------------------------ prompt
    def prompt_note(self, state: Optional[EmotionState] = None) -> str:
        """The emotional context handed to the model for this turn."""
        state = state or self.current
        if not self.enabled or state.confidence < 0.22 and not state.crisis:
            return ""
        lines = [f"How they seem right now: {state.describe()}."]
        tr = self.trend()
        if tr.get("samples", 0) >= 3 and tr["direction"] != "steady":
            lines.append(f"Across this conversation their mood has been {tr['direction']}.")
        if state.crisis:
            sig = crisis_mod.CrisisSignal(state.crisis["level"], state.crisis.get("matched", []),
                                          state.crisis.get("resources", []))
            lines.append(sig.guidance())
        elif state.needs_support and self.support_mode:
            lines.append("Lead with acknowledgement before anything practical. Keep it short, warm and specific; "
                         "do not perform sympathy or pile on questions.")
        elif state.needs_support:
            lines.append("Acknowledge it in a sentence, then get on with what they asked for.")
        elif state.is_positive and state.intensity > 0.4:
            lines.append("Match their energy briefly - a short genuine reaction, then carry on.")
        return "\n".join(lines)

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "prosody": self.use_prosody, "region": self.region, "support_mode": self.support_mode,
                "current": self.current.to_dict(), "trend": self.trend(),
                "voice_baseline": self.baseline.to_dict()}
