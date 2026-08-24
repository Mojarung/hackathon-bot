"""The pinned live card - the bot's main surface in a topic.

It is edited in place rather than re-sent, so the topic stays clean. Every line
is optional: a half-filled hackathon renders a shorter, still-tidy card.
"""

from __future__ import annotations

from datetime import datetime

from hackbot.db.models import Event, Hackathon, Participant
from hackbot.domain.enums import HackStatus
from hackbot.domain.services.hackathons import (
    current_event,
    hack_tz,
    next_event,
    primary_deadline,
    progress_ratio,
)
from hackbot.domain.textutils import esc, truncate
from hackbot.domain.timeutils import fmt_dt, humanize_delta, now_utc
from hackbot.render.components import (
    RULE,
    countdown_block,
    date_span,
    event_meta,
    footer_updated,
    progress_block,
    title_line,
)


def render_card(
    hack: Hackathon,
    events: list[Event],
    participants: list[Participant],
    *,
    rsvp_going: int | None = None,
    rsvp_event: Event | None = None,
    now: datetime | None = None,
) -> str:
    now = now or now_utc()
    tz = hack_tz(hack)
    status = hack.status

    lines: list[str] = [f"{status.emoji} <b>{title_line(hack)}</b>"]

    span = date_span(hack, tz)
    if span:
        lines.append(f"<i>{esc(span)}</i>")

    lines.append(RULE)

    # status + progress
    ratio = progress_ratio(hack, now)
    status_row = f"<b>{esc(status.label)}</b>"
    if hack.is_online:
        status_row += "  ·  онлайн"
    lines.append(status_row)

    bar = progress_block(ratio, hack, tz, now)
    if bar and status in {HackStatus.RUNNING, HackStatus.JUDGING}:
        lines.append(bar)

    # the headline countdown
    target = primary_deadline(hack, events)
    counter = countdown_block(target, hack, now)
    if counter:
        lines.append(counter)

    # what is happening right now, or what comes next
    live = current_event(events, now)
    upcoming = next_event(events, now)
    if live is not None:
        lines.append("")
        lines.append(f"▶️ <b>сейчас:</b> {live.kind.emoji} {esc(live.title)}")
        meta = event_meta(live)
        if meta:
            lines.append(f"     {meta}")
    if upcoming is not None:
        lines.append("")
        left = humanize_delta((upcoming.starts_at - now).total_seconds())
        lines.append(f"📍 <b>дальше:</b> {upcoming.kind.emoji} {esc(upcoming.title)}")
        lines.append(f"     {esc(fmt_dt(upcoming.starts_at, tz))}  ·  через {left}")
        meta = event_meta(upcoming)
        if meta:
            lines.append(f"     {meta}")
    elif not events:
        lines.append("")
        lines.append("<i>этапы ещё не заданы — /add или просто напиши боту</i>")

    # team
    if participants:
        team_row = f"👥 {len(participants)} в команде"
        if rsvp_going is not None and rsvp_event is not None:
            team_row += (
                f"  ·  ✅ {rsvp_going} из {len(participants)}"
                f" на «{esc(truncate(rsvp_event.title.lower(), 24))}»"
            )
        lines.append("")
        lines.append(team_row)

    # results, once there are any
    if hack.result_place:
        lines.append("")
        lines.append(f"🏆 <b>{esc(hack.result_place)}</b>")
        if hack.result_note:
            lines.append(f"<i>{esc(truncate(hack.result_note, 200))}</i>")

    lines.append(RULE)
    lines.append(footer_updated(now, tz))
    return "\n".join(lines)


def render_info(
    hack: Hackathon,
    events: list[Event],
    participants: list[Participant],
    *,
    now: datetime | None = None,
) -> str:
    """A fuller, non-pinned view for `/info` - everything the card omits."""
    tz = hack_tz(hack)
    now = now or now_utc()
    lines: list[str] = [f"{hack.status.emoji} <b>{title_line(hack)}</b>", ""]

    def row(label: str, value: str | None) -> None:
        if value:
            lines.append(f"<b>{label}:</b> {value}")

    row("Статус", esc(hack.status.label))
    row("Организатор", esc(hack.organizer))
    row("Формат", "онлайн" if hack.is_online else (esc(hack.city) if hack.city else None))
    row("Начало", esc(fmt_dt(hack.starts_at, tz, with_year=True)) if hack.starts_at else None)
    row("Конец", esc(fmt_dt(hack.ends_at, tz, with_year=True)) if hack.ends_at else None)
    row(
        "Регистрация до",
        esc(fmt_dt(hack.reg_deadline, tz, with_year=True)) if hack.reg_deadline else None,
    )
    row("Часовой пояс", esc(hack.tz))
    row("Этапов", str(len(events)) if events else None)
    row("В команде", str(len(participants)) if participants else None)
    if hack.github_url:
        repo_label = esc(hack.github_repo or "GitHub")
        row("Репозиторий", f'<a href="{esc(hack.github_url)}">{repo_label}</a>')
    if hack.result_place:
        row("Результат", f"🏆 {esc(hack.result_place)}")

    if hack.description:
        lines.append("")
        lines.append(f"<blockquote expandable>{esc(hack.description)}</blockquote>")

    if hack.links:
        lines.append("")
        lines.append("<b>Ссылки</b>")
        for link in hack.links:
            label = esc(link.title or link.kind.label)
            lines.append(f'{link.kind.emoji} <a href="{esc(link.url)}">{label}</a>')

    target = primary_deadline(hack, events)
    if target and target.starts_at > now:
        lines.append("")
        left = humanize_delta((target.starts_at - now).total_seconds(), parts=3)
        lines.append(f"⏳ До «{esc(target.title.lower())}» — <b>{left}</b>")

    return "\n".join(lines)
