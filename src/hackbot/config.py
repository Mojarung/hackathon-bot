"""Application settings, loaded once from the environment / .env file."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Where `.env` and `data/` live.

    Walking up from `__file__` only works for an editable install; a regular one
    lands in site-packages and would put the database somewhere surprising. The
    working directory is what the service actually sets, so it wins.
    """
    override = os.environ.get("HACKBOT_ROOT")
    if override:
        return Path(override).resolve()
    cwd = Path.cwd()
    if (cwd / ".env").exists() or (cwd / "pyproject.toml").exists():
        return cwd
    return Path(__file__).resolve().parents[2]


ROOT_DIR = _project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    bot_token: str
    bot_admin_ids: str = ""

    # LLM (Ollama Cloud, OpenAI-compatible surface)
    ollama_api_key: str = ""
    # A second key, used only when the first one answers with an error -
    # a spent quota or a revoked key, not a bad prompt.
    ollama_api_key_fallback: str = ""
    ollama_base_url: str = "https://ollama.com/v1"
    llm_model: str = "minimax-m3"
    llm_vision_model: str = "minimax-m3"
    llm_fun_model: str = ""  # humour generator; falls back to llm_model
    # The conversational agent may sit on another OpenAI-compatible provider
    # (Featherless, OpenRouter, a local gateway). Blank = same as Ollama above.
    chat_base_url: str = ""
    chat_api_key: str = ""
    llm_max_tokens: int = 2048

    # GitHub
    github_token: str = ""
    github_org: str = "Mojarung"
    github_private: bool = True

    # App
    tz_default: str = "Europe/Moscow"
    db_path: Path = Path("data/hackbot.db")
    files_dir: Path = Path("data/files")
    web_host: str = "0.0.0.0"
    web_port: int = 9999
    web_public_url: str = ""
    card_refresh_seconds: int = 60
    sticker_set: str = "mojarung"      # pack the bot sprinkles into the chat
    sticker_chance: float = 0.25       # 0 disables it entirely

    # Butting into a conversation nobody invited it to.
    banter_chance: float = 0.25        # per message; 0 disables it entirely
    banter_cooldown_seconds: int = 90   # per topic, so a burst cannot become a monologue
    banter_context: int = 8            # how many recent lines it gets to read
    # Covers reactions too. Off means the bot only comes alive in topics with a
    # hackathon, which leaves it mute in General - where most chatter happens.
    banter_everywhere: bool = True

    # Silent emoji reactions on other people's messages.
    reaction_chance: float = 0.25      # per message; 0 disables it entirely
    reaction_cooldown_seconds: int = 60  # per topic; reactions are cheap, so shorter
    log_level: str = "INFO"

    @property
    def admin_ids(self) -> set[int]:
        """Explicit allowlist. Empty means "fall back to chat administrators"."""
        out: set[int] = set()
        for chunk in self.bot_admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                out.add(int(chunk))
        return out

    @property
    def default_tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_default)

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.abs_db_path}"

    @property
    def abs_db_path(self) -> Path:
        p = self.db_path
        return p if p.is_absolute() else (ROOT_DIR / p)

    @property
    def abs_files_dir(self) -> Path:
        p = self.files_dir
        return p if p.is_absolute() else (ROOT_DIR / p)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.ollama_api_key)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)

    def public_url(self, path: str = "") -> str:
        base = self.web_public_url.rstrip("/")
        if not base:
            return ""
        return f"{base}/{path.lstrip('/')}" if path else base


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
