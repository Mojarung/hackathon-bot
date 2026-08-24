"""Shared plumbing for handlers: topic resolution, permission replies, echoes."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.bot.cards import refresh_card
from hackbot.bot.filters import is_editor
from hackbot.bot.utils import Attachment, actor_name, topic_id
from hackbot.db.models import Hackathon
from hackbot.domain.services import kv, textract
from hackbot.domain.services.hackathons import audit, get_by_topic
from hackbot.render.digest import render_change_note

log = logging.getLogger(__name__)

NO_HACK = (
    "В этой теме хакатона нет.\n\n"
    "Заводи: <code>/new Название</code>\n"
    "Или кидай афишу с подписью <code>/new</code> — сам разберу."
)
NO_RIGHTS = "Не-а. Данные правят только админы чата."


async def find_hack(session: AsyncSession, message: Message) -> Hackathon | None:
    return await get_by_topic(session, message.chat.id, topic_id(message))


async def require_hack(session: AsyncSession, message: Message) -> Hackathon | None:
    hack = await find_hack(session, message)
    if hack is None:
        await message.reply(NO_HACK)
    return hack


async def require_editor(message: Message, bot: Bot) -> bool:
    user = message.from_user
    if user is None:
        return False
    if await is_editor(bot, message.chat.id, user.id):
        return True
    await message.reply(NO_RIGHTS)
    return False


async def note_change(
    session: AsyncSession,
    bot: Bot,
    hack: Hackathon,
    message: Message,
    *,
    action: str,
    detail: str,
    audit_action: str,
    payload: dict | None = None,
) -> None:
    """Announce a mutation in the topic and record it, then repaint the card.

    Everyone in the topic sees who moved what, which is the cheap alternative to
    a permissions matrix nobody wants to configure.
    """
    actor = actor_name(message)
    await audit(
        session,
        hack,
        action=audit_action,
        actor=actor,
        tg_user_id=message.from_user.id if message.from_user else None,
        details=payload,
    )
    await message.reply(render_change_note(actor, action, detail))
    await refresh_card(bot, session, hack)


async def read_attachments(
    downloaded: list[tuple[Attachment, bytes]],
) -> tuple[list[str], list[str]]:
    """Turn readable attachments into prompt blocks.

    Returns the blocks plus warnings for files that could not be read - a scanned
    PDF has no text layer, and saying so beats silently ignoring it.
    """
    blocks: list[str] = []
    warnings: list[str] = []

    for item, payload in downloaded:
        if item.is_photo or not textract.is_readable(item.file_name, item.mime):
            continue
        extracted = await textract.extract(item.file_name, item.mime, payload)
        if extracted is None:
            continue
        if extracted.needs_ocr:
            warnings.append(
                f"«{item.file_name}» — сканированный PDF без текстового слоя. "
                "Пришли скриншот страницы, картинку я прочитаю."
            )
            continue
        blocks.append(textract.as_prompt_block(item.file_name, extracted))
        log.info("read %s from %s (%s chars)", extracted.kind, item.file_name,
                 len(extracted.text))

    return blocks, warnings


def _pending_key(chat_id: int, thread_id: int | None) -> str:
    return f"pending_questions:{chat_id}:{thread_id or 0}"


async def remember_questions(
    session: AsyncSession, chat_id: int, thread_id: int | None, questions: list[str]
) -> None:
    """Park the clarifying questions so a plain reply can be understood later.

    Without this the agent sees an answer with no idea what it answers - the
    questions were asked by the extraction pass, which keeps no conversation.
    """
    await kv.set_json(session, _pending_key(chat_id, thread_id), questions[:4])


async def take_questions(
    session: AsyncSession, chat_id: int, thread_id: int | None
) -> list[str]:
    """Read and clear. They only make sense for the reply that follows them."""
    key = _pending_key(chat_id, thread_id)
    questions: list[str] = await kv.get_json(session, key, []) or []
    if questions:
        await kv.set_json(session, key, [])
    return questions


async def answer_query(query: CallbackQuery, text: str | None = None, *, alert: bool = False
                       ) -> None:
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception as exc:  # query too old - harmless
        log.debug("callback answer failed: %s", exc)
