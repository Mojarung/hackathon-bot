"""Ownership of the pinned live card.

The card is a single message that gets edited, never re-sent, so the topic does
not fill up with countdown spam. If it goes missing (deleted by a human, or too
old to edit) a fresh one is posted and pinned.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.bot.keyboards.common import card_kb
from hackbot.bot.utils import edit_html, send_html, try_pin
from hackbot.db.models import Doc, Event, Hackathon
from hackbot.domain.services.events import list_events
from hackbot.domain.services.hackathons import next_event
from hackbot.domain.services.participants import list_participants, rsvp_summary
from hackbot.render.card import render_card

log = logging.getLogger(__name__)


async def _card_payload(
    session: AsyncSession, hack: Hackathon
) -> tuple[str, InlineKeyboardMarkup]:
    events = await list_events(session, hack.id)
    people = await list_participants(session, hack.id)

    rsvp_going: int | None = None
    rsvp_event: Event | None = None
    upcoming = next_event([e for e in events if e.needs_rsvp])
    if upcoming is not None and people:
        summary = await rsvp_summary(session, upcoming)
        rsvp_going, rsvp_event = summary.going, upcoming

    doc_count = await session.scalar(
        select(func.count()).select_from(Doc).where(Doc.hackathon_id == hack.id)
    )

    text = render_card(hack, events, people, rsvp_going=rsvp_going, rsvp_event=rsvp_event)
    return text, card_kb(hack, has_docs=bool(doc_count))


async def refresh_card(
    bot: Bot, session: AsyncSession, hack: Hackathon, *, force_new: bool = False
) -> Message | None:
    """Update the pinned card, recreating it when the old message is gone."""
    text, markup = await _card_payload(session, hack)

    if hack.card_message_id and not force_new:
        ok = await edit_html(bot, hack.chat_id, hack.card_message_id, text, reply_markup=markup)
        if ok:
            return None
        log.info("card %s vanished in chat %s, posting a new one",
                 hack.card_message_id, hack.chat_id)

    sent = await send_html(
        bot, hack.chat_id, text, thread_id=hack.thread_id,
        reply_markup=markup, disable_notification=True,
    )
    if sent is None:
        return None

    hack.card_message_id = sent.message_id
    await session.flush()
    await try_pin(bot, hack.chat_id, sent.message_id)
    return sent


async def announce(
    bot: Bot,
    hack: Hackathon,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    silent: bool = False,
) -> Message | None:
    """Post into the hackathon's own topic."""
    return await send_html(
        bot, hack.chat_id, text, thread_id=hack.thread_id,
        reply_markup=reply_markup, disable_notification=silent,
    )
