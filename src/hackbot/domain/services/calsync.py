"""Mirroring hackathon timelines into one shared Google Calendar.

Every hackathon lands in the same calendar - the one the human created and
shared with the service account - so the hackathon has to be visible in the
event title itself. Three hackathons at once would otherwise produce an
indistinguishable pile of "Сдача решения" rows.

Calendar ids are derived from the database ids rather than stored next to them:
the project has no migrations, so a new column would never reach the live
database. Deriving them also makes a sync idempotent - a second pass updates the
same rows instead of duplicating them - and survives a database restored from
backup, which keeps pointing at the same calendar entries.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.config import get_settings
from hackbot.db.models import Event, Hackathon

# events is imported as a module rather than by symbol on purpose: the mutations
# in events.py are the ones that call mark_dirty(), and a symbol import would
# turn that cycle into an ImportError at startup.
from hackbot.domain.services import events as event_service
from hackbot.domain.services import gcal, kv
from hackbot.domain.services.hackathons import hack_tz, list_hackathons
from hackbot.domain.timeutils import as_utc, now_utc

log = logging.getLogger(__name__)

FULL_SYNC_KEY = "gcal_last_full_sync"

_DEFAULT_DURATION = timedelta(hours=1)

# Google lets the caller pick an event id, but only in base32hex: 5 to 1024
# characters out of [a-v0-9]. That alphabet holds 32 symbols, so w, x, y and z
# do not exist in it at all and any id carrying one comes back as a flat 400.
# Hence the hb/e separators below - h, b and e are all inside the alphabet.
_GID_RE = re.compile(r"[a-v0-9]{5,1024}")

# Hackathons whose timeline changed since the last tick. In memory on purpose;
# sync_due() explains why losing this set on restart costs nothing.
_dirty: set[int] = set()
_DIRTY_CAP = 500


# ---------------------------------------------------------------- event body


def gid(hack_id: int, event_id: int) -> str:
    """The calendar id of one timeline event, derived and never stored."""
    value = f"hb{hack_id}e{event_id}"
    if not _GID_RE.fullmatch(value):
        raise ValueError(f"not a valid base32hex calendar id: {value!r}")
    return value


def build_body(hack: Hackathon, event: Event) -> dict[str, Any]:
    """The Calendar API resource for one stage of one hackathon."""
    tz = hack_tz(hack)
    start = as_utc(event.starts_at).astimezone(tz)
    end = as_utc(event.ends_at).astimezone(tz) if event.ends_at else start + _DEFAULT_DURATION
    if end <= start:
        # Google refuses a non-positive span. A stale ends_at is treated as if it
        # were missing rather than allowed to fail the event outright.
        end = start + _DEFAULT_DURATION

    body: dict[str, Any] = {
        "id": gid(hack.id, event.id),
        "summary": f"{event.kind.emoji} {event.title} · {hack.title}",
        # A deleted event lingers as a cancelled tombstone for a while, and an
        # update that stays silent about status inherits it. Saying "confirmed"
        # out loud is what brings the entry back to life.
        "status": "confirmed",
        "start": _moment(start, tz),
        "end": _moment(end, tz),
        "description": _description(hack, event),
        "reminders": _reminders(event),
        "extendedProperties": {
            "private": {"hackbot_hack": str(hack.id), "hackbot_event": str(event.id)},
        },
    }
    if event.place:
        body["location"] = event.place
    return body


def _moment(when: datetime, tz: ZoneInfo) -> dict[str, str]:
    # str(ZoneInfo) hands back the IANA key. hack.tz itself may be nonsense that
    # hack_tz() already fell back from, and Google would reject the nonsense.
    return {"dateTime": when.isoformat(), "timeZone": str(tz)}


def _reminders(event: Event) -> dict[str, Any]:
    """An hour of warning for the stages that cost the whole run if missed."""
    if not event.kind.is_critical:
        return {"useDefault": True}
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]}


def _description(hack: Hackathon, event: Event) -> str:
    parts = [f"{hack.title} {hack.year}"]
    if event.notes:
        parts.append(event.notes)
    if event.is_mandatory:
        parts.append("Обязательное участие")
    if event.url:
        parts.append(f"Ссылка: {event.url}")
    page = get_settings().public_url(f"h/{hack.slug}")
    if page:
        parts.append(f"Страница хакатона: {page}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- pushing


async def push_hackathon(session: AsyncSession, hack: Hackathon) -> int:
    """Write every stage of one hackathon, then clear out what no longer exists.

    One refused event must not cost the other twenty, so failures are counted
    and reported rather than raised.
    """
    if not gcal.enabled():
        return 0

    rows = await event_service.list_events(session, hack.id)
    alive: set[str] = set()
    written = 0
    failed = 0

    for row in rows:
        event_id = gid(hack.id, row.id)
        alive.add(event_id)
        try:
            await gcal.upsert_event(event_id, build_body(hack, row))
        except gcal.GCalError as exc:
            failed += 1
            log.warning(
                "calendar: stage %s of hackathon %s refused: %s", row.id, hack.id, exc.message
            )
        else:
            written += 1

    if failed and not written:
        # Not one write got through: the calendar is unreachable or misconfigured,
        # and the orphan pass would only repeat the same pile of errors.
        log.warning("calendar: hackathon %s wrote nothing, %s stages failed", hack.id, failed)
        return 0

    await _drop_orphans(session, hack.id, alive)
    return written


async def _drop_orphans(session: AsyncSession, hack_id: int, alive: set[str]) -> None:
    """Stages deleted from the database have to leave the calendar too.

    Google is asked what it holds instead of the database remembering it: there
    is nowhere to remember it, and Google is the only side that knows.
    """
    try:
        remote = await gcal.list_event_ids(hack_id)
    except gcal.GCalError as exc:
        log.warning("calendar: cannot list hackathon %s: %s", hack_id, exc.message)
        return

    stale = remote - alive
    if not stale:
        return

    # `alive` was fixed before the upserts, a few seconds and a few round trips
    # ago. /gcal, the agent's tool and the scheduler tick share one event loop, so
    # a concurrent /add can have created a stage in Google inside that window -
    # and removing it as an orphan would destroy a real one. Confirm against the
    # timeline as it stands now, and only when there is actually something to drop.
    fresh = {gid(hack_id, row.id) for row in await event_service.list_events(session, hack_id)}
    for event_id in stale - fresh:
        try:
            await gcal.delete_event(event_id)
        except gcal.GCalError as exc:
            log.warning("calendar: cannot delete %s: %s", event_id, exc.message)


async def push_all(session: AsyncSession) -> int:
    """Every hackathon still in play.

    Finished ones are left alone: what sits in the calendar for them is a record
    of dates that already passed, not stale data worth reconciling.
    """
    if not gcal.enabled():
        return 0

    total = 0
    for hack in await list_hackathons(session, live_only=True):
        total += await push_hackathon(session, hack)
    return total


# ---------------------------------------------------------------- scheduling


def mark_dirty(hack_id: int) -> None:
    """Note that a timeline changed; the scheduler flushes it on the next tick.

    Synchronous and cheap on purpose - it is called from the middle of an event
    mutation, which must not wait on the network or care whether the integration
    is configured at all.
    """
    if not gcal.enabled() or hack_id in _dirty:
        return
    if len(_dirty) >= _DIRTY_CAP:
        # Bounded, so a runaway caller cannot grow it without limit. Nothing is
        # lost either: whatever is refused here rides the next full pass.
        log.warning("calendar: dirty set full, hackathon %s waits for the full pass", hack_id)
        return
    _dirty.add(hack_id)


async def sync_due(session: AsyncSession) -> int:
    """One scheduler tick worth of calendar work.

    Two halves, and both earn their keep. The dirty set is the fast half: an edit
    reaches the calendar within a tick, but it lives in memory, so a restart or a
    crash forgets it. The full pass is the slow half: it costs a listing per
    hackathon and runs once every google_calendar_sync_minutes, which is what
    eventually reconciles whatever the fast half dropped - a forgotten set, a
    write that failed while the calendar was unreachable, an edit made by a
    previous process. One half is latency, the other is the safety net; neither
    is a duplicate of the other.
    """
    if not gcal.enabled():
        _dirty.clear()
        return 0

    # Drained first, and separately from the sweep. push_all walks only live
    # hackathons, so a stage edited on a finished one would be discarded together
    # with the set and never reconciled by a later pass either. The overlap - a
    # dirty live hackathon written twice on a sweep tick - is idempotent and
    # happens at most once every google_calendar_sync_minutes.
    written = await _drain_dirty(session)
    if await _claim_full_pass(session):
        written += await push_all(session)
    return written


async def _drain_dirty(session: AsyncSession) -> int:
    """Push every hackathon marked since the last tick.

    Cleared before the pushes rather than after: an edit landing mid-pass marks
    its hackathon dirty again and is picked up on the next tick, instead of being
    wiped out together with the batch that was already handled.
    """
    pending = sorted(_dirty)
    _dirty.clear()

    written = 0
    for hack_id in pending:
        hack = await session.get(Hackathon, hack_id)
        if hack is None:
            continue
        written += await push_hackathon(session, hack)
    return written


async def _claim_full_pass(session: AsyncSession) -> bool:
    """True at most once per google_calendar_sync_minutes, restarts included.

    The stamp is written and committed before the pass runs, the way the daily
    backup does it: a pass that fails must not be retried every fifteen seconds,
    and SQLite must not sit on a write lock for the length of a few hundred HTTP
    round trips.
    """
    every = timedelta(minutes=max(1, get_settings().google_calendar_sync_minutes))
    last = _read_stamp(await kv.get(session, FULL_SYNC_KEY))
    now = now_utc()
    if last is not None and now - last < every:
        return False

    await kv.set_value(session, FULL_SYNC_KEY, now.isoformat())
    await session.commit()
    return True


def _read_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        # A corrupted stamp should force a pass, never wedge the sync forever.
        log.warning("calendar: unreadable %s stamp %r, forcing a full pass", FULL_SYNC_KEY, raw)
        return None
