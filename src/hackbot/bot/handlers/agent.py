"""Free-form conversation: mention the bot, or reply to it, and say what you want.

Registered last so every slash command wins the routing race. Attachments that
arrive here are stored as documents first, then handed to the model, so nothing
is lost even when the model makes nothing of them.
"""

from __future__ import annotations

import io
import logging
import re

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import select

from hackbot.agent.extract import extract_hackathon
from hackbot.agent.llm import LLMUnavailableError, llm_available
from hackbot.agent.react import AgentDeps, load_history, run_agent
from hackbot.bot.cards import refresh_card
from hackbot.bot.filters import MentionsBot, strip_mention
from hackbot.bot.handlers._helpers import find_hack, read_attachments, take_questions
from hackbot.bot.handlers.fun import build_wisdom, random_teammate
from hackbot.bot.handlers.intake import apply_extracted
from hackbot.bot.handlers.media import _push_everything
from hackbot.bot.utils import actor_name, collect_attachments, message_text, topic_id
from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.db.models import AgentThread, Hackathon
from hackbot.domain.services.docs import add_doc
from hackbot.domain.services.events import detect_conflicts, list_events
from hackbot.domain.services.github import attach_repo
from hackbot.domain.services.hackathons import audit, create, hack_tz, next_event
from hackbot.domain.services.ics import build_calendar, ics_filename
from hackbot.domain.services.participants import list_participants
from hackbot.domain.textutils import esc, truncate
from hackbot.render.digest import render_ping
from hackbot.render.timeline import render_timeline

log = logging.getLogger(__name__)
router = Router(name="agent")

MAX_PROMPT = 3000
# Attachment text gets its own, much larger ceiling: a rulebook is the point.
MAX_DOC_PROMPT = 24_000
_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp")

# Phrasings that mean "tell a joke" and nothing else, matched before the agent.
WISDOM_RE = re.compile(
    r"(мудрост|дай\s+совет|расскажи\s+имбу?|выдай\s+имбу?|имба\s+дня|совет\s+дня"
    r"|скажи\s+что-?нибудь\s+умн)",
    re.IGNORECASE,
)


def _wisdom_target(message: Message, text: str) -> str | None:
    """Who is being put on the spot, if anyone."""
    replied = message.reply_to_message
    if replied and replied.from_user and not replied.from_user.is_bot:
        user = replied.from_user
        return f'<a href="tg://user?id={user.id}">{esc(user.full_name)}</a>'
    handle = re.search(r"@[A-Za-z0-9_]{4,}", text)
    return esc(handle.group(0)) if handle else None


