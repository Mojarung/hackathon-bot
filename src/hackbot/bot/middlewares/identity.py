"""Remember everyone the bot sees speak, not only the people who address it.

Runs as an outer middleware so it fires before any filter decides the message is
none of the bot's business. That is the point: by the time someone asks "кто
такой Саня", Саня has to already be in the table, and he will never have
mentioned the bot.

Only the Telegram identity is written here. Character and occupation are learned
through the agent's tools, on purpose - see domain/services/people.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from hackbot.db.base import session_scope
from hackbot.domain.services.people import touch

log = logging.getLogger(__name__)


class IdentityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if isinstance(event, Message) and user is not None and not user.is_bot:
            try:
                async with session_scope() as session:
                    await touch(
                        session,
                        tg_user_id=user.id,
                        username=user.username,
                        full_name=user.full_name or "",
                        chat_id=event.chat.id,
                    )
            except Exception:
                # Bookkeeping must never cost someone their message.
                log.exception("could not record the sender")
        return await handler(event, data)
