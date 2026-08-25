"""Feed every message into the recent-lines buffer.

Outer, like the identity middleware, because the bot has to overhear the whole
conversation - not only the parts addressed to it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from hackbot.bot import recent
from hackbot.bot.utils import actor_name, has_attachment, message_text, topic_id


class RecentMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # A caption belongs to the file it is attached to. Letting it into the
        # buffer would put it in front of the model on the next message, which
        # is exactly what skipping media messages is supposed to prevent.
        if isinstance(event, Message) and event.from_user is not None and not has_attachment(event):
            text = message_text(event)
            if text:
                recent.record(
                    event.chat.id,
                    topic_id(event),
                    author=actor_name(event),
                    user_id=event.from_user.id,
                    text=text,
                    is_bot=event.from_user.is_bot,
                )
        return await handler(event, data)
