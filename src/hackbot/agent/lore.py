"""Chat lore: who these people are, how they talk, what they keep joking about.

Two tiers, because they cost different things. The compact core rides in every
prompt - it is what stops the bot answering like a stranger who wandered into
someone else's chat. The corpus and the quote bank are three times bigger and
only matter when someone actually asks about a name, a meme or a past hackathon,
so they are searched on demand through a tool instead.

The files are deliberately kept out of git: they carry real names, birthdays and
health details of living people, and the repository is public. They are copied to
the server the same way the service-account key is. Everything here degrades to
"no lore" when they are missing, so a plain checkout still runs - and the tests
must not assume they exist.
"""

from __future__ import annotations

import logging
import re

from hackbot.agent import prompts

log = logging.getLogger(__name__)

CORE = "lore"
DEEP = ("lore_corpus", "lore_quotes")

# Whole sections are returned, never single lines. A quote torn out of its
# exchange loses the thing worth copying - the rhythm of who answers whom.
_HEADING = re.compile(r"^#{1,3} .+$", re.M)
_WORD = re.compile(r"[\w-]{3,}", re.U)

MAX_HITS = 3
MAX_CHARS = 2600


def compact() -> str:
    """The core that rides in every prompt. Empty string when lore is absent."""
    return prompts.load(CORE, "")


def installed() -> bool:
    return bool(compact())


def _sections(name: str) -> list[str]:
    text = prompts.load(name, "")
    if not text:
        return []
    starts = [m.start() for m in _HEADING.finditer(text)]
    if not starts:
        return [text]
    bounds = [*starts, len(text)]
    chunks = (text[bounds[i] : bounds[i + 1]].strip() for i in range(len(starts)))
    return [c for c in chunks if c]


def search(query: str) -> str:
    """Sections of the deep lore that match the query, most relevant first."""
    words = {w.casefold() for w in _WORD.findall(query or "")}
    if not words:
        return ""

    scored: list[tuple[int, int, str]] = []
    for name in DEEP:
        for section in _sections(name):
            hay = section.casefold()
            # Rank by how many distinct query words land, not by raw count: a
            # section repeating one name is a worse answer than one that ties
            # two of the asked-about things together.
            hits = sum(1 for w in words if w in hay)
            if hits:
                scored.append((hits, -len(section), section))

    if not scored:
        return ""

    scored.sort(reverse=True)
    out: list[str] = []
    budget = MAX_CHARS
    for _, _, section in scored[:MAX_HITS]:
        if len(section) > budget:
            section = section[:budget].rsplit("\n", 1)[0]
        if not section.strip():
            break
        out.append(section)
        budget -= len(section)
        if budget <= 0:
            break
    return "\n\n".join(out)
