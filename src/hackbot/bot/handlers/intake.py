"""Creating and filling a hackathon: /new, /template.

`/new` is the one place where a photo of a poster turns into a full timeline.
The flow is deliberately forgiving: a bare name works, a wall of text works, an
image works, and anything the model could not find becomes a follow-up question
rather than a failure.
"""

from __future__ import annotations

import io
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hackbot.agent.extract import ExtractedHackathon, extract_hackathon
from hackbot.agent.llm import LLMUnavailableError, llm_available
from hackbot.bot.cards import refresh_card
from hackbot.bot.handlers._helpers import (
    read_attachments,
    remember_questions,
    require_editor,
    require_hack,
)
from hackbot.bot.utils import collect_attachments, message_text, topic_id
from hackbot.db.base import session_scope
from hackbot.db.models import Hackathon
from hackbot.domain.services.docs import add_doc
from hackbot.domain.services.events import (
    add_event,
    ensure_deadline_events,
    list_events,
    propose_timeline,
)
from hackbot.domain.services.hackathons import audit, create, get_by_topic, hack_tz, missing_fields
from hackbot.domain.services.ingest import IngestPlan, IngestResult, apply_plan, build_plan
from hackbot.domain.textutils import esc, truncate
from hackbot.domain.timeutils import fmt_dt
from hackbot.render.digest import render_missing, render_proposal

log = logging.getLogger(__name__)
router = Router(name="intake")

MAX_IMAGE_BYTES = 12 * 1024 * 1024
_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp")


async def _download(bot: Bot, file_id: str) -> bytes | None:
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:
        log.warning("download failed for %s: %s", file_id, exc)
        return None
    if buffer is None:
        return None
    data = buffer.read() if isinstance(buffer, io.IOBase) else bytes(buffer)
    return data if len(data) <= MAX_IMAGE_BYTES else None


