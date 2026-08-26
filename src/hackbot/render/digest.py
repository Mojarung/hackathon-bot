"""Reminder pings, the morning digest and other scheduled announcements."""

from __future__ import annotations

from datetime import datetime

from hackbot.db.models import Event, Hackathon, Participant
from hackbot.domain.enums import EventKind
from hackbot.domain.services.events import ProposedEvent
from hackbot.domain.services.hackathons import hack_tz, primary_deadline
from hackbot.domain.services.ics import google_calendar_link
from hackbot.domain.textutils import esc
from hackbot.domain.timeutils import (
    fmt_time,
    fmt_when,
    humanize_delta,
    n_plural,
    now_utc,
    to_local,
)
from hackbot.render.components import RULE, event_meta, links_block, title_line, when_full


def _lead(offset_minutes: int) -> str:
    """`Через 1 час`, `Через 15 минут`, `Через 2 дня`."""
    if offset_minutes >= 1440 and offset_minutes % 1440 == 0:
        days = offset_minutes // 1440
        return f"Через {n_plural(days, 'день', 'дня', 'дней')}"
    if offset_minutes >= 60 and offset_minutes % 60 == 0:
        hours = offset_minutes // 60
        return f"Через {n_plural(hours, 'час', 'часа', 'часов')}"
    return f"Через {n_plural(offset_minutes, 'минуту', 'минуты', 'минут')}"


def render_reminder(
    hack: Hackathon,
    event: Event,
    offset_minutes: int,
    *,
    now: datetime | None = None,
) -> str:
    now = now or now_utc()
    tz = hack_tz(hack)

    urgency = "🚨" if offset_minutes <= 30 and event.kind.is_critical else "⏰"
    lines = [
        f"{urgency} <b>{_lead(offset_minutes)}</b> — {event.kind.emoji} <b>{esc(event.title)}</b>",
        "",
        f"🕐 {esc(when_full(event, tz))}",
    ]

    meta = event_meta(event)
    if meta:
        lines.append(meta)
    if event.is_mandatory:
        lines.append("❗ <b>обязательное</b>")
    if event.notes:
        lines.append("")
        lines.append(f"<blockquote expandable>{esc(event.notes)}</blockquote>")

    # For the start and the registration cut-off the useful links are exactly
    # what people scramble for, so they ride along with the ping.
    if event.kind in {EventKind.START, EventKind.REGISTRATION}:
        links = links_block(hack, limit=5)
        if links:
            lines.append("")
            lines.append(links)

    # Rides on the footer line rather than its own: a ping is read in one glance,
    # and this is only for the few who never subscribed to the shared calendar.
    add_link = google_calendar_link(hack, event)
    lines.append("")
    lines.append(f'<a href="{esc(add_link)}">📅 в календарь</a> · <i>{esc(title_line(hack))}</i>')
    return "\n".join(lines)


def render_start_announcement(hack: Hackathon, events: list[Event]) -> str:
    """Posted when the hackathon actually starts: everything needed in one place."""
    tz = hack_tz(hack)
    lines = [
        f"🚀 <b>ПОЕХАЛИ — {esc(title_line(hack))}</b>",
        "",
    ]
    if hack.ends_at:
        lines.append(f"🏁 Финиш: {esc(fmt_when(hack.ends_at, tz))}")

    target = primary_deadline(hack, events)
    if target is not None:
        lines.append(f"📤 {esc(target.title)}: {esc(fmt_when(target.starts_at, tz))}")

    links = links_block(hack, limit=8)
    if links:
        lines.append("")
        lines.append("<b>Всё нужное под рукой</b>")
        lines.append(links)

    if hack.github_url:
        repo_label = esc(hack.github_repo or "репозиторий")
        lines.append(f'🐙 <a href="{esc(hack.github_url)}">{repo_label}</a>')

    lines.append("")
    lines.append("<i>Таймлайн — в закреплённой карточке.</i>")
    return "\n".join(lines)


