"""Inline button handling: card navigation and attendance answers."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from hackbot.bot.callbacks import CardCb, EventCb, RsvpCb
from hackbot.bot.cards import refresh_card
from hackbot.bot.handlers._helpers import answer_query
from hackbot.bot.keyboards.common import event_kb
from hackbot.db.base import session_scope
from hackbot.db.models import Hackathon
from hackbot.domain.enums import RsvpStatus
from hackbot.domain.services.docs import list_docs
from hackbot.domain.services.events import detect_conflicts, get_event, list_events
from hackbot.domain.services.ics import build_calendar, feed_url, ics_filename
from hackbot.domain.services.participants import join, list_participants, rsvp_summary, set_rsvp
from hackbot.domain.textutils import esc, truncate
from hackbot.render.card import render_info
from hackbot.render.timeline import render_event, render_rsvp_summary, render_timeline

log = logging.getLogger(__name__)
router = Router(name="queries")


@router.callback_query(RsvpCb.filter())
async def on_rsvp(query: CallbackQuery, callback_data: RsvpCb, bot: Bot) -> None:
    user = query.from_user
    try:
        status = RsvpStatus(callback_data.status)
    except ValueError:
        await answer_query(query, "Неизвестный ответ")
        return

    async with session_scope() as session:
        event = await get_event(session, callback_data.event_id)
        if event is None:
            await answer_query(query, "Этап уже удалён", alert=True)
            return
        hack = await session.get(Hackathon, event.hackathon_id)
        if hack is None:
            await answer_query(query, "Хакатон не найден", alert=True)
            return

        # Answering counts as joining: nobody should have to /join first.
        await join(
            session, hack,
            tg_user_id=user.id, username=user.username, full_name=user.full_name,
        )
        await set_rsvp(session, event, user.id, status)
        summary = await rsvp_summary(session, event)
        title = event.title
        text = render_event(hack, event, summary=summary)
        markup = event_kb(event)
        await refresh_card(bot, session, hack)

    await answer_query(query, f"{status.emoji} {status.label} — записал")
    if query.message is not None:
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception as exc:
            log.debug("could not repaint rsvp message for %s: %s", title, exc)


@router.callback_query(EventCb.filter(F.action == "who"))
async def on_who(query: CallbackQuery, callback_data: EventCb) -> None:
    async with session_scope() as session:
        event = await get_event(session, callback_data.event_id)
        if event is None:
            await answer_query(query, "Этап уже удалён", alert=True)
            return
        summary = await rsvp_summary(session, event)
        text = render_rsvp_summary(summary)
    await answer_query(query)
    if query.message is not None:
        await query.message.reply(f"<b>{esc(event.title)}</b>\n{text}")


@router.callback_query(CardCb.filter())
async def on_card_button(query: CallbackQuery, callback_data: CardCb) -> None:
    action = callback_data.action

    async with session_scope() as session:
        hack = await session.get(Hackathon, callback_data.hack_id)
        if hack is None:
            await answer_query(query, "Хакатон не найден", alert=True)
            return

        if action == "timeline":
            events = await list_events(session, hack.id)
            payload = render_timeline(hack, events, conflicts=detect_conflicts(events))
        elif action == "info":
            events = await list_events(session, hack.id)
            people = await list_participants(session, hack.id)
            payload = render_info(hack, events, people)
        elif action == "team":
            people = await list_participants(session, hack.id)
            if people:
                rows = [
                    ("👑 " if p.is_captain else "• ")
                    + f"<b>{esc(p.display)}</b>"
                    + (f" — {esc(p.role)}" if p.role else "")
                    for p in people
                ]
                payload = "👥 <b>Команда</b>\n\n" + "\n".join(rows)
            else:
                payload = "В команде пока никого. Каждый пишет /join."
        elif action == "links":
            if hack.links:
                rows = [
                    f'{link.kind.emoji} <a href="{esc(link.url)}">'
                    f'{esc(link.title or link.kind.label)}</a>'
                    for link in hack.links
                ]
                payload = "🔗 <b>Ссылки</b>\n\n" + "\n".join(rows)
            else:
                payload = "Ссылок пока нет. Добавь: /link сайт https://…"
        elif action == "docs":
            docs = await list_docs(session, hack.id)
            if docs:
                rows = [f"• <code>{esc(d.file_name)}</code>" for d in docs]
                payload = "📎 <b>Документы</b>\n\n" + "\n".join(rows)
            else:
                payload = "Документов пока нет."
        elif action == "ics":
            events = await list_events(session, hack.id)
            if not events:
                await answer_query(query, "Этапов ещё нет", alert=True)
                return
            calendar = build_calendar(hack, events)
            name = ics_filename(hack)
            subscribe = feed_url(hack)
            await answer_query(query)
            if query.message is not None:
                caption = "📥 Импортируй в календарь."
                if subscribe:
                    caption += f"\n\nПодписка на ленту:\n<code>{esc(subscribe)}</code>"
                await query.message.reply_document(
                    BufferedInputFile(calendar, filename=name), caption=caption
                )
            return
        else:
            await answer_query(query, "Не знаю такую кнопку")
            return

    await answer_query(query)
    if query.message is not None:
        await query.message.reply(truncate(payload, 3900), disable_web_page_preview=True)
