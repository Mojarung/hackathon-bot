"""Text helpers: transliteration, slugs, safe HTML and truncation."""

from __future__ import annotations

import html
import re

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    """Cyrillic to latin, good enough for repository names."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in text.casefold())


def slugify(text: str, *, max_len: int = 48) -> str:
    """`Тендер Хак Нижний` -> `tender_hak_nizhniy`. Always safe as a repo name."""
    slug = translit(text or "")
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    slug = re.sub(r"_{2,}", "_", slug)
    return slug[:max_len].strip("_") or "hackathon"


def repo_name(title: str, year: int) -> str:
    """The organisation convention: `<название_хакатона>_<год>`."""
    return f"{slugify(title, max_len=80)}_{year}"


def esc(text: str | None) -> str:
    """Escape for Telegram HTML parse mode. Only < > & need handling."""
    return html.escape(text or "", quote=False)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def find_urls(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for url in _URL_RE.findall(text):
        cleaned = url.rstrip(".,;:!?)")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://", "tg://")):
        url = "https://" + url
    return url


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Strip path separators and anything a filesystem would object to."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:120] or fallback


def progress_bar(ratio: float, width: int = 10, *, filled: str = "▓", empty: str = "░") -> str:
    ratio = min(1.0, max(0.0, ratio))
    done = round(ratio * width)
    return filled * done + empty * (width - done)