def render_digest(
    hack: Hackathon,
    today: list[Event],
    all_events: list[Event],
    *,
    now: datetime | None = None,
) -> str:
    """Morning summary. Returns an empty string when there is nothing to say."""
    now = now or now_utc()
    tz = hack_tz(hack)
    local = to_local(now, tz)

    target = primary_deadline(hack, all_events)
    has_countdown = target is not None and target.starts_at > now
    if not today and not has_countdown:
        return ""

    day_note = ""
    if hack.starts_at and hack.ends_at:
        total = max(
            1,
            (to_local(hack.ends_at, tz).date() - to_local(hack.starts_at, tz).date()).days + 1,
        )
        current = (local.date() - to_local(hack.starts_at, tz).date()).days + 1
        if 1 <= current <= total:
            day_note = f"  ·  день {current} из {total}"

    lines = [
        f"☀️ <b>Сегодня</b>{day_note}",
        f"<i>{esc(title_line(hack))}</i>",
        RULE,
    ]

    if today:
        for event in sorted(today, key=lambda e: e.starts_at):
            mark = "✅" if event.starts_at <= now else "•"
            row = (
                f"{mark} {event.kind.emoji} <b>{fmt_time(event.starts_at, tz)}</b> — "
                f"{esc(event.title)}"
            )
            if event.ends_at:
                row += f" <i>(до {fmt_time(event.ends_at, tz)})</i>"
            lines.append(row)
            meta = event_meta(event)
            if meta:
                lines.append(f"     {meta}")
    else:
        lines.append("<i>сегодня этапов нет</i>")

    if has_countdown and target is not None:
        left = humanize_delta((target.starts_at - now).total_seconds(), parts=3)
        lines.append("")
        lines.append(f"⏳ До «{esc(target.title.lower())}» — <b>{left}</b>")

    return "\n".join(lines)


def render_ping(hack: Hackathon, event: Event | None, people: list[Participant],
                note: str | None = None) -> str:
    """Explicit `/ping` - the only place the bot tags people by name."""
    tz = hack_tz(hack)
    lines: list[str] = []
    if event is not None:
        lines.append(f"📣 {event.kind.emoji} <b>{esc(event.title)}</b>")
        lines.append(f"🕐 {esc(fmt_when(event.starts_at, tz))}")
    else:
        lines.append(f"📣 <b>{esc(title_line(hack))}</b>")
    if note:
        lines.append("")
        lines.append(esc(note))
    lines.append("")
    lines.append(" ".join(p.mention_html for p in people))
    return "\n".join(lines)


def render_status_change(hack: Hackathon, old_label: str) -> str:
    return (
        f"{hack.status.emoji} <b>{esc(title_line(hack))}</b>\n"
        f"Статус: <s>{esc(old_label)}</s> → <b>{esc(hack.status.label)}</b>"
    )


def render_change_note(actor: str, action: str, detail: str) -> str:
    """Short public note so the topic sees who changed what."""
    who = f"<b>{esc(actor)}</b>" if actor else "Кто-то"
    return f"✏️ {who} {esc(action)}: {detail}"


def render_alarm(hack: Hackathon, event: Event, now: datetime | None = None) -> str:
    """Final-stretch nag before a critical deadline."""
    now = now or now_utc()
    tz = hack_tz(hack)
    left = (event.starts_at - now).total_seconds()
    return (
        f"🚨 <b>{humanize_delta(left, parts=2)} до «{esc(event.title.lower())}»</b>\n"
        f"🕐 {esc(fmt_time(event.starts_at, tz))}\n\n"
        f"<i>{esc(title_line(hack))}</i>"
    )


def render_conflict_warning(first: Event, second: Event, hack: Hackathon) -> str:
    tz = hack_tz(hack)
    return (
        "⚠️ <b>Этапы накладываются</b>\n"
        f"{first.kind.emoji} {esc(first.title)} — {esc(when_full(first, tz))}\n"
        f"{second.kind.emoji} {esc(second.title)} — {esc(when_full(second, tz))}"
    )


def render_missing(gaps: list[str]) -> str:
    if not gaps:
        return "✅ Всё заполнено."
    rows = "\n".join(f"• {esc(g)}" for g in gaps)
    return f"📝 <b>Ещё не хватает</b>\n{rows}"


def render_proposal(hack: Hackathon, proposals: list[ProposedEvent]) -> str:
    """Preview of the auto-generated standard timeline before it is applied."""
    tz = hack_tz(hack)
    lines = ["🧩 <b>Предлагаю стандартный набор этапов</b>", ""]
    for item in proposals:
        lines.append(
            f"{item.kind.emoji} <b>{esc(item.title)}</b> — {esc(fmt_when(item.starts_at, tz))}"
        )
    lines.append("")
    lines.append("<i>Что не подходит — потом поправим одной командой.</i>")
    return "\n".join(lines)
