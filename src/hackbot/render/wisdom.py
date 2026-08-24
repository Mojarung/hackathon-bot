"""Rendering for the daily joke advice."""

from __future__ import annotations

import random

from hackbot.agent.wisdom import Advice
from hackbot.domain.textutils import esc

_ASK_LINES = (
    "поделись мудростью на сегодня 🙏",
    "твоя очередь делиться мудростью на сегодня",
    "а какая у тебя мудрость на сегодня?",
    "выдай мудрость дня, команда ждёт",
    "поделись сокровенным знанием на сегодня",
)

_MINE_LINES = (
    "Пока моя версия:",
    "А у меня на сегодня так:",
    "Моя мудрость на сегодня:",
    "С меня начну:",
    "Вот что накопил я:",
)

_ICONS = ("🧘", "🪷", "📿", "🕯", "🔮", "🧠")


def render_advice(advice: Advice, header: str, *, icon: str | None = None) -> str:
    """Just the advice, no ping."""
    icon = icon or random.choice(_ICONS)
    body = esc(advice.text)
    if advice.accent:
        body += f" <b>{esc(advice.accent)}</b>"
    return f"{icon} <b>{esc(header)}</b>\n\n{body}"


def render_wisdom_ping(
    advice: Advice,
    header: str,
    *,
    mention_html: str,
    icon: str | None = None,
) -> str:
    """Ping a person for their wisdom and offer the bot's own in the same breath."""
    icon = icon or random.choice(_ICONS)
    body = esc(advice.text)
    if advice.accent:
        body += f" <b>{esc(advice.accent)}</b>"
    return (
        f"{icon} {mention_html}, {esc(random.choice(_ASK_LINES))}\n\n"
        f"<i>{esc(random.choice(_MINE_LINES))}</i>\n"
        f"{body}"
    )
