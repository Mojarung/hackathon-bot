"""Custom filters. aiogram ships no mention filter, and media messages put their
entities in `caption_entities`, which is the trap this module exists to avoid.
"""

from __future__ import annotations

import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, MessageEntityType
from aiogram.filters import Filter
from aiogram.types import Message

from hackbot.config import get_settings

_ADMIN_TTL = 300.0
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


def _entities(message: Message) -> list:
    return list(message.entities or []) + list(message.caption_entities or [])


async def mentions_bot(message: Message, bot: Bot) -> bool:
    """True when the message @-mentions this bot, by username or text mention."""
    me = await bot.me()
    text = message.text or message.caption or ""
    for ent in _entities(message):
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and ent.user.id == me.id:
            return True
        if ent.type == MessageEntityType.MENTION and me.username:
            fragment = text[ent.offset: ent.offset + ent.length].lstrip("@")
            if fragment.casefold() == me.username.casefold():
                return True
    return False


def strip_mention(text: str, username: str | None) -> str:
    if not username:
        return text.strip()
    return text.replace(f"@{username}", " ").replace(f"@{username.lower()}", " ").strip()


class MentionsBot(Filter):
    """Message that addresses the bot: an @mention or a reply to its own message."""

    def __init__(self, *, allow_reply: bool = True) -> None:
        self.allow_reply = allow_reply

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if await mentions_bot(message, bot):
            return True
        if not self.allow_reply:
            return False
        replied = message.reply_to_message
        if replied and replied.from_user:
            me = await bot.me()
            return replied.from_user.id == me.id
        return False


class IsPrivate(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"


class IsGroup(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type in {"group", "supergroup"}


async def is_editor(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Who may mutate data: the configured allowlist, or any chat administrator.

    Cached briefly - `getChatMember` on every button press would be wasteful and
    counts against the rate limit.
    """
    settings = get_settings()
    allowlist = settings.admin_ids
    if allowlist:
        return user_id in allowlist

    key = (chat_id, user_id)
    cached = _admin_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[1] < _ADMIN_TTL:
        return cached[0]

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        allowed = member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except Exception:
        allowed = False  # private chats and lookup failures fall through to False
    _admin_cache[key] = (allowed, now)
    return allowed


class IsEditor(Filter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.chat.type == "private":
            return True
        if not message.from_user:
            return False
        return await is_editor(bot, message.chat.id, message.from_user.id)
