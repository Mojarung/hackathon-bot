"""The bot butting into a conversation nobody addressed to it.

Registered dead last, after the free-form agent, so it only ever sees messages
that no command, no button and no mention wanted. That ordering is the whole
safety story: anything addressed to the bot has already been handled by the time
a message reaches here.

Reactions are rolled here too, but they live in bot/reactions.py: the mention
handler wants them as well, and a message can get a reaction, a reply, both, or -
most of the time - neither.

Hard rule: neither looks at attachments. Documents and photos are read only
when someone tags the bot or replies to it - see handlers/agent.py and
handlers/media.py. A message carrying a file is skipped outright rather than
answered from its caption, because a reply under a document reads as a comment
on the document whatever the words say.
"""

from __future__ import annotations

import logging
import random
import time
from collections import OrderedDict

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from hackbot.agent.banter import make_banter
from hackbot.agent.llm import llm_available
from hackbot.bot import reactions, recent
from hackbot.bot.utils import has_attachment, message_text, topic_id
from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.domain.services import people
from hackbot.domain.services.hackathons import get_by_topic
from hackbot.domain.textutils import esc

log = logging.getLogger(__name__)
router = Router(name="banter")

# Below this there is nothing to riff on: "ок", "+", "ага".
MIN_TEXT_CHARS = 12
# One line of context is not a conversation, so a butt-in needs at least two.
# A reaction does not: it answers a single message.
MIN_LINES = 2
# Topics a cooldown is remembered for. Bounded for the same reason the message
# buffer is: the bot can be added to any number of chats.
MAX_TRACKED_TOPICS = 500

Key = tuple[int, int | None]

_last_spoken: OrderedDict[Key, float] = OrderedDict()


def _on_cooldown(key: Key, now: float, seconds: int) -> bool:
    last = _last_spoken.get(key)
    return last is not None and now - last < seconds


def _claim(key: Key, now: float) -> None:
    _last_spoken[key] = now
    _last_spoken.move_to_end(key)
    while len(_last_spoken) > MAX_TRACKED_TOPICS:
        _last_spoken.popitem(last=False)


def _release(key: Key) -> None:
    _last_spoken.pop(key, None)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def maybe_react_or_butt_in(message: Message, bot: Bot) -> None:
    settings = get_settings()
    if message.from_user is None or message.from_user.is_bot:
        return
    if has_attachment(message):
        return
    if not llm_available():
        return

    text = message_text(message)
    if len(text) < MIN_TEXT_CHARS or text.startswith("/"):
        return

    chat_id, thread_id = message.chat.id, topic_id(message)
    key: Key = (chat_id, thread_id)
    lines = recent.tail(chat_id, thread_id, settings.banter_context)

    # From here to the claims there must be no await. aiogram handles updates
    # concurrently, and a gap would let two messages arriving together both pass
    # the cooldown check - the bot would answer twice.
    now = time.monotonic()
    react = reactions.wanted(key, text, now)
    speak = (
        settings.banter_chance > 0
        and len(lines) >= MIN_LINES
        and random.random() < settings.banter_chance
        and not _on_cooldown(key, now, settings.banter_cooldown_seconds)
    )
    if not (react or speak):
        return
    if speak:
        _claim(key, now)

    me = await bot.me()

    async with session_scope() as session:
        if not settings.banter_everywhere:
            if await get_by_topic(session, chat_id, thread_id) is None:
                reactions.release(key)
                _release(key)
                return
        profiles: dict[int, str] = {}
        if speak:
            for line in lines:
                if line.user_id == me.id or line.user_id in profiles:
                    continue
                person = await people.get(session, line.user_id)
                if person is not None and person.facts:
                    profiles[line.user_id] = person.summary()

    if react:
        reactions.schedule(bot, message, key, lines, text)

    if not speak:
        return

    reply = await make_banter(lines, profiles, me.full_name or "бот", me.id)
    if reply is None:
        # Nothing worth saying - give the slot back so a real conversation is
        # not silenced for the rest of the cooldown.
        _release(key)
        return

    log.info("banter in chat %s topic %s", chat_id, thread_id)
    try:
        await message.reply(esc(reply), disable_web_page_preview=True)
    except TelegramAPIError as exc:
        # Nobody is waiting for this message. A deleted trigger or a muted bot
        # should not raise out of a handler that was never asked to run.
        log.info("banter not delivered: %s", exc)
        return

    recent.record(
        chat_id, thread_id, author=me.full_name or "бот",
        user_id=me.id, text=reply, is_bot=True,
    )
