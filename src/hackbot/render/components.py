"""Shared building blocks for Telegram HTML messages.

Only the tags Telegram actually supports are used: b, i, u, s, code, pre, a,
blockquote (optionally expandable) and tg-time. Everything user-supplied goes
through `esc` first.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import EventKind
from hackbot.domain.textutils import esc, progress_bar, truncate
from hackbot.domain.timeutils import (
    MONTHS_GEN,
    fmt_dt,
    fmt_range,
    fmt_time,
    fmt_when,
    humanize_delta,
    now_utc,
    to_local,
)

RULE = "━━━━━━━━━━━━━━━━━━━━"
THIN_RULE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def title_line(hack: Hackathon) -> str:
    """`ТЕНДЕР ХАК · Нижний Новгород · 2026`, upper-cased for weight."""
    bits = [hack.title.upper()]
    if hack.city:
        bits.append(hack.city)
    elif hack.is_online:
        bits.append("онлайн")
    bits.append(str(hack.year))
    return " · ".join(esc(b) for b in bits)


def date_span(hack: Hackathon, tz: ZoneInfo) -> str:
    """`20–22 сентября` or `20 сентября – 3 октября`, blank when unknown."""
    if not hack.starts_at:
        return ""
    start = to_local(hack.starts_at, tz)
    if not hack.ends_at:
        return f"{start.day} {MONTHS_GEN[start.month - 1]}"
    end = to_local(hack.ends_at, tz)
    if start.month == end.month and start.year == end.year:
        if start.day == end.day:
            return f"{start.day} {MONTHS_GEN[start.month - 1]}"
        return f"{start.day}–{end.day} {MONTHS_GEN[start.month - 1]}"
    return f"{start.day} {MONTHS_GEN[start.month - 1]} – {end.day} {MONTHS_GEN[end.month - 1]}"


def event_line(
    event: Event, tz: ZoneInfo, *, now: datetime | None = None, with_relative: bool = True
) -> str:
    """One-line event summary used inside cards and digests."""
    now = now or now_utc()
    when = fmt_when(event.starts_at, tz, now)
    out = f"{event.kind.emoji} <b>{esc(event.title)}</b>\n     {when}"
    if event.ends_at:
        out += f" – {fmt_time(event.ends_at, tz)}"
    if with_relative and event.starts_at > now:
        out += f" · через {humanize_delta((event.starts_at - now).total_seconds())}"
    return out


def event_meta(event: Event) -> str:
    """Place and link, rendered only when present."""
    bits: list[str] = []
    if event.place:
        bits.append(f"📍 {esc(truncate(event.place, 60))}")
    if event.url:
        bits.append(f'🔗 <a href="{esc(event.url)}">ссылка</a>')
    return " · ".join(bits)


def countdown_block(target: Event | None, hack: Hackathon, now: datetime) -> str:
    """The single most important number on the card."""
    if target is not None and target.starts_at > now:
        left = (target.starts_at - now).total_seconds()
        label = (
            "до сдачи решения"
            if target.kind is EventKind.SUBMISSION
            else f"до «{esc(target.title.lower())}»"
        )
        return f"⏳ {label} — <b>{humanize_delta(left, parts=3)}</b>"
    if hack.starts_at and now < hack.starts_at:
        left = (hack.starts_at - now).total_seconds()
        return f"⏳ до старта — <b>{humanize_delta(left, parts=3)}</b>"
    if hack.ends_at and now < hack.ends_at:
        left = (hack.ends_at - now).total_seconds()
        return f"⏳ до конца — <b>{humanize_delta(left, parts=3)}</b>"
    return ""


def progress_block(ratio: float | None, hack: Hackathon, tz: ZoneInfo, now: datetime) -> str:
    if ratio is None:
        return ""
    bar = progress_bar(ratio, 10)
    pct = round(ratio * 100)
    day_note = ""
    if hack.starts_at and hack.ends_at:
        first_day = to_local(hack.starts_at, tz).date()
        last_day = to_local(hack.ends_at, tz).date()
        total_days = max(1, (last_day - first_day).days + 1)
        current_day = (to_local(now, tz).date() - first_day).days + 1
        if 1 <= current_day <= total_days:
            day_note = f"  ·  день {current_day} из {total_days}"
    return f"<code>{bar}</code> {pct}%{day_note}"


def links_block(hack: Hackathon, *, limit: int = 8) -> str:
    if not hack.links:
        return ""
    rows = [
        f'{link.kind.emoji} <a href="{esc(link.url)}">{esc(link.title or link.kind.label)}</a>'
        for link in hack.links[:limit]
    ]
    return "\n".join(rows)


def timeline_marker(event: Event, now: datetime) -> str:
    if event.ends_at and event.starts_at <= now < event.ends_at:
        return "▶️"
    if event.starts_at <= now:
        return "✅"
    return "⬜"


def deadline_badge(event: Event, primary: Event | None) -> str:
    if primary is not None and event.id == primary.id:
        return "  ⟵ <b>дедлайн</b>"
    if event.is_mandatory:
        return "  ⟵ обязательно"
    return ""


def quote(text: str, *, expandable: bool = False) -> str:
    """Telegram collapses long expandable quotes behind a `Show more` control."""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{text}</blockquote>"


def footer_updated(now: datetime, tz: ZoneInfo) -> str:
    return f"<i>обновлено {fmt_time(now, tz)}</i>"


def when_full(event: Event, tz: ZoneInfo) -> str:
    if event.ends_at:
        return fmt_range(event.starts_at, event.ends_at, tz)
    return fmt_dt(event.starts_at, tz)
