"""Two Chatterbox voices, chosen by the language of the reply.

Neither model is the right answer on its own, and both numbers were measured on this machine
(RTX 4070 Laptop, 8 GB, nothing else on the card):

* **Turbo** speaks English at 1.7-2.2x realtime - fast enough that Zeta can generate the next
  sentence while the current one is playing, which is what makes streamed speech work. It
  cannot say a word of Hindi: given Devanagari it produced 16 s of audio that transcribes back
  as "Comeway. Comewood's lit-scar...".
* **Multilingual** says Hindi properly (Whisper detects `hi` at p=1.00) but runs at 0.37x, so
  an English reply that took 9.5 s of compute takes about 50 s. Streaming falls apart.

So Zeta keeps both and picks per reply. Only one holds the graphics card at a time: before
speaking, the other is parked into system RAM and comes back in 2-3 s when it is next needed.
That is the same handover already used between the voice and the language model, so switching
language costs one resume, not a model load.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from app.providers.tts.base import TTSProvider
from app.providers.tts.chatterbox import ChatterboxTTS

log = logging.getLogger(__name__)


class LanguageRoutedTTS(TTSProvider):
    name = "chatterbox"

    def __init__(self, english: ChatterboxTTS, other: ChatterboxTTS):
        self.english = english
        self.other = other

    # ------------------------------------------------------------------ routing
    def voice_for(self, text: str) -> ChatterboxTTS:
        """English unless the reply is written in another script.

        Detection is by script, so Devanagari goes to the multilingual voice and everything
        else stays on the fast one. Romanised Hinglish ("kya haal hai") reads as English here
        and is spoken by turbo, which handles it passably - better, at least, than waiting
        50 seconds for the multilingual model to say the same thing.
        """
        from app.emotion.speech import language_of

        return self.other if language_of(text) else self.english

    @property
    def expressive(self) -> bool:
        return True

    @property
    def speech_model(self) -> str:
        """The prompt is built once, so it describes the everyday voice.

        Harmless if a reply turns out to be Hindi: each voice re-checks the tags against what
        its own model performs, and drops the ones it would read out loud.
        """
        return self.english.speech_model

    @property
    def vram_mb(self) -> int:
        return max(self.english.vram_mb, self.other.vram_mb)

    # ------------------------------------------------------------------ speaking
    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        voice = self.voice_for(text)
        idle = self.other if voice is self.english else self.english
        try:
            await idle.release_gpu()      # only one of the two may hold the card
        except Exception as e:  # noqa: BLE001
            log.debug("could not park the %s voice: %s", idle.model, e)
        return await voice.synthesize(text, style, lead)

    async def release_gpu(self) -> Dict[str, Any]:
        """Hand the card back for a language-model turn: both voices, not just the busy one."""
        a = await self.english.release_gpu()
        b = await self.other.release_gpu()
        return {"free": bool(a["free"] and b["free"]),
                "busy": bool(a["busy"] or b["busy"]),
                "parked": bool(a["parked"] or b["parked"])}

    def stop(self) -> None:
        self.english.stop()
        self.other.stop()

    # ------------------------------------------------------------------ info
    async def health(self) -> Dict[str, Any]:
        """The English voice decides whether speech works at all.

        The second one is started lazily, the first time something non-English is said, so
        "not running" is its normal state and must not be reported as a fault.
        """
        main = await self.english.health()
        try:
            other = await self.other.health()
            note = other.get("detail", "") if other.get("ok") else "not started yet"
        except Exception:  # noqa: BLE001
            note = "not started yet"
        detail = f"{main.get('detail', '')}; non-English: {note}"
        return {**main, "detail": detail}

    async def voices(self) -> list:
        return await self.english.voices()
