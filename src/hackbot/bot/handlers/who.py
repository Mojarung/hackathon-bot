"""Reading and clearing what the bot remembers about people.

Writing profiles is the agent's job - plain Russian handles "запомни, Саня
фронтендер" better than any argument parsing would. These two commands exist for
the things a command does better: checking what is stored, and wiping it without
having to negotiate with a model that has been told to be rude.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.domain.services import people as people_service
from hackbot.domain.textutils import esc
from hackbot.domain.timeutils import fmt_dt

router = Router(name="who")

# Note: /кто is taken - it asks who is coming to an event (handlers/timeline.py).


@router.message(Command("whois", "профиль"))
async def cmd_whois(message: Message, command: CommandObject) -> None:
    """/whois — про себя, /whois @ник — про другого, ответом — про автора сообщения."""
    if message.from_user is None:
        return
    needle = (command.args or "").strip()
    replied = message.reply_to_message
    target_id = message.from_user.id
    if not needle and replied and replied.from_user and not replied.from_user.is_bot:
        target_id = replied.from_user.id

    async with session_scope() as session:
        person = (
            await people_service.find(session, needle, speaker_id=message.from_user.id)
            if needle
            else await people_service.get(session, target_id)
        )
        if person is None:
            await message.reply(
                f"Про {esc(needle)} ничего нет." if needle
                else "Про тебя ничего не записано. Расскажи о себе — запомню."
            )
            return
        seen = fmt_dt(person.last_seen, get_settings().default_tz)
        lines = [f"👤 <b>{esc(person.display)}</b>"]
        if person.username:
            lines.append(f"@{esc(person.username)}")
        if person.about:
            lines.append(f"Чем занимается: {esc(person.about)}")
        if person.traits:
            lines.append(f"Характер: {esc(person.traits)}")
        if person.notes:
            lines.append(f"Ещё: {esc(person.notes)}")
        if not person.facts:
            lines.append("Ничего не записано — только видел в чате.")
        lines.append(f"\nСообщений: {person.messages}, последний раз {esc(seen)}")
    await message.reply("\n".join(lines))


@router.message(Command("forgetme", "забудь"))
async def cmd_forget_me(message: Message, command: CommandObject) -> None:
    """Стереть, что бот записал о тебе. Само знакомство остаётся."""
    if message.from_user is None:
        return
    field = (command.args or "all").strip() or "all"
    async with session_scope() as session:
        person = await people_service.get(session, message.from_user.id)
        if person is None:
            await message.reply("А я про тебя ничего и не знаю.")
            return
        wiped = await people_service.forget(session, person, field)
    await message.reply(f"Стёр: {esc(wiped)}." if wiped else "Стирать нечего.")
