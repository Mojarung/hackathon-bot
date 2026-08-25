"""Emoji reactions, shared by every path that reads a message.

Lives outside the handlers because both of them want it: the bot reacts to
messages nobody addressed to it, and to messages that are addressed to it. The
first version only did the former, which meant that anyone testing the feature
the obvious way - by talking to the bot - saw nothing at all.

The cooldown is shared for the same reason it exists: a reaction on every second
message stops meaning anything, and it does not matter which path put it there.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, ReactionTypeEmoji

from hackbot.agent.reaction import pick_reaction
from hackbot.bot.recent import Line
from hackbot.config import get_settings

log = logging.getLogger(__name__)

# Shorter than this there is nothing to have an opinion about.
MIN_TEXT_CHARS = 12
# Topics a cooldown is remembered for; bounded because the bot can be added to
# any number of chats and nothing else would evict them.
MAX_TRACKED_TOPICS = 500

Key = tuple[int, int | None]

_last_reacted: OrderedDict[Key, float] = OrderedDict()


def on_cooldown(key: Key, now: float, seconds: int) -> bool:
    last = _last_reacted.get(key)
    return last is not None and now - last < seconds


def claim(key: Key, now: float) -> None:
    _last_reacted[key] = now
    _last_reacted.move_to_end(key)
    while len(_last_reacted) > MAX_TRACKED_TOPICS:
        _last_reacted.popitem(last=False)


def release(key: Key) -> None:
    _last_reacted.pop(key, None)


def wanted(key: Key, text: str, now: float | None = None) -> bool:
    """Roll the dice and take the slot in one go.

    Deliberately does both: the caller must not await between deciding and
    claiming, or two messages arriving together would each get a reaction.
    """
    settings = get_settings()
    if settings.reaction_chance <= 0 or len(text.strip()) < MIN_TEXT_CHARS:
        return False
    now = time.monotonic() if now is None else now
    if random.random() >= settings.reaction_chance:
        return False
    if on_cooldown(key, now, settings.reaction_cooldown_seconds):
        return False
    claim(key, now)
    return True


def schedule(bot: Bot, message: Message, key: Key, lines: list[Line], text: str) -> None:
    """Fire and forget: a reaction is a garnish, nothing may wait on it."""
    asyncio.create_task(  # noqa: RUF006 - deliberately detached
        _react(bot, message, key, lines, text)
    )


async def _react(
    bot: Bot, message: Message, key: Key, lines: list[Line], text: str
) -> None:
    emoji = await pick_reaction(lines, text)
    if emoji is None:
        release(key)  # nothing fitted, so do not spend the cooldown on it
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
    log.info("reacted %s in chat %s", emoji, message.chat.id)
