"""The one interface every voice must satisfy.

Kept in its own module so provider implementations (which live in sibling modules)
can import it without a circular import through the package's `__init__`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple


class TTSProvider(ABC):
    name = "base"

    @abstractmethod
    async def synthesize(self, text: str, style: Any = None, lead: bool = True) -> Tuple[bytes, str]:
        """Speak `text`, optionally in the delivery chosen by the emotion engine. Returns (audio, mime).

        `lead` is False for every piece of a reply after the first, so a voice that opens with a
        breath or a sigh does it once per answer rather than once per sentence.
        """

    @property
    def expressive(self) -> bool:
        """True when the provider can perform delivery tags such as [laughs]."""
        return False

    @property
    def speech_model(self) -> str:
        """Which delivery dialect this voice speaks (see app/emotion/speech.py)."""
        return self.name

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "detail": self.name}

    async def voices(self) -> list:
        return []

    def stop(self) -> None:
        """Release anything the provider owns (a model server process, a handle). Optional."""
