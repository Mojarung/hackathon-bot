"""Full timeline view and the per-event detail card."""

from __future__ import annotations

from datetime import datetime

from hackbot.db.models import Event, Hackathon
from hackbot.domain.services.events import Conflict
from hackbot.domain.services.hackathons import hack_tz, primary_deadline
from hackbot.domain.services.participants import RsvpSummary
from hackbot.domain.textutils import esc, truncate
from hackbot.domain.timeutils import (
    MONTHS_GEN,
    WEEKDAYS_SHORT,
    fmt_time,
    humanize_delta,
    now_utc,
    to_local,
)
from hackbot.render.components import (
    RULE,
    THIN_RULE,
    date_span,
    deadline_badge,
    event_meta,
    timeline_marker,
    title_line,
    when_full,
)


def render_timeline(
    hack: Hackathon,
    events: list[Event],
    *,
    conflicts: list[Conflict] | None = None,
    now: datetime | None = None,
) -> str:
    now = now or now_utc()
    tz = hack_tz(hack)
    primary = primary_deadline(hack, events)

    lines: list[str] = [f"📅 <b>Таймлайн — {title_line(hack)}</b>"]
    span = date_span(hack, tz)
    if span:
        lines.append(f"<i>{esc(span)}</i>")
    lines.append(RULE)

    if not events:
        lines.append("")
        lines.append("Пока ни одного этапа.")
        lines.append("")
        lines.append("Добавь командой <code>/add Сдача решения 22.09 18:00</code>")
        lines.append("или просто напиши боту, что известно — он разберётся.")
        return "\n".join(lines)

    current_day: str | None = None
    for event in sorted(events, key=lambda e: e.starts_at):
        local = to_local(event.starts_at, tz)
        day_key = local.date().isoformat()
        if day_key != current_day:
            current_day = day_key
            header = (
                f"{WEEKDAYS_SHORT[local.weekday()]}, "
                f"{local.day} {MONTHS_GEN[local.month - 1]}"
            )
            lines.append("")
            lines.append(f"<b>{esc(header)}</b>")
            lines.append(THIN_RULE)

        marker = timeline_marker(event, now)
        title = esc(event.title)
        if marker == "✅":
            title = f"<s>{title}</s>"
        elif marker == "▶️" or (primary and event.id == primary.id):
            title = f"<b>{title}</b>"

        lines.append(f"{marker} {event.kind.emoji} {fmt_time(event.starts_at, tz)} — {title}"
                     f"{deadline_badge(event, primary)}")

        detail: list[str] = []
        if event.ends_at:
            detail.append(f"до {fmt_time(event.ends_at, tz)}")
        if event.starts_at > now:
            detail.append(f"через {humanize_delta((event.starts_at - now).total_seconds())}")
        meta = event_meta(event)
        if meta:
            detail.append(meta)
        if detail:
            lines.append(f"       <i>{' · '.join(detail)}</i>")

    if conflicts:
        lines.append("")
        lines.append("⚠️ <b>Накладки</b>")
        for conflict in conflicts:
            lines.append(
                f"• {esc(truncate(conflict.first.title, 30))} ↔ "
                f"{esc(truncate(conflict.second.title, 30))}"
            )

    return "\n".join(lines)


def render_event(
    hack: Hackathon,
    event: Event,
    *,
    summary: RsvpSummary | None = None,
    now: datetime | None = None,
) -> str:
    now = now or now_utc()
    tz = hack_tz(hack)

    lines = [f"{event.kind.emoji} <b>{esc(event.title)}</b>", f"<i>{esc(event.kind.label)}</i>", ""]
    lines.append(f"🕐 {esc(when_full(event, tz))}")

    if event.starts_at > now:
        lines.append(f"⏳ через {humanize_delta((event.starts_at - now).total_seconds(), parts=3)}")
    elif event.ends_at and now < event.ends_at:
        lines.append("▶️ <b>идёт прямо сейчас</b>")
    else:
        lines.append("✅ прошло")

    if event.place:
        lines.append(f"📍 {esc(event.place)}")
    if event.url:
        lines.append(f'🔗 <a href="{esc(event.url)}">открыть</a>')
    if event.is_mandatory:
        lines.append("❗ обязательное")
    if event.notes:
        lines.append("")
        lines.append(f"<blockquote expandable>{esc(event.notes)}</blockquote>")

    if summary is not None and (summary.answered or summary.pending):
        lines.append("")
        lines.append(render_rsvp_summary(summary))

    return "\n".join(lines)


def render_rsvp_summary(summary: RsvpSummary) -> str:
    """Who confirmed, grouped by answer, with the silent ones listed last."""
    rows: list[str] = []
    for status, people in summary.by_status.items():
        names = ", ".join(esc(p.display) for p in people)
        rows.append(f"{status.emoji} <b>{esc(status.label)}</b> ({len(people)}): {names}")
    if summary.pending:
        names = ", ".join(esc(p.display) for p in summary.pending)
        rows.append(f"⏸ <b>не ответили</b> ({len(summary.pending)}): {names}")
    return "\n".join(rows) if rows else "<i>пока никто не ответил</i>"
