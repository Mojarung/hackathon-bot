"""Prompts live in `prompts/*.md`, not in the source.

Two reasons. Tuning a bot's character is editing prose, and prose does not
belong wedged between imports. And the file is re-read whenever its mtime
changes, so a reworded persona takes effect on the next message instead of
waiting for a redeploy.

If a file is missing or unreadable the built-in default is used, so a typo in
the prompts directory can never take the bot down.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from hackbot.config import ROOT_DIR

log = logging.getLogger(__name__)

PROMPTS_DIR = ROOT_DIR / "prompts"

# Notes to self inside the file are for the human editing it, not for the model.
_NOTE_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_notes(text: str) -> str:
    return _NOTE_RE.sub("", text).strip()


@dataclass(frozen=True, slots=True)
class _Cached:
    text: str
    mtime: float


_cache: dict[str, _Cached] = {}


def prompt_path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.md"


def load(name: str, fallback: str) -> str:
    """Current text of a prompt, re-read only when the file actually changed."""
    path = prompt_path(name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return fallback

    cached = _cache.get(name)
    if cached is not None and cached.mtime == mtime:
        return cached.text

    try:
        text = _strip_notes(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("could not read prompt %s: %s", path, exc)
        return fallback

    if not text:
        log.warning("prompt %s is empty, using the built-in default", path)
        return fallback

    _cache[name] = _Cached(text=text, mtime=mtime)
    log.info("prompt %r loaded from %s (%s chars)", name, path, len(text))
    return text


def write_defaults(defaults: dict[str, str]) -> list[Path]:
    """Materialise built-in prompts on first run so there is something to edit."""
    written: list[Path] = []
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in defaults.items():
        path = prompt_path(name)
        if path.exists():
            continue
        path.write_text(text.strip() + "\n", encoding="utf-8")
        written.append(path)
    return written
