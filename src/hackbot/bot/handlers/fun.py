"""The joke command: /wisdom.

Three shapes, all replying to the message that triggered them:
  /wisdom              - the bot's own advice
  /wisdom @user        - ping that person for theirs, and offer one in return
  reply + /wisdom      - same, addressed to the author of the replied-to message
"""

from __future__ import annotations

import logging
import random

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from hackbot.agent.wisdom import generate_advice
from hackbot.bot.utils import topic_id
from hackbot.db.base import session_scope
from hackbot.domain.services.hackathons import get_by_topic
from hackbot.domain.services.kv import get_json, push_capped
from hackbot.domain.services.participants import list_participants
from hackbot.domain.textutils import esc
from hackbot.render.wisdom import render_advice, render_wisdom_ping

log = logging.getLogger(__name__)
router = Router(name="fun")

# Remembering recent jokes per chat is what keeps the model from telling the
# same one every day; the key is per chat so two topics do not fight over it.
HISTORY_CAP = 25


def _history_key(chat_id: int) -> str:
    return f"wisdom_history:{chat_id}"


async def build_wisdom(chat_id: int, target_html: str | None = None) -> str:
    """Generate one advice and render it. Shared by the command and the agent tool.

    Everything goes through here so the tuned prompt, the per-chat memory of
    recent jokes and the formatting stay in one place.
    """
    async with session_scope() as session:
        recent: list[str] = await get_json(session, _history_key(chat_id), []) or []

    advice, seed = await generate_advice(avoid=recent)

    async with session_scope() as session:
        await push_capped(session, _history_key(chat_id), advice.joined(), cap=HISTORY_CAP)

    if target_html:
        return render_wisdom_ping(advice, seed.header, mention_html=target_html)
    return render_advice(advice, seed.header)


@router.message(Command("wisdom", "мудрость", "w", "совет", "имба", "имбу"))
async def cmd_wisdom(message: Message, command: CommandObject, bot: Bot) -> None:
    arg = (command.args or "").strip()

    target_html: str | None = None
    replied = message.reply_to_message
    if replied is not None and replied.from_user and not replied.from_user.is_bot:
        user = replied.from_user
        target_html = f'<a href="tg://user?id={user.id}">{esc(user.full_name)}</a>'
    elif arg.startswith("@") and len(arg) > 1:
        target_html = esc(arg.split()[0])
    elif arg.casefold() in {"кто-нибудь", "любой", "random", "рандом"}:
        target_html = await random_teammate(message.chat.id, topic_id(message),
                                            message.from_user.id if message.from_user else 0)

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id,
                                       message_thread_id=topic_id(message)):
        text = await build_wisdom(message.chat.id, target_html)

    await message.reply(text, disable_web_page_preview=True)


async def random_teammate(chat_id: int, thread_id: int | None, asker_id: int) -> str | None:
    """Pick someone from the roster to put on the spot, preferring not the asker."""
    async with session_scope() as session:
        hack = await get_by_topic(session, chat_id, thread_id)
        if hack is None:
            return None
        people = await list_participants(session, hack.id)
    others = [p for p in people if p.tg_user_id != asker_id] or people
    return random.choice(others).mention_html if others else None
