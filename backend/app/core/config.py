"""Configuration for Zeta.

Everything is driven by environment variables (or a `.env` file next to the
backend).  Validation happens at import time so misconfiguration fails early
with a readable error rather than a mysterious runtime failure.

Secrets (API keys, passwords) are held here but are NEVER placed into prompts.
See `app/security/secrets.py` for how tools obtain them.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class ZetaMode(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class LLMProviderName(str, Enum):
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    MOCK = "mock"


class STTProviderName(str, Enum):
    DISABLED = "disabled"
    WHISPER = "whisper"          # faster-whisper, local
    OPENAI = "openai"            # OpenAI-compatible /audio/transcriptions
    ELEVENLABS = "elevenlabs"    # ElevenLabs Scribe (cloud, uses ELEVENLABS_API_KEY)


class TTSProviderName(str, Enum):
    DISABLED = "disabled"
    CHATTERBOX = "chatterbox"    # Resemble AI Chatterbox, local GPU, voice cloning from a short clip
    LOCAL = "local"              # Windows SAPI (System.Speech) - zero dependencies
    PIPER = "piper"
    ELEVENLABS = "elevenlabs"


class WhatsAppProviderName(str, Enum):
    DISABLED = ""
    WEB = "web"
    BUSINESS = "business"
    WAPI = "wapi"


class EmailProviderName(str, Enum):
    DISABLED = ""
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    IMAP = "imap"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- General -----------------------------------------------------
    zeta_mode: ZetaMode = ZetaMode.LOCAL
    zeta_name: str = "Zeta"
    host: str = "127.0.0.1"
    port: int = 8765
    debug: bool = False
    log_level: str = "INFO"
    data_dir: Path = Field(default=PROJECT_DIR / "data")
    logs_dir: Path = Field(default=PROJECT_DIR / "logs")
    database_url: str = ""  # default derived from data_dir
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8765,http://127.0.0.1:8765,tauri://localhost"
    # Shared secret for the API when exposed beyond localhost (cloud mode).
    api_token: str = ""

    # ---- LLM ---------------------------------------------------------
    llm_provider: LLMProviderName = LLMProviderName.OLLAMA
    llm_model: str = "llama3.1"
    llm_base_url: str = ""          # override provider default
    llm_api_key: str = ""           # generic key for openai_compatible
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_seconds: int = 180
    llm_supports_tools: bool = True  # if False, Zeta falls back to JSON tool-calls in text
    llm_vision_model: str = ""       # optional separate vision-capable model for screen analysis
    llm_context_length: int = 16384  # Ollama num_ctx; the tool schemas need >4096 (Ollama default) or they get truncated
    llm_think: bool = False          # Ollama "thinking" models (qwen3, deepseek-r1): enable reasoning mode (slower)
    llm_stream: bool = True          # stream reply tokens to the UI as they are generated
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    lmstudio_base_url: str = "http://localhost:1234/v1"
    embedding_provider: str = ""      # "" | "ollama" | "openai"
    embedding_model: str = "nomic-embed-text"

    # ---- Agent -------------------------------------------------------
    agent_max_steps: int = 25
    agent_history_messages: int = 30
    confirmation_timeout_seconds: int = 300
    tool_timeout_seconds: int = 120
    tool_output_max_chars: int = 12000

    # ---- Voice -------------------------------------------------------
    stt_provider: STTProviderName = STTProviderName.DISABLED
    stt_model: str = "base"          # faster-whisper model size, or API model name
    stt_language: str = ""           # pin one language; empty = choose per utterance
    stt_languages: str = ""          # narrow that choice, e.g. "en,hi"; empty = all 99
    stt_device: str = "auto"         # auto | cpu | cuda  (auto tries the GPU and falls back to CPU)
    stt_base_url: str = ""           # for openai-compatible STT
    stt_api_key: str = ""
    tts_provider: TTSProviderName = TTSProviderName.DISABLED
    voice_provider: str = ""         # alias of tts_provider (spec compatibility)
    tts_voice: str = ""              # SAPI voice name / ElevenLabs voice id / piper model path
    tts_rate: int = 0                # SAPI rate -10..10
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    piper_executable: str = "piper"
    wake_word_enabled: bool = False
    wake_word: str = "hey zeta"
    # ---- Chatterbox (local neural voice) -----------------------------
    chatterbox_base_url: str = "http://127.0.0.1:8766"
    chatterbox_voice: str = ""            # reference .wav for the voice; empty = the model's built-in voice
    chatterbox_model: str = "turbo"       # turbo (fast, English) | base | multilingual (23 languages)
    chatterbox_device: str = "auto"       # auto | cuda | cpu
    chatterbox_exaggeration: float = 0.5  # neutral 0.5; higher = more emotional
    chatterbox_cfg_weight: float = 0.5    # lower = slower, more deliberate pacing
    chatterbox_temperature: float = 0.8
    chatterbox_autostart: bool = True     # start the voice server on first use if it is not already running
    # A second voice for languages the main model cannot speak. Turbo is English-only and fast;
    # multilingual says Hindi properly but runs about five times slower, so Zeta runs both and
    # picks per reply rather than making English pay for Hindi.
    chatterbox_non_english_model: str = ""        # "" = off. e.g. multilingual
    chatterbox_non_english_url: str = "http://127.0.0.1:8767"
    chatterbox_language: str = ""          # "" = detect from the reply (Devanagari -> Hindi); needs the multilingual model
    chatterbox_idle_unload: float = 300.0  # safety-net timer; Zeta parks the voice itself before each turn (0 = never)
    gpu_share: str = "auto"               # auto | on | off. On a small card, let the voice and the LLM take turns

    # ---- Emotional intelligence -------------------------------------
    emotion_enabled: bool = True         # detect how the person feels and adapt the reply
    emotion_prosody: bool = True         # also analyse tone of voice, not just words
    emotion_adapt_voice: bool = True     # adapt the spoken delivery (ElevenLabs voice settings / v3 tags)
    emotion_region: str = "in"           # which crisis helplines to offer: in | us | uk | intl
    emotion_support_mode: bool = True    # allow Zeta to prioritise support over task efficiency when someone is struggling
    wake_word_engine: str = "auto"       # auto | whisper | openwakeword
    wake_word_model: str = ""            # openwakeword model name/path (e.g. hey_jarvis) - whisper engine ignores it
    wake_word_sensitivity: float = 0.5   # openwakeword score threshold (0-1)
    wake_word_device: str = ""           # sounddevice input device name/index; empty = default microphone

    # ---- Filesystem --------------------------------------------------
    # Semicolon-separated roots Zeta may access.  Defaults to the user's home.
    fs_allowed_roots: str = ""
    fs_index_roots: str = ""          # roots to index (defaults to allowed roots)
    fs_index_exclude: str = "node_modules;.git;__pycache__;AppData;.venv;venv;$Recycle.Bin;Windows;Program Files;Program Files (x86);ProgramData;.cache;.npm;.nuget;.gradle"
    fs_index_on_startup: bool = True
    fs_index_content: bool = True
    fs_index_content_max_bytes: int = 2_000_000
    fs_max_read_bytes: int = 5_000_000

    # ---- Terminal ----------------------------------------------------
    terminal_shell: str = "powershell"   # powershell | cmd | bash
    terminal_timeout_seconds: int = 120
    terminal_cwd: str = ""

    # ---- Browser -----------------------------------------------------
    browser_headless: bool = False
    browser_profile_dir: str = ""     # persistent profile (cookies) - defaults to data/browser_profile
    browser_search_engine: str = "duckduckgo"  # duckduckgo | bing | google
    browser_download_dir: str = ""

    # ---- Messaging ---------------------------------------------------
    whatsapp_provider: WhatsAppProviderName = WhatsAppProviderName.DISABLED
    whatsapp_confirm_send: bool = True
    whatsapp_business_token: str = ""
    whatsapp_business_phone_id: str = ""
    wapi_base_url: str = ""
    wapi_token: str = ""
    wapi_instance_id: str = ""
    contacts_file: str = ""           # JSON list of {name, phone, email}

    # ---- Email -------------------------------------------------------
    email_provider: EmailProviderName = EmailProviderName.DISABLED
    email_address: str = ""
    email_password: str = ""          # app password
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_confirm_send: bool = True
    email_auth: str = "password"      # password | oauth
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_tenant: str = "common"

    # ---- Developer ---------------------------------------------------
    github_token: str = ""

    # ---- Cloud (optional) --------------------------------------------
    aws_enabled: bool = False
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = ""

    # ---- Secrets -----------------------------------------------------
    secret_store: str = "env"          # env | keyring

    # ---- Derived -----------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid LOG_LEVEL: {v}")
        return v

    @model_validator(mode="after")
    def _finalize(self) -> "Settings":
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.logs_dir = Path(self.logs_dir).expanduser().resolve()
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{(self.data_dir / 'zeta.db').as_posix()}"
        if self.voice_provider and self.tts_provider == TTSProviderName.DISABLED:
            try:
                self.tts_provider = TTSProviderName(self.voice_provider.lower())
            except ValueError as e:
                raise ValueError(f"Invalid VOICE_PROVIDER: {self.voice_provider}") from e
        if not self.fs_allowed_roots:
            self.fs_allowed_roots = str(Path.home())
        if not self.fs_index_roots:
            self.fs_index_roots = self.fs_allowed_roots
        if not self.browser_profile_dir:
            self.browser_profile_dir = str(self.data_dir / "browser_profile")
        if not self.browser_download_dir:
            self.browser_download_dir = str(Path.home() / "Downloads")
        if not self.contacts_file:
            self.contacts_file = str(BACKEND_DIR / "config" / "contacts.json")
        if self.zeta_mode == ZetaMode.CLOUD and self.host not in ("127.0.0.1", "localhost") and not self.api_token:
            raise ValueError("API_TOKEN is required when ZETA_MODE=cloud and HOST is not localhost")
        # Provider-specific sanity checks (fail early, clear message)
        if self.llm_provider == LLMProviderName.OPENAI and not (self.openai_api_key or self.llm_api_key):
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.llm_provider == LLMProviderName.ANTHROPIC and not (self.anthropic_api_key or self.llm_api_key):
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.llm_provider == LLMProviderName.OPENROUTER and not (self.openrouter_api_key or self.llm_api_key):
            raise ValueError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter")
        if self.tts_provider == TTSProviderName.ELEVENLABS and not self.elevenlabs_api_key:
            raise ValueError("ELEVENLABS_API_KEY is required when TTS_PROVIDER=elevenlabs")
        if self.stt_provider == STTProviderName.ELEVENLABS and not self.elevenlabs_api_key:
            raise ValueError("ELEVENLABS_API_KEY is required when STT_PROVIDER=elevenlabs")
        return self

    # ---- Helpers -----------------------------------------------------
    @property
    def allowed_roots(self) -> List[Path]:
        return [Path(p.strip()).expanduser().resolve() for p in self.fs_allowed_roots.split(";") if p.strip()]

    @property
    def index_roots(self) -> List[Path]:
        return [Path(p.strip()).expanduser().resolve() for p in self.fs_index_roots.split(";") if p.strip()]

    @property
    def index_excludes(self) -> List[str]:
        return [p.strip().lower() for p in self.fs_index_exclude.split(";") if p.strip()]

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_local(self) -> bool:
        return self.zeta_mode == ZetaMode.LOCAL

    @property
    def env_file_path(self) -> Path:
        return PROJECT_DIR / ".env"

    def secret_values(self) -> List[str]:
        """All secret values, used by the log scrubber. Never expose elsewhere."""
        return [
            v for v in (
                self.api_token, self.llm_api_key, self.openai_api_key, self.anthropic_api_key,
                self.stt_api_key, self.openrouter_api_key, self.elevenlabs_api_key, self.whatsapp_business_token, self.wapi_token,
                self.email_password, self.github_token, self.google_client_secret, self.aws_access_key_id, self.aws_secret_access_key,
            ) if v
        ]

    def public_summary(self) -> dict:
        """Non-secret configuration summary for the UI."""
        return {
            "mode": self.zeta_mode.value,
            "llm_provider": self.llm_provider.value,
            "llm_model": self.llm_model,
            "stt_provider": self.stt_provider.value,
            "tts_provider": self.tts_provider.value,
            "whatsapp_provider": self.whatsapp_provider.value or "disabled",
            "email_provider": self.email_provider.value or "disabled",
            "allowed_roots": [str(p) for p in self.allowed_roots],
            "emotion_enabled": self.emotion_enabled,
            "emotion_prosody": self.emotion_prosody,
            "emotion_adapt_voice": self.emotion_adapt_voice,
            "emotion_region": self.emotion_region,
            "elevenlabs_model": self.elevenlabs_model,
            "chatterbox_voice": self.chatterbox_voice,
            "chatterbox_model": self.chatterbox_model,
            "chatterbox_device": self.chatterbox_device,
            "wake_word_enabled": self.wake_word_enabled,
            "wake_word": self.wake_word,
            "wake_word_engine": self.wake_word_engine,
            "llm_stream": self.llm_stream,
            "tts_voice": self.tts_voice,
            "api_token_set": bool(self.api_token),
            "database_backend": "sqlite" if self.database_url.startswith("sqlite") else "postgresql",
            "browser_headless": self.browser_headless,
        }


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
        _settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def reload_settings() -> Settings:
    """Re-read `.env` (used after the setup wizard writes configuration)."""
    global _settings
    _settings = None
    return get_settings()
