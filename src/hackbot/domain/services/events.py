"""Timeline events and the reminders derived from them.

`fire_at` on a reminder is denormalised from its event, so whenever an event
moves the reminder set is rebuilt. Already-sent reminders are preserved, so
nothing gets re-delivered after a reschedule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.db.models import Event, Hackathon, Reminder
from hackbot.domain.enums import EventKind
from hackbot.domain.timeutils import now_utc

EDITABLE_FIELDS = frozenset(
    {"kind", "title", "starts_at", "ends_at", "place", "url", "notes", "is_mandatory", "needs_rsvp"}
)

# Minutes before an event at which to ping: 3 дня, сутки, 3 часа, час, 15 минут.
# Steps already in the past when the event is created simply never fire, so a
# stage added two hours ahead only gets the last two rungs of the ladder.
STANDARD_LADDER: tuple[int, ...] = (3 * 1440, 1440, 180, 60, 15)

# The submission deadline earns one extra nudge; missing it costs the whole run.
DEFAULT_OFFSETS: dict[EventKind, tuple[int, ...]] = {
    EventKind.SUBMISSION: (*STANDARD_LADDER, 30),
}


def default_offsets(kind: EventKind) -> tuple[int, ...]:
    return DEFAULT_OFFSETS.get(kind, STANDARD_LADDER)


# A full programme lists meals and open work blocks. They belong on the timeline
# but not in anyone's notifications: a printed schedule of 19 rows would
# otherwise turn into ~95 pings, several of them announcing dinner three days out.
_FILLER_RE = re.compile(
    r"^\s*(завтрак|обед|ужин|кофе|перерыв|перекус|работа\s+над\s+проект|свободн"
    r"|заселени|нетворкинг|фуршет|отдых|сон\b)",
    re.IGNORECASE,
)


def is_filler(title: str) -> bool:
    """Scheduled, but not something anyone needs to be reminded about."""
    return bool(_FILLER_RE.match(title or ""))


async def list_events(session: AsyncSession, hackathon_id: int) -> list[Event]:
    stmt = select(Event).where(Event.hackathon_id == hackathon_id).order_by(Event.starts_at)
    return list(await session.scalars(stmt))


async def get_event(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def add_event(
    session: AsyncSession,
    hack: Hackathon,
    *,
    title: str,
    starts_at: datetime,
    kind: EventKind | None = None,
    ends_at: datetime | None = None,
    place: str | None = None,
    url: str | None = None,
    notes: str | None = None,
    is_mandatory: bool | None = None,
    needs_rsvp: bool | None = None,
    offsets: tuple[int, ...] | None = None,
) -> Event:
    kind = kind or EventKind.guess(title)
    event = Event(
        hackathon_id=hack.id,
        kind=kind,
        title=title.strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        place=place,
        url=url,
        notes=notes,
        is_mandatory=kind.is_critical if is_mandatory is None else is_mandatory,
        needs_rsvp=kind.is_critical if needs_rsvp is None else needs_rsvp,
    )
    session.add(event)
    await session.flush()
    await sync_reminders(session, event, offsets)
    return event


async def update_event(
    session: AsyncSession, event: Event, fields: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    changed: dict[str, tuple[Any, Any]] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        old = getattr(event, key, None)
        if old == value:
            continue
        setattr(event, key, value)
        changed[key] = (old, value)
    if changed:
        await session.flush()
        if "starts_at" in changed:
            await sync_reminders(session, event)
    return changed


async def delete_event(session: AsyncSession, event: Event) -> None:
    await session.delete(event)
    await session.flush()


# ---------------------------------------------------------------- reminders


async def sync_reminders(
    session: AsyncSession, event: Event, offsets: tuple[int, ...] | None = None
) -> list[Reminder]:
    """Rebuild the pending reminder set for an event.

    Sent reminders are left untouched: they are history, and re-creating them
    would fire a duplicate ping the moment the scheduler next wakes up.
    """
    if offsets is not None:
        wanted = set(offsets)          # explicit request always wins
    elif is_filler(event.title):
        wanted = set()                 # on the timeline, silent in notifications
    else:
        wanted = set(default_offsets(event.kind))

    stmt = select(Reminder).where(Reminder.event_id == event.id)
    existing = list(await session.scalars(stmt))

    kept: list[Reminder] = []
    for rem in existing:
        if rem.sent_at is not None:
            kept.append(rem)
            wanted.discard(rem.offset_minutes)
            continue
        if rem.offset_minutes in wanted:
            rem.fire_at = event.starts_at - timedelta(minutes=rem.offset_minutes)
            kept.append(rem)
            wanted.discard(rem.offset_minutes)
        else:
            await session.delete(rem)

    for offset in sorted(wanted, reverse=True):
        rem = Reminder(
            event_id=event.id,
            offset_minutes=offset,
            fire_at=event.starts_at - timedelta(minutes=offset),
        )
        session.add(rem)
        kept.append(rem)

    await session.flush()
    return kept


async def resync_future_reminders(session: AsyncSession, now: datetime | None = None) -> int:
    """Rebuild reminders for everything still ahead.

    Run at startup so a change to the offset ladder reaches events that were
    created under the old one, without touching anything already delivered.
    """
    now = now or now_utc()
    stmt = select(Event).where(Event.starts_at > now)
    events = list(await session.scalars(stmt))
    for event in events:
        await sync_reminders(session, event)
    return len(events)


async def due_reminders(
    session: AsyncSession, now: datetime | None = None, *, grace_minutes: int = 90
) -> list[Reminder]:
    """Unsent reminders whose moment has arrived.

    Anything older than the grace window is skipped rather than delivered, so a
    restart after downtime does not dump a wall of stale pings into the topic.
    """
    now = now or now_utc()
    floor = now - timedelta(minutes=grace_minutes)
    stmt = (
        select(Reminder)
        .where(Reminder.sent_at.is_(None), Reminder.fire_at <= now, Reminder.fire_at >= floor)
        .order_by(Reminder.fire_at)
    )
    return list(await session.scalars(stmt))


async def expire_stale_reminders(session: AsyncSession, now: datetime | None = None,
                                 *, grace_minutes: int = 90) -> int:
    """Mark long-overdue reminders as handled so they never fire late."""
    now = now or now_utc()
    floor = now - timedelta(minutes=grace_minutes)
    stmt = select(Reminder).where(Reminder.sent_at.is_(None), Reminder.fire_at < floor)
    rows = list(await session.scalars(stmt))
    for row in rows:
        row.sent_at = now
    if rows:
        await session.flush()
    return len(rows)


async def ensure_deadline_events(session: AsyncSession, hack: Hackathon) -> list[Event]:
    """Mirror the hackathon's own dates into real events.

    `reg_deadline` and `starts_at` live on the hackathon for display, but only
    events carry reminders - so missing the registration cut-off would otherwise
    be silent. Both are kept in sync here rather than duplicated by every caller.
    """
    existing = await list_events(session, hack.id)
    by_kind = {e.kind: e for e in existing}
    touched: list[Event] = []

    wanted: list[tuple[EventKind, str, datetime | None]] = [
        (EventKind.REGISTRATION, "Дедлайн регистрации", hack.reg_deadline),
        (EventKind.START, "Старт хакатона", hack.starts_at),
    ]

    for kind, title, moment in wanted:
        if moment is None:
            continue
        current = by_kind.get(kind)
        if current is None:
            touched.append(
                await add_event(session, hack, title=title, starts_at=moment, kind=kind)
            )
        elif current.starts_at != moment:
            await update_event(session, current, {"starts_at": moment})
            touched.append(current)

    return touched


# ---------------------------------------------------------------- analysis


@dataclass(frozen=True, slots=True)
class Conflict:
    first: Event
    second: Event


def detect_conflicts(events: list[Event]) -> list[Conflict]:
    """Overlapping events, considered only when both declare an end time."""
    out: list[Conflict] = []
    ordered = sorted(events, key=lambda e: e.starts_at)
    for i, a in enumerate(ordered):
        if not a.ends_at:
            continue
        for b in ordered[i + 1:]:
            if b.starts_at >= a.ends_at:
                break
            if b.ends_at:
                out.append(Conflict(a, b))
    return out


def events_on_day(events: list[Event], start: datetime, end: datetime) -> list[Event]:
    return [e for e in events if start <= e.starts_at < end]


# ---------------------------------------------------------------- templates


@dataclass(frozen=True, slots=True)
class ProposedEvent:
    kind: EventKind
    title: str
    starts_at: datetime
    ends_at: datetime | None = None


def propose_timeline(hack: Hackathon, existing: list[Event]) -> list[ProposedEvent]:
    """Suggest the stages a hackathon almost always has, skipping any already set.

    Anchored on the declared start and end, so the user only has to confirm.
    """
    if not hack.starts_at or not hack.ends_at:
        return []

    have = {e.kind for e in existing}
    start, end = hack.starts_at, hack.ends_at
    span = end - start
    out: list[ProposedEvent] = []

    def add(kind: EventKind, title: str, at: datetime, until: datetime | None = None) -> None:
        if kind not in have and at > now_utc() - timedelta(days=1):
            out.append(ProposedEvent(kind, title, at, until))

    if hack.reg_deadline:
        add(EventKind.REGISTRATION, "Дедлайн регистрации", hack.reg_deadline)
    add(EventKind.START, "Открытие и старт", start)
    if span > timedelta(hours=24):
        add(EventKind.CHECKPOINT, "Чек-поинт", start + span / 2)
    add(EventKind.CODE_FREEZE, "Код-фриз", end - timedelta(hours=2))
    add(EventKind.SUBMISSION, "Сдача решения", end)
    add(EventKind.DEFENSE, "Защита", end + timedelta(hours=2), end + timedelta(hours=5))
    add(EventKind.RESULTS, "Объявление результатов", end + timedelta(hours=6))
    return out
