"""Two things the bot does without being asked: react, and butt in.

Registered dead last, after the free-form agent, so it only ever sees messages
that no command, no button and no mention wanted. That ordering is the whole
safety story: anything addressed to the bot has already been handled by the time
a message reaches here.

Both live in one handler because a router stops at the first matching one, and
this one matches every group message. They roll their dice separately, so a
message can get a reaction, a reply, both, or - most of the time - neither.

Hard rule: neither path looks at attachments. Documents and photos are read only
when someone tags the bot or replies to it - see handlers/agent.py and
handlers/media.py. A message carrying a file is skipped outright rather than
answered from its caption, because a reply under a document reads as a comment
on the document whatever the words say.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, ReactionTypeEmoji

from hackbot.agent.banter import make_banter
from hackbot.agent.llm import llm_available
from hackbot.agent.reaction import pick_reaction
from hackbot.bot import recent
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
Clock = OrderedDict[Key, float]

_last_spoken: Clock = OrderedDict()
_last_reacted: Clock = OrderedDict()


def _on_cooldown(clock: Clock, key: Key, now: float, seconds: int) -> bool:
    last = clock.get(key)
    return last is not None and now - last < seconds


def _claim(clock: Clock, key: Key, now: float) -> None:
    clock[key] = now
    clock.move_to_end(key)
    while len(clock) > MAX_TRACKED_TOPICS:
        clock.popitem(last=False)


def _release(clock: Clock, key: Key) -> None:
    clock.pop(key, None)


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
    react = (
        settings.reaction_chance > 0
        and random.random() < settings.reaction_chance
        and not _on_cooldown(_last_reacted, key, now, settings.reaction_cooldown_seconds)
    )
    speak = (
        settings.banter_chance > 0
        and len(lines) >= MIN_LINES
        and random.random() < settings.banter_chance
        and not _on_cooldown(_last_spoken, key, now, settings.banter_cooldown_seconds)
    )
    if not (react or speak):
        return
    if react:
        _claim(_last_reacted, key, now)
    if speak:
        _claim(_last_spoken, key, now)

    me = await bot.me()

    async with session_scope() as session:
        if not settings.banter_everywhere:
            if await get_by_topic(session, chat_id, thread_id) is None:
                _release(_last_reacted, key)
                _release(_last_spoken, key)
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
        # Detached: a reaction is a garnish, and a reply must not wait on it.
        asyncio.create_task(  # noqa: RUF006 - deliberately detached
            _react(bot, message, lines, text, key)
        )

    if not speak:
        return

    reply = await make_banter(lines, profiles, me.full_name or "бот", me.id)
    if reply is None:
        # Nothing worth saying - give the slot back so a real conversation is
        # not silenced for the rest of the cooldown.
        _release(_last_spoken, key)
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


async def _react(
    bot: Bot, message: Message, lines: list[recent.Line], text: str, key: Key
) -> None:
    emoji = await pick_reaction(lines, text)
    if emoji is None:
        _release(_last_reacted, key)
        return
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except TelegramAPIError as exc:
        # A chat can narrow the set of allowed reactions, and the message may be
        # gone by now. Neither deserves a traceback for something nobody asked for.
        log.info("reaction %s not set: %s", emoji, exc)
        return
    log.info("reacted %s in chat %s topic %s", emoji, message.chat.id, topic_id(message))