@router.message(Command("new", "новый", "создать"))
async def cmd_new(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return

    raw_text = (command.args or "").strip()
    source = message.reply_to_message if message.reply_to_message else message
    attachments = collect_attachments(message) + collect_attachments(message.reply_to_message)
    if not raw_text and source is not message:
        raw_text = message_text(source)

    if not raw_text and not attachments:
        await message.reply(
            "<b>Как заводить</b>\n\n"
            "<code>/new ТендерХак Нижний</code> — просто имя, остальное потом\n"
            "<code>/new</code> + фото афиши — вытащу даты и этапы сам\n"
            "Или реплай на сообщение с афишей и <code>/new</code>\n\n"
            "Текстом тоже можно: <code>/new</code> и следом весь анонс как есть."
        )
        return

    # Download once, keep everything, and pass only the images to the vision model.
    # PDFs and archives are stored as documents even though the model cannot read them.
    downloaded: list[tuple[object, bytes]] = []
    images: list[bytes] = []
    for item in attachments:
        blob = await _download(bot, item.file_id)
        if blob is None:
            continue
        downloaded.append((item, blob))
        if item.is_photo or (item.mime or "") in _IMAGE_MIME:
            images.append(blob)

    async with session_scope() as session:
        hack = await get_by_topic(session, message.chat.id, topic_id(message))
        created = False
        if hack is None:
            title = _guess_title(raw_text)
            hack = await create(
                session,
                title=title,
                chat_id=message.chat.id,
                thread_id=topic_id(message),
                created_by=message.from_user.id if message.from_user else None,
            )
            created = True
            await audit(
                session,
                hack,
                action="create",
                actor=message.from_user.full_name if message.from_user else "",
                tg_user_id=message.from_user.id if message.from_user else None,
            )

        # Store the source material regardless of whether the model can read it.
        for item, blob in downloaded:
            await add_doc(
                session, hack, file_name=item.file_name, payload=blob,
                tg_file_id=item.file_id, mime=item.mime,
                caption=truncate(raw_text, 200) or None,
                uploaded_by=message.from_user.id if message.from_user else None,
            )

        hack_id, tz = hack.id, hack_tz(hack)
        title_now = hack.title

    # PDFs, docx and plain text cannot go to a vision model, but their text can
    # ride along with the prompt.
    blocks, warnings = await read_attachments(downloaded)
    if blocks:
        raw_text = "\n\n".join([raw_text, *blocks]).strip()

    worth_parsing = bool(images) or len(raw_text) > 40
    if not worth_parsing or not llm_available():
        async with session_scope() as session:
            hack = await session.get(Hackathon, hack_id)
            assert hack is not None
            await refresh_card(bot, session, hack)
        note = "Завёл" if created else "Обновил"
        await message.reply(
            f"{note} <b>{esc(title_now)}</b>.\n\n"
            "Дальше: <code>/set начало 20.09 10:00</code>, "
            "<code>/set конец 22.09 18:00</code>, <code>/add Защита 22.09 20:00</code>.\n"
            "Или тегни меня и скажи словами."
        )
        return

    status = await message.reply("🔍 Читаю…" + (" распознаю афишу" if images else ""))

    try:
        extracted = await extract_hackathon(raw_text, images, tz=tz)
    except LLMUnavailableError:
        await status.edit_text("LLM не настроена, заполняй руками. /help")
        return
    except Exception as exc:
        log.exception("extraction failed")
        await status.edit_text(
            f"Не разобрал ({esc(type(exc).__name__)}).\n"
            "Давай руками: <code>/set начало 20.09 10:00</code>"
        )
        return

    summary = await apply_extracted(message, bot, hack_id, extracted, tz, warnings)
    await status.edit_text(summary, disable_web_page_preview=True)


async def apply_extracted(
    message: Message,
    bot: Bot,
    hack_id: int,
    extracted: ExtractedHackathon,
    tz: ZoneInfo,
    warnings: list[str] | None = None,
) -> str:
    """Repair, store and describe what a source turned out to contain.

    Shared by `/new` and by a mention that carries a picture, so a schedule
    screenshot lands through exactly the same audited path either way.
    """
    async with session_scope() as session:
        hack = await session.get(Hackathon, hack_id)
        assert hack is not None
        plan = build_plan(extracted, tz, existing=hack)
        result = await apply_plan(session, hack, plan)
        await ensure_deadline_events(session, hack)
        events = await list_events(session, hack.id)
        gaps = missing_fields(hack, events)
        summary = _summarize(hack, plan, result, tz)
        await audit(
            session, hack, action="ingest",
            actor=message.from_user.full_name if message.from_user else "",
            tg_user_id=message.from_user.id if message.from_user else None,
            details={"fields": list(plan.fields), "events": len(plan.events)},
        )
        await refresh_card(bot, session, hack)

    tail: list[str] = []
    if gaps:
        tail.append(render_missing(gaps))
    if plan.questions:
        rows = "\n".join(f"• {esc(q)}" for q in plan.questions)
        tail.append(
            f"❓ <b>Уточни, пожалуйста</b>\n{rows}\n\n"
            "<i>Ответь на это сообщение одной фразой — разберу.</i>"
        )
        # Parked so a plain reply is understood: the questions were asked by the
        # extraction pass, which holds no conversation of its own.
        async with session_scope() as session:
            await remember_questions(
                session, message.chat.id, topic_id(message), plan.questions
            )
    if warnings:
        tail.append("⚠️ " + "\n⚠️ ".join(esc(w) for w in warnings))
    if tail:
        summary += "\n\n" + "\n\n".join(tail)
    return summary


def _guess_title(text: str) -> str:
    """First line, trimmed - good enough, and /set название fixes it."""
    first = (text or "").strip().splitlines()[0] if text.strip() else ""
    first = first.strip(" .!—-")
    return truncate(first, 90) if first else "Хакатон"


def _summarize(hack: Hackathon, plan: IngestPlan, result: IngestResult, tz: ZoneInfo) -> str:
    lines = [f"✅ <b>{esc(hack.title)}</b> — разобрал"]

    facts: list[str] = []
    if hack.starts_at:
        facts.append(f"начало {esc(fmt_dt(hack.starts_at, tz, with_year=True))}")
    if hack.ends_at:
        facts.append(f"конец {esc(fmt_dt(hack.ends_at, tz))}")
    if hack.reg_deadline:
        facts.append(f"регистрация до {esc(fmt_dt(hack.reg_deadline, tz))}")
    if facts:
        lines.append("")
        lines += [f"• {f}" for f in facts]

    counts: list[str] = []
    if result.added_events:
        counts.append(f"этапов добавлено: {len(result.added_events)}")
    if result.updated_events:
        counts.append(f"обновлено: {len(result.updated_events)}")
    if result.added_links:
        counts.append(f"ссылок: {result.added_links}")
    if counts:
        lines.append("")
        lines.append("· ".join(counts))

    if plan.notes:
        lines.append("")
        lines.append("<i>" + esc("; ".join(plan.notes)) + "</i>")

    lines.append("")
    lines.append("/timeline — посмотреть, что получилось")
    return "\n".join(lines)


@router.message(Command("template", "шаблон"))
async def cmd_template(message: Message, bot: Bot) -> None:
    """Offer the stages nearly every hackathon has, anchored on start and end."""
    if not await require_editor(message, bot):
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        if not hack.starts_at or not hack.ends_at:
            await message.reply(
                "Сначала нужны даты хакатона:\n"
                "<code>/set начало 20.09 10:00</code>\n"
                "<code>/set конец 22.09 18:00</code>"
            )
            return

        existing = await list_events(session, hack.id)
        proposals = propose_timeline(hack, existing)
        if not proposals:
            await message.reply("Все стандартные этапы уже есть. /timeline")
            return

        for item in proposals:
            await add_event(
                session, hack, title=item.title, starts_at=item.starts_at,
                kind=item.kind, ends_at=item.ends_at,
            )
        text = render_proposal(hack, proposals)
        await refresh_card(bot, session, hack)

    await message.reply(
        text.replace("Предлагаю стандартный набор этапов", "Добавил стандартные этапы")
        + "\n\nЛишнее убери: <code>/rm id</code> · /timeline"
    )