@router.message(MentionsBot(), ~F.text.startswith("/"))
async def on_mention(message: Message, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    me = await bot.me()
    text = strip_mention(message_text(message), me.username)

    # A reply that only says "@bot" is asking about the message it replies to.
    replied = message.reply_to_message
    quoted = message_text(replied)
    replied_by_human = bool(replied and replied.from_user and not replied.from_user.is_bot)
    if quoted and replied_by_human:
        text = f"{text}\n\nСообщение, на которое отвечают:\n{truncate(quoted, 1500)}".strip()

    attachments = collect_attachments(message) + collect_attachments(message.reply_to_message)

    if not text and not attachments:
        await message.reply("Ну? Говори, что надо.")
        return

    # Asking for a joke is answered directly. Routing it through the agent meant
    # the model announced the joke and the joke arrived separately - two
    # messages for one punchline, and the announcement read like a bug.
    if not attachments and WISDOM_RE.search(text):
        async with ChatActionSender.typing(
            bot=bot, chat_id=message.chat.id, message_thread_id=topic_id(message)
        ):
            wisdom = await build_wisdom(message.chat.id, _wisdom_target(message, text))
        await message.reply(wisdom, disable_web_page_preview=True)
        return

    if not llm_available():
        await message.reply("Без LLM я тупой как пробка. Командами работает: /help")
        return

    images: list[bytes] = []
    downloaded: list[tuple[object, bytes]] = []
    for item in attachments:
        blob = await _download(bot, item.file_id)
        if blob is None:
            continue
        downloaded.append((item, blob))
        if item.is_photo or (item.mime or "") in _IMAGE_MIME:
            images.append(blob)

    # A picture is almost always a poster or a schedule. The structured
    # extraction pass is far better at that than the agent calling add_event
    # fifteen times, and it is the only path with a vision-capable model.
    if images and await _handle_pictures(message, bot, text, downloaded, images):
        return

    async with session_scope() as session:
        hack = await find_hack(session, message)
        deps = AgentDeps(
            chat_id=message.chat.id,
            thread_id=topic_id(message),
            hack_id=hack.id if hack else None,
            user_id=message.from_user.id,
            actor=actor_name(message),
            tz=hack_tz(hack) if hack else get_settings().default_tz,
        )
        hack_id = hack.id if hack else None

        if hack is not None:
            for item, blob in downloaded:
                await add_doc(
                    session, hack, file_name=item.file_name, payload=blob,
                    tg_file_id=item.file_id, mime=item.mime,
                    caption=truncate(text, 200) or None,
                    uploaded_by=message.from_user.id,
                )

        thread = await _get_thread(session, message.chat.id, topic_id(message))
        history = load_history(thread.history if thread else None)

        # A reply to the bot is almost always an answer to what it just asked,
        # but the questions came from the extraction pass, which keeps no
        # conversation of its own - so they are replayed as context here.
        pending: list[str] = []
        if replied and replied.from_user and replied.from_user.is_bot:
            pending = await take_questions(session, message.chat.id, topic_id(message))

    blocks, warnings = await read_attachments(downloaded)
    prompt = truncate(text or "Разбери вложение и заполни, что сможешь.", MAX_PROMPT)
    if blocks:
        prompt = "\n\n".join([prompt, *blocks])[:MAX_DOC_PROMPT]
    if pending:
        asked = "\n".join(f"- {q}" for q in pending)
        prompt = (
            f"Ранее ты спросил у пользователя:\n{asked}\n\n"
            f"Пользователь отвечает:\n{prompt}\n\n"
            "Разбери ответ и сохрани всё, что удалось понять, через инструменты."
        )

    try:
        async with ChatActionSender.typing(
            bot=bot, chat_id=message.chat.id, message_thread_id=topic_id(message)
        ):
            reply, history_json = await run_agent(deps, prompt, images, history)
    except LLMUnavailableError:
        await message.reply("LLM отвалилась. /help")
        return
    except Exception:
        log.exception("agent run failed")
        await message.reply("Что-то сломалось. Скажи иначе или командой — /help")
        return

    # The agent may have created the hackathon during this run.
    hack_id = deps.hack_id or hack_id

    async with session_scope() as session:
        await _save_thread(session, message.chat.id, topic_id(message), history_json)
        if hack_id is not None:
            hack = await session.get(Hackathon, hack_id)
            if hack is not None:
                await audit(
                    session, hack, action="agent",
                    actor=actor_name(message), tg_user_id=message.from_user.id,
                    details={"prompt": truncate(prompt, 300)},
                )
                await refresh_card(bot, session, hack)

    # When the real answer is arriving as its own message, a chatty "see the
    # separate message" line is pure noise - drop it.
    self_sufficient = {"timeline", "ics"} & {i.split(":")[0] for i in deps.outbox}
    pointer_like = "отдельн" in reply.casefold() or len(reply.strip()) < 60

    if not (self_sufficient and pointer_like):
        answer = esc(reply) or "Готово."
        if warnings:
            answer += "\n\n⚠️ " + "\n⚠️ ".join(esc(w) for w in warnings)
        await message.reply(answer, disable_web_page_preview=True)

    await _run_outbox(message, bot, hack_id, deps.outbox)


async def _run_outbox(message: Message, bot: Bot, hack_id: int | None, outbox: list[str]) -> None:
    """Perform the side effects the agent asked for but could not do itself."""
    for item in dict.fromkeys(outbox):  # dedupe, preserve order
        try:
            # The joke does not belong to any hackathon, so it runs even in a
            # topic where nothing is set up yet.
            if item.startswith("wisdom:"):
                await _send_wisdom(message, bot, item.split(":", 1)[1])
            elif hack_id is not None:
                await _do_outbox_item(message, bot, hack_id, item)
        except Exception:
            log.exception("outbox item %r failed", item)


async def _send_wisdom(message: Message, bot: Bot, mode: str) -> None:
    target = None
    if mode == "team":
        target = await random_teammate(
            message.chat.id, topic_id(message),
            message.from_user.id if message.from_user else 0,
        )
    async with ChatActionSender.typing(
        bot=bot, chat_id=message.chat.id, message_thread_id=topic_id(message)
    ):
        text = await build_wisdom(message.chat.id, target)
    await message.reply(text, disable_web_page_preview=True)


async def _do_outbox_item(message: Message, bot: Bot, hack_id: int, item: str) -> None:
    async with session_scope() as session:
        hack = await session.get(Hackathon, hack_id)
        if hack is None:
            return
        events = await list_events(session, hack.id)

        if item == "timeline":
            text = render_timeline(hack, events, conflicts=detect_conflicts(events))
            await message.reply(text, disable_web_page_preview=True)
        elif item == "ics":
            if not events:
                return
            payload = build_calendar(hack, events)
            await message.reply_document(
                BufferedInputFile(payload, filename=ics_filename(hack)),
                caption="📥 Импортируй в календарь.",
            )
        elif item.startswith("ping:"):
            people = await list_participants(session, hack.id)
            if people:
                note = item.split(":", 1)[1] or None
                await message.reply(
                    render_ping(hack, next_event(events), people, note),
                    disable_web_page_preview=True,
                )
        elif item == "push_docs":
            if hack.github_repo:
                count = await _push_everything(hack.id, await attach_repo(hack.github_repo))
                await message.reply(f"🐙 Залил в репозиторий документов: {count}")
        elif item == "card":
            await refresh_card(bot, session, hack)


async def _handle_pictures(
    message: Message,
    bot: Bot,
    text: str,
    downloaded: list[tuple[object, bytes]],
    images: list[bytes],
) -> bool:
    """Read a poster or schedule into the hackathon. Returns True when handled."""
    async with session_scope() as session:
        hack = await find_hack(session, message)
        if hack is None:
            title = (text.strip().splitlines() or ["Хакатон"])[0][:90] or "Хакатон"
            hack = await create(
                session,
                title=title,
                chat_id=message.chat.id,
                thread_id=topic_id(message),
                created_by=message.from_user.id if message.from_user else None,
            )
        for item, blob in downloaded:
            await add_doc(
                session, hack, file_name=item.file_name, payload=blob,
                tg_file_id=item.file_id, mime=item.mime,
                caption=truncate(text, 200) or None,
                uploaded_by=message.from_user.id if message.from_user else None,
            )
        hack_id, tz = hack.id, hack_tz(hack)

    status = await message.reply("🔍 Читаю картинку…")
    blocks, warnings = await read_attachments(downloaded)
    source_text = "\n\n".join([text, *blocks]).strip()

    try:
        extracted = await extract_hackathon(source_text, images, tz=tz)
    except Exception:
        log.exception("picture extraction failed")
        await status.edit_text(
            "Не разобрал картинку. Скинь покрупнее или продиктуй текстом."
        )
        return True

    summary = await apply_extracted(message, bot, hack_id, extracted, tz, warnings)
    await status.edit_text(summary, disable_web_page_preview=True)
    return True


async def _download(bot: Bot, file_id: str) -> bytes | None:
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:
        log.warning("download failed: %s", exc)
        return None
    if buffer is None:
        return None
    return buffer.read() if isinstance(buffer, io.IOBase) else bytes(buffer)


async def _get_thread(session, chat_id: int, thread_id: int | None) -> AgentThread | None:
    stmt = select(AgentThread).where(
        AgentThread.chat_id == chat_id,
        AgentThread.thread_id.is_not_distinct_from(thread_id),
    )
    return (await session.scalars(stmt)).first()


async def _save_thread(session, chat_id: int, thread_id: int | None, payload: bytes) -> None:
    """Keep the tail only: full history would grow without bound and cost tokens."""
    text = payload.decode("utf-8")
    if len(text) > 60_000:
        text = ""  # simplest safe truncation - start the conversation fresh
    row = await _get_thread(session, chat_id, thread_id)
    if row is None:
        session.add(AgentThread(chat_id=chat_id, thread_id=thread_id, history=text))
    else:
        row.history = text
    await session.flush()
