"""Occasional sticker after the bot speaks.

Implemented as an outgoing-request middleware rather than a call at every reply
site: there are dozens of those, and one of them would inevitably be forgotten.

Only `sendMessage` triggers it. Notably NOT the card edits - those happen every
minute, and a sticker each time would be unbearable.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import Response, TelegramType

from hackbot.config import get_settings

log = logging.getLogger(__name__)

_cache: dict[str, list[str]] = {}
_failed: set[str] = set()


async def sticker_ids(bot: Bot, set_name: str) -> list[str]:
    """File ids of the pack, fetched once and remembered for the process."""
    if set_name in _cache:
        return _cache[set_name]
    if set_name in _failed:
        return []
    try:
        pack = await bot.get_sticker_set(set_name)
    except Exception as exc:
        log.warning("sticker set %r unavailable: %s", set_name, exc)
        _failed.add(set_name)
        return []
    ids = [s.file_id for s in pack.stickers]
    _cache[set_name] = ids
    log.info("sticker set %r loaded: %s stickers", set_name, len(ids))
    return ids


class StickerMiddleware(BaseRequestMiddleware):
    """Fires a sticker after some of the bot's own messages."""

    def __init__(self, set_name: str, chance: float) -> None:
        self.set_name = set_name
        self.chance = max(0.0, min(1.0, chance))

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        response = await make_request(bot, method)

        if not self.chance or not isinstance(method, SendMessage):
            return response
        if random.random() >= self.chance:
            return response

        # Fire and forget: the reply must not wait on a sticker, and a failure
        # here should never surface as a failed command.
        asyncio.create_task(  # noqa: RUF006 - deliberately detached
            self._send(bot, method.chat_id, getattr(method, "message_thread_id", None))
        )
        return response

    async def _send(self, bot: Bot, chat_id: Any, thread_id: int | None) -> None:
        try:
            ids = await sticker_ids(bot, self.set_name)
            if not ids:
                return
            await bot.send_sticker(
                chat_id,
                random.choice(ids),
                message_thread_id=thread_id,
                disable_notification=True,
            )
        except Exception as exc:
            log.debug("sticker send skipped: %s", exc)


def install(bot: Bot) -> None:
    settings = get_settings()
    if not settings.sticker_set or settings.sticker_chance <= 0:
        return
    bot.session.middleware(StickerMiddleware(settings.sticker_set, settings.sticker_chance))
    log.info(
        "sticker spice on: pack %r, chance %.0f%%",
        settings.sticker_set, settings.sticker_chance * 100,
    )
