"""Two Chatterbox voices, chosen by the language of the reply.

Neither model is the right answer on its own. Measured on an RTX 4070 Laptop (8 GB) on the
*same* 150-character English sentence, each model running alone - comparing different sentences
flatters the slower model, because fixed overheads dominate a short one:

* **Turbo** speaks English at 0.42x realtime (7.5-9.4 s of audio for 18-22 s of compute). It
  cannot say a word of Hindi: given Devanagari it produced 16 s of audio that transcribes back
  as "Comeway. Comewood's lit-scar...".
* **Multilingual** says Hindi properly (Whisper detects `hi` at p=1.00) but takes 33-38 s for
  the same sentence - 0.17x realtime, about 2.4x turbo's cost per second of speech.

So Zeta keeps both and picks per reply. Only one holds the graphics card at a time: before
speaking, the other is parked into system RAM and comes back in 2-3 s when it is next needed.
That is the same handover already used between the voice and the language model, so switching
language costs one resume, not a model load.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Tuple

from app.providers.tts.base import TTSProvider
from app.providers.tts.chatterbox import ChatterboxTTS

log = logging.getLogger(__name__)

PARK_ATTEMPTS = 8            # a sentence takes a few seconds; wait it out rather than barge in
PARK_RETRY_SECONDS = 2.0


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
        and is spoken by turbo, which handles it passably - and about twice as quickly as the
        multilingual model would say the same sentence.
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
        await self._free_card(self.other if voice is self.english else self.english)
        return await voice.synthesize(text, style, lead)

    async def _free_card(self, idle: ChatterboxTTS) -> bool:
        """Wait for the other voice to hand the card back before loading into it.

        Asking once and carrying on is not a small mistake. With both models resident an 8 GB
        card runs out - measured at 84 MB free - and Windows silently moves the overflow into
        shared system RAM rather than failing. Generation then falls off a cliff: turbo went
        from 9 iterations a second to one every 106 seconds, which is indistinguishable from a
        hang. So if the other voice is mid-sentence, wait for it; a sentence ends soon enough.
        """
        for _ in range(PARK_ATTEMPTS):
            try:
                state = await idle.release_gpu()
            except Exception as e:  # noqa: BLE001
                log.debug("could not reach the %s voice to park it: %s", idle.model, e)
                return True                       # not running, so it is holding nothing
            if state.get("free"):
                return True
            if not state.get("busy"):
                return False                      # it will not move and is not speaking either
            await asyncio.sleep(PARK_RETRY_SECONDS)
        log.warning("The %s voice would not release the GPU; speaking anyway, which will be slow.", idle.model)
        return False

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
