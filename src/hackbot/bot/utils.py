"""Telegram-side helpers: topic resolution, tolerant sends, attachment digging."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Document, InlineKeyboardMarkup, Message, PhotoSize

log = logging.getLogger(__name__)

MAX_MESSAGE = 4096


def topic_id(message: Message) -> int | None:
    """The forum topic a message belongs to.

    `message_thread_id` is also set for plain discussion-group reply chains, so
    `is_topic_message` is what actually disambiguates. Messages in the General
    topic carry neither, and None is exactly what you pass back to reply there.
    """
    return message.message_thread_id if message.is_topic_message else None


def thread_kwargs(thread_id: int | None) -> dict[str, int]:
    return {"message_thread_id": thread_id} if thread_id else {}


def actor_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return ""
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


@dataclass(slots=True)
class Attachment:
    file_id: str
    file_name: str
    mime: str | None
    size: int | None
    is_photo: bool


def collect_attachments(message: Message | None) -> list[Attachment]:
    """Photos and documents from a message, largest photo size only."""
    if message is None:
        return []
    out: list[Attachment] = []
    if message.photo:
        biggest: PhotoSize = message.photo[-1]
        out.append(
            Attachment(
                file_id=biggest.file_id,
                file_name=f"photo_{biggest.file_unique_id}.jpg",
                mime="image/jpeg",
                size=biggest.file_size,
                is_photo=True,
            )
        )
    doc: Document | None = message.document
    if doc:
        out.append(
            Attachment(
                file_id=doc.file_id,
                file_name=doc.file_name or f"file_{doc.file_unique_id}",
                mime=doc.mime_type,
                size=doc.file_size,
                is_photo=(doc.mime_type or "").startswith("image/"),
            )
        )
    return out


def message_text(message: Message | None) -> str:
    if message is None:
        return ""
    return (message.text or message.caption or "").strip()


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on blank lines, then lines, so HTML tags never straddle a boundary."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n"):
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = block[:limit]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_html(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool = False,
) -> Message | None:
    """Send, splitting when needed and surviving the usual Telegram grumbles."""
    parts = split_message(text)
    sent: Message | None = None
    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        try:
            sent = await bot.send_message(
                chat_id,
                part,
                reply_markup=reply_markup if is_last else None,
                disable_notification=disable_notification,
                **thread_kwargs(thread_id),
            )
        except TelegramRetryAfter as exc:
            log.warning("flood control: sleeping %ss", exc.retry_after)
            raise
        except TelegramForbiddenError:
            log.warning("bot was kicked from chat %s", chat_id)
            return None
        except TelegramBadRequest as exc:
            if "thread not found" in str(exc).lower():
                # topic was deleted - fall back to the chat's General topic
                sent = await bot.send_message(chat_id, part)
            else:
                log.warning("send failed in %s: %s", chat_id, exc)
                return None
    return sent


async def edit_html(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit in place. `message is not modified` is a success, not a failure."""
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
        return True
    except TelegramBadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" in reason:
            return True
        if "message to edit not found" in reason or "message can't be edited" in reason:
            return False
        log.warning("edit failed for %s/%s: %s", chat_id, message_id, exc)
        return False
    except TelegramForbiddenError:
        return False


async def try_pin(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.info("could not pin in %s: %s", chat_id, exc)
        return False
