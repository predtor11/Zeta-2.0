"""How much room is left on the GPU.

Zeta's local stack wants the same card three times over: Ollama for the language model,
Chatterbox for the voice, faster-whisper for listening. On a desktop with 24 GB that is a
non-issue. On an 8 GB laptop card it is *the* issue - when a model does not fit, Ollama
quietly runs the overflowing layers on the CPU and a two-second reply becomes a
four-minute one. So Zeta measures instead of hoping.

`nvidia-smi` is used rather than a Python binding because the backend virtualenv has no
CUDA packages in it, and the tool ships with every NVIDIA driver. The reading is cached
for a moment: it is only needed at the few points where something is about to claim VRAM.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_CACHE: Tuple[float, Optional[Tuple[int, int]]] = (0.0, None)
_CACHE_SECONDS = 2.0


def gpu_memory(*, max_age: float = _CACHE_SECONDS) -> Optional[Tuple[int, int]]:
    """(total MB, free MB) for the first NVIDIA GPU, or None when there isn't one."""
    global _CACHE
    age, cached = _CACHE
    if cached is not None and time.monotonic() - age < max_age:
        return cached
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        total, free = (int(x.strip()) for x in out.strip().splitlines()[0].split(","))
    except Exception as e:  # noqa: BLE001
        log.debug("nvidia-smi unavailable: %s", e)
        return None
    _CACHE = (time.monotonic(), (total, free))
    return total, free


def free_mb() -> int:
    """Unused VRAM in MB. -1 when there is no NVIDIA GPU to ask about."""
    mem = gpu_memory()
    return mem[1] if mem else -1


def needs_room_for(megabytes: int) -> bool:
    """True when something wanting `megabytes` would not fit as things stand.

    False when there is no GPU at all: nothing to arbitrate, everything is on the CPU
    already and the caller should not start evicting things.
    """
    mem = gpu_memory()
    return bool(mem and mem[1] < megabytes)
