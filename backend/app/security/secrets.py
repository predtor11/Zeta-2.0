"""Secret store.

Tools ask the SecretStore for credentials at execution time.  Secrets never
enter prompts, tool schemas, tool results, logs, or the UI.

Backends:
  * env      - values from Settings / environment (.env)
  * keyring  - OS credential store (Windows Credential Manager) via `keyring`
               package; falls back to env when a key is absent.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from app.core.config import Settings

log = logging.getLogger(__name__)

SERVICE_NAME = "zeta"


class SecretStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._backend = settings.secret_store.lower()
        self._keyring = None
        if self._backend == "keyring":
            try:
                import keyring  # type: ignore

                self._keyring = keyring
            except ImportError:
                log.warning("SECRET_STORE=keyring but the 'keyring' package is not installed; falling back to env")
                self._backend = "env"

    def _from_env(self, name: str) -> Optional[str]:
        mapping: Dict[str, str] = {
            "elevenlabs_api_key": self._settings.elevenlabs_api_key,
            "openai_api_key": self._settings.openai_api_key,
            "anthropic_api_key": self._settings.anthropic_api_key,
            "llm_api_key": self._settings.llm_api_key,
            "stt_api_key": self._settings.stt_api_key,
            "whatsapp_business_token": self._settings.whatsapp_business_token,
            "wapi_token": self._settings.wapi_token,
            "email_password": self._settings.email_password,
            "github_token": self._settings.github_token,
            "aws_access_key_id": self._settings.aws_access_key_id,
            "aws_secret_access_key": self._settings.aws_secret_access_key,
            "api_token": self._settings.api_token,
        }
        return mapping.get(name) or None

    def get(self, name: str) -> Optional[str]:
        if self._keyring is not None:
            try:
                v = self._keyring.get_password(SERVICE_NAME, name)
                if v:
                    return v
            except Exception as e:  # noqa: BLE001
                log.warning("keyring lookup failed for %s: %s", name, e.__class__.__name__)
        return self._from_env(name)

    def set(self, name: str, value: str) -> bool:
        if self._keyring is None:
            return False
        try:
            self._keyring.set_password(SERVICE_NAME, name, value)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("keyring store failed for %s: %s", name, e.__class__.__name__)
            return False

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def mask(self, name: str) -> str:
        v = self.get(name)
        if not v:
            return "(not set)"
        return f"{v[:3]}…{v[-2:]}" if len(v) > 8 else "***"

    def all_values(self):
        """Used ONLY by the log scrubber."""
        return [v for v in (self._from_env(k) for k in (
            "elevenlabs_api_key", "openai_api_key", "anthropic_api_key", "llm_api_key", "stt_api_key",
            "whatsapp_business_token", "wapi_token", "email_password", "github_token",
            "aws_access_key_id", "aws_secret_access_key", "api_token")) if v]
