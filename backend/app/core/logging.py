"""Structured logging.

Writes human-readable logs to the console and JSON-lines logs to `logs/zeta.jsonl`.
Secrets are scrubbed by `SecretScrubber` before anything is written.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{10,})"),
    re.compile(r"(xox[abp]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{20,})"),
    re.compile(r"(AKIA[0-9A-Z]{16})"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)([\"'\s:=]+)([^\s\"',;]{6,})"),
]


class SecretScrubber(logging.Filter):
    """Redacts known secret values and secret-looking patterns from log records."""

    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***REDACTED***")
        for pat in _SECRET_PATTERNS:
            if pat.groups >= 3:
                text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)
            else:
                text = pat.sub("***REDACTED***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = self.scrub(msg)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("task_id", "tool", "event", "duration_ms", "conversation_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False
_scrubber = SecretScrubber()


def configure_logging(logs_dir: Path, level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    global _configured, _scrubber
    logs_dir.mkdir(parents=True, exist_ok=True)
    _scrubber = SecretScrubber(secrets)
    root = logging.getLogger()
    if _configured:
        for h in root.handlers:
            h.filters.clear()
            h.addFilter(_scrubber)
        root.setLevel(level)
        return
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    console.addFilter(_scrubber)
    root.addHandler(console)

    jsonl = logging.handlers.RotatingFileHandler(logs_dir / "zeta.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    jsonl.setFormatter(JsonFormatter())
    jsonl.addFilter(_scrubber)
    root.addHandler(jsonl)

    for noisy in ("httpx", "httpcore", "uvicorn.access", "watchfiles", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def scrub(text: str) -> str:
    return _scrubber.scrub(text)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
