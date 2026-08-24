"""Team roster: /join, /leave, /team, /role, /ping."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hackbot.bot.cards import refresh_card
from hackbot.bot.handlers._helpers import require_editor, require_hack
from hackbot.db.base import session_scope
from hackbot.domain.services.events import list_events
from hackbot.domain.services.hackathons import next_event
from hackbot.domain.services.participants import (
    ROLES,
    join,
    leave,
    list_participants,
    set_captain,
    set_role,
)
from hackbot.domain.textutils import esc
from hackbot.render.digest import render_ping

router = Router(name="team")


@router.message(Command("join", "я", "участвую"))
async def cmd_join(message: Message, command: CommandObject, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    role = (command.args or "").strip() or None

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        participant, created = await join(
            session,
            hack,
            tg_user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role=role,
        )
        title = hack.title
        await refresh_card(bot, session, hack)

    if created:
        suffix = f" · роль: {esc(participant.role)}" if participant.role else ""
        await message.reply(
            f"Записал в <b>{esc(title)}</b>{suffix}.\n"
            f"<i>Роль:</i> <code>/role бэкенд</code>"
        )
    else:
        await message.reply("Ты и так в команде. /team — состав.")


@router.message(Command("leave", "выйти"))
async def cmd_leave(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        removed = await leave(session, hack, user.id)
        if removed:
            await refresh_card(bot, session, hack)
    await message.reply("Убрал." if removed else "Тебя тут и не было.")


@router.message(Command("role", "роль"))
async def cmd_role(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None:
        return
    role = (command.args or "").strip()
    if not role:
        await message.reply(
            "<code>/role бэкенд</code>\n\n"
            f"<i>Например:</i> {esc(', '.join(ROLES))}"
        )
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        participant = await set_role(session, hack, user.id, role[:64])
    if participant is None:
        await message.reply("Сначала /join")
        return
    await message.reply(f"Роль: <b>{esc(role)}</b>")


@router.message(Command("captain", "капитан"))
async def cmd_captain(message: Message, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if target is None:
        return
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        ok = await set_captain(session, hack, target.id)
    if not ok:
        await message.reply("Его нет в команде. Пусть сделает /join")
        return
    await message.reply(f"Капитан: <b>{esc(target.full_name)}</b>")


@router.message(Command("team", "команда"))
async def cmd_team(message: Message) -> None:
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        people = await list_participants(session, hack.id)
        title = hack.title

    if not people:
        await message.reply(
            f"В <b>{esc(title)}</b> пока никого.\n"
            "Пусть каждый напишет <code>/join</code>, тогда буду знать, кого дёргать."
        )
        return

    lines = [f"👥 <b>Команда — {esc(title)}</b>", ""]
    for person in people:
        row = "👑 " if person.is_captain else "• "
        row += f"<b>{esc(person.display)}</b>"
        if person.role:
            row += f" — {esc(person.role)}"
        lines.append(row)
    await message.reply("\n".join(lines))


@router.message(Command("ping", "пинг"))
async def cmd_ping(message: Message, command: CommandObject) -> None:
    """The one place the bot tags people by name, and only when asked to."""
    note = (command.args or "").strip() or None

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        people = await list_participants(session, hack.id)
        if not people:
            await message.reply("Некого пинговать, команда пустая. /join в помощь.")
            return
        events = await list_events(session, hack.id)
        upcoming = next_event(events)
        text = render_ping(hack, upcoming, people, note)

    await message.reply(text, disable_web_page_preview=True)
