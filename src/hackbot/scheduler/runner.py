"""The scheduler loop.

Deliberately hand-rolled rather than APScheduler-backed. Every scheduled thing
this bot does is already a row in SQLite - reminders, event times, hackathon
status - so a job store would be a second source of truth that has to be kept in
sync, and its pickled callables break the moment a function is renamed. A plain
loop over indexed queries survives restarts for free: anything past its time is
simply due on the next pass.

Timezone handling stays correct because every comparison happens in UTC and only
rendering converts to the hackathon's own zone, so DST never enters the maths.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from hackbot.bot.cards import announce, refresh_card
from hackbot.bot.keyboards.common import rsvp_kb
from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import HackStatus
from hackbot.domain.services import kv
from hackbot.domain.services.events import (
    due_reminders,
    expire_stale_reminders,
    list_events,
)
from hackbot.domain.services.hackathons import derive_status, hack_tz, list_bound
from hackbot.domain.timeutils import day_bounds, now_utc, to_local
from hackbot.render.digest import (
    render_digest,
    render_reminder,
    render_start_announcement,
    render_status_change,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 15
DIGEST_HOUR = 9
BACKUP_KEY = "last_backup_on"
_card_last_painted: dict[int, float] = {}


async def scheduler_loop(bot: Bot, stop: asyncio.Event) -> None:
    """One loop, many small responsibilities. Never lets one failure stop the rest."""
    log.info("scheduler started, tick=%ss", TICK_SECONDS)
    while not stop.is_set():
        for step in (_send_due_reminders, _advance_statuses, _send_digests,
                     _refresh_cards, _daily_backup):
            try:
                await step(bot)
            except TelegramRetryAfter as exc:
                log.warning("flood control in %s: waiting %ss", step.__name__, exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except Exception:
                log.exception("scheduler step %s failed", step.__name__)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    log.info("scheduler stopped")


# ---------------------------------------------------------------- reminders


async def _send_due_reminders(bot: Bot) -> None:
    async with session_scope() as session:
        # Anything that slipped past the grace window is retired unsent: after
        # downtime nobody wants yesterday's countdown dumped into the topic.
        expired = await expire_stale_reminders(session)
        if expired:
            log.info("retired %s stale reminders", expired)
        pending = await due_reminders(session)

    for reminder_id in [r.id for r in pending]:
        async with session_scope() as session:
            from hackbot.db.models import Reminder

            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.sent_at is not None:
                continue
            event = await session.get(Event, reminder.event_id)
            if event is None:
                continue
            hack = await session.get(Hackathon, event.hackathon_id)
            if hack is None or hack.status is HackStatus.ARCHIVED:
                reminder.sent_at = now_utc()
                continue

            text = render_reminder(hack, event, reminder.offset_minutes)
            markup = rsvp_kb(event) if event.needs_rsvp else None
            sent = await announce(bot, hack, text, reply_markup=markup)

            reminder.sent_at = now_utc()
            reminder.message_id = sent.message_id if sent else None
            log.info("reminder -%smin for event %s sent", reminder.offset_minutes, event.id)


# ---------------------------------------------------------------- status


async def _advance_statuses(bot: Bot) -> None:
    async with session_scope() as session:
        hacks = await list_bound(session)
        for hack in hacks:
            events = await list_events(session, hack.id)
            new_status = derive_status(hack, events)
            if new_status == hack.status:
                continue

            old_label = hack.status.label
            hack.status = new_status
            await session.flush()
            log.info("hackathon %s: %s -> %s", hack.id, old_label, new_status.value)

            if new_status is HackStatus.RUNNING:
                # The moment it starts is exactly when people hunt for the links.
                await announce(bot, hack, render_start_announcement(hack, events))
            else:
                await announce(bot, hack, render_status_change(hack, old_label), silent=True)
            await refresh_card(bot, session, hack)


# ---------------------------------------------------------------- digest


async def _send_digests(bot: Bot) -> None:
    async with session_scope() as session:
        hacks = await list_bound(session)
        for hack in hacks:
            if hack.status in {HackStatus.DRAFT, HackStatus.FINISHED}:
                continue
            tz = hack_tz(hack)
            local = to_local(now_utc(), tz)
            today_iso = local.date().isoformat()

            if local.hour < DIGEST_HOUR or hack.last_digest_on == today_iso:
                continue
            # Only worth a digest while the hackathon is actually near.
            if hack.starts_at and local.date() < (
                to_local(hack.starts_at, tz).date() - timedelta(days=3)
            ):
                hack.last_digest_on = today_iso
                continue

            events = await list_events(session, hack.id)
            start, end = day_bounds(local.date(), tz)
            today = [e for e in events if start <= e.starts_at < end]

            text = render_digest(hack, today, events)
            hack.last_digest_on = today_iso
            await session.flush()
            if text:
                await announce(bot, hack, text, silent=True)
                log.info("digest sent for hackathon %s", hack.id)


# ---------------------------------------------------------------- card


async def _refresh_cards(bot: Bot) -> None:
    settings = get_settings()
    interval = max(30, settings.card_refresh_seconds)
    loop_now = asyncio.get_running_loop().time()

    async with session_scope() as session:
        hacks = await list_bound(session)
        for hack in hacks:
            if hack.status in {HackStatus.DRAFT, HackStatus.FINISHED}:
                continue
            last = _card_last_painted.get(hack.id, 0.0)
            if loop_now - last < interval:
                continue
            _card_last_painted[hack.id] = loop_now
            await refresh_card(bot, session, hack)


# ---------------------------------------------------------------- backup


async def _daily_backup(_bot: Bot) -> None:
    """A copy of the SQLite file once a day. Small, boring, saves weekends."""
    settings = get_settings()
    today = to_local(now_utc(), settings.default_tz).date().isoformat()

    async with session_scope() as session:
        if await kv.get(session, BACKUP_KEY) == today:
            return
        await kv.set_value(session, BACKUP_KEY, today)

    source = settings.abs_db_path
    if not source.exists():
        return
    backups = source.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"{source.stem}-{today}{source.suffix}"

    await asyncio.to_thread(shutil.copy2, source, target)
    log.info("database backed up to %s", target)

    # Keep a fortnight; older copies are noise.
    stale = sorted(backups.glob(f"{source.stem}-*{source.suffix}"))[:-14]
    for path in stale:
        with contextlib.suppress(OSError):
            path.unlink()
