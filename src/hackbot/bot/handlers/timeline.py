"""Timeline commands: /timeline, /add, /move, /rm, /who."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hackbot.bot.handlers._helpers import note_change, require_editor, require_hack
from hackbot.bot.keyboards.common import event_kb
from hackbot.db.base import session_scope
from hackbot.domain.enums import EventKind
from hackbot.domain.services.events import (
    add_event,
    delete_event,
    detect_conflicts,
    get_event,
    list_events,
    update_event,
)
from hackbot.domain.services.hackathons import hack_tz
from hackbot.domain.services.participants import rsvp_summary
from hackbot.domain.textutils import esc
from hackbot.domain.timeutils import WEEKDAYS_SHORT, fmt_dt, parse_dt
from hackbot.render.timeline import render_event, render_timeline

router = Router(name="timeline")

ADD_HELP = (
    "<b>Добавить этап</b>\n"
    "<code>/add Защита 22.09 20:00</code>\n"
    "<code>/add Код-фриз завтра 18:00</code>\n"
    "<code>/add Чек-поинт пт 12:00</code>\n\n"
    "Тип этапа определяю по названию — «защита», «сдача», «код-фриз» и так далее.\n"
    "Место и ссылку можно дописать через <code>|</code>:\n"
    "<code>/add Защита 22.09 20:00 | Главный зал | https://meet…</code>"
)


@router.message(Command("timeline", "tl", "таймлайн", "этапы"))
async def cmd_timeline(message: Message) -> None:
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        events = await list_events(session, hack.id)
        text = render_timeline(hack, events, conflicts=detect_conflicts(events))
        if events:
            ids = "  ".join(f"<code>{e.id}</code>" for e in events)
            text += f"\n\n<i>id этапов для /move и /rm:</i> {ids}"
    await message.reply(text, disable_web_page_preview=True)


@router.message(Command("add", "добавить"))
async def cmd_add(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return
    args = (command.args or "").strip()
    if not args:
        await message.reply(ADD_HELP)
        return

    head, _, extra = args.partition("|")
    place, _, url = extra.partition("|")

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        tz = hack_tz(hack)

        title, moment = _split_title_and_date(head.strip(), tz)
        if moment is None:
            await message.reply(
                "Не нашёл дату в этой строке.\n\n" + ADD_HELP
            )
            return
        if not title:
            title = EventKind.guess(head).label.capitalize()

        event = await add_event(
            session,
            hack,
            title=title,
            starts_at=moment,
            place=place.strip() or None,
            url=url.strip() or None,
        )
        detail = (
            f"{event.kind.emoji} <b>{esc(event.title)}</b> — "
            f"{esc(fmt_dt(event.starts_at, tz))} <code>#{event.id}</code>"
        )
        await note_change(
            session, bot, hack, message,
            action="добавил этап", detail=detail,
            audit_action="event_add",
            payload={"id": event.id, "title": event.title},
        )


@router.message(Command("move", "перенести"))
async def cmd_move(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return
    args = (command.args or "").strip()
    id_part, _, when_part = args.partition(" ")
    if not id_part.isdigit() or not when_part.strip():
        await message.reply(
            "<code>/move 12 19:30</code> — id этапа берётся из /timeline"
        )
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        event = await get_event(session, int(id_part))
        if event is None or event.hackathon_id != hack.id:
            await message.reply("Нет такого id. Смотри /timeline")
            return

        tz = hack_tz(hack)
        parsed = parse_dt(when_part, tz)
        if parsed is None:
            await message.reply("Не разобрал дату. Например: <code>/move 12 22.09 19:30</code>")
            return

        old = fmt_dt(event.starts_at, tz)
        # A bare time keeps the original day - the common case is "same day, later".
        target = parsed.dt
        if not parsed.has_time:
            target = target.replace(
                hour=event.starts_at.astimezone(tz).hour,
                minute=event.starts_at.astimezone(tz).minute,
            )
        await update_event(session, event, {"starts_at": target})

        await note_change(
            session, bot, hack, message,
            action="перенёс",
            detail=(
                f"{event.kind.emoji} <b>{esc(event.title)}</b>: "
                f"<s>{esc(old)}</s> → <b>{esc(fmt_dt(target, tz))}</b>"
            ),
            audit_action="event_move",
            payload={"id": event.id, "to": target.isoformat()},
        )


@router.message(Command("rm", "удалить", "del"))
async def cmd_rm(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.reply("<code>/rm 12</code> — id этапа из /timeline")
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        event = await get_event(session, int(arg))
        if event is None or event.hackathon_id != hack.id:
            await message.reply("Нет такого id.")
            return
        title, emoji = event.title, event.kind.emoji
        await delete_event(session, event)
        await note_change(
            session, bot, hack, message,
            action="удалил этап", detail=f"{emoji} <s>{esc(title)}</s>",
            audit_action="event_delete", payload={"id": int(arg), "title": title},
        )


@router.message(Command("who", "кто", "явка"))
async def cmd_who(message: Message, command: CommandObject) -> None:
    """Attendance for one event, or for the next one that asks for it."""
    arg = (command.args or "").strip()
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        events = await list_events(session, hack.id)

        if arg.isdigit():
            event = next((e for e in events if e.id == int(arg)), None)
        else:
            from hackbot.domain.services.hackathons import next_event

            event = next_event([e for e in events if e.needs_rsvp])
        if event is None:
            await message.reply("Нет этапа, по которому собирают явку. /timeline")
            return

        summary = await rsvp_summary(session, event)
        text = render_event(hack, event, summary=summary)
        markup = event_kb(event)
    await message.reply(text, reply_markup=markup, disable_web_page_preview=True)


def _split_title_and_date(text: str, tz: ZoneInfo) -> tuple[str, datetime | None]:
    """Peel the trailing date off `Защита 22.09 20:00`.

    `parse_dt` happily ignores words it does not understand, so feeding it the
    whole string would swallow the title too. Instead the longest suffix that
    still leaves a title behind wins, and only if nothing does is the whole
    string treated as a bare date.
    """
    words = text.split()
    if not words:
        return "", None

    for cut in range(1, len(words)):
        # The date has to actually begin here, otherwise `Сдача решения 22.09`
        # would cut at "решения" and quietly drop a word from the title.
        if not _looks_like_date_start(words[cut]):
            continue
        parsed = parse_dt(" ".join(words[cut:]), tz)
        if parsed is None:
            continue
        title = " ".join(words[:cut]).strip(" ,-—")
        # Guard against slicing a date in half, e.g. `22.09 | 20:00`.
        if len(title.split()) <= 2 and parse_dt(title, tz) is not None:
            break
        return title, parsed.dt

    whole = parse_dt(text, tz)
    return ("", whole.dt) if whole is not None else (text, None)


_RELATIVE_WORDS = frozenset({"сегодня", "завтра", "послезавтра", "в"})


def _looks_like_date_start(word: str) -> bool:
    token = word.casefold().replace("ё", "е").strip(",")
    if token[:1].isdigit():
        return True
    if token in _RELATIVE_WORDS:
        return True
    return any(token.startswith(day) for day in WEEKDAYS_SHORT)
