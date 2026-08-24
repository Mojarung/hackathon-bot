"""Documents, repository and calendar: /doc, /docs, /repo, /ics."""

from __future__ import annotations

import io
import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from hackbot.bot.cards import refresh_card
from hackbot.bot.handlers._helpers import require_editor, require_hack
from hackbot.bot.utils import collect_attachments, message_text
from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.domain.services.docs import add_doc, build_readme, list_docs, push_doc
from hackbot.domain.services.events import list_events
from hackbot.domain.services.github import (
    GitHubError,
    RepoInfo,
    attach_repo,
    create_repo,
    put_file,
    set_topics,
)
from hackbot.domain.services.hackathons import audit, hack_tz
from hackbot.domain.services.ics import build_calendar, feed_url, ics_filename
from hackbot.domain.textutils import esc, truncate
from hackbot.domain.timeutils import fmt_dt_short

log = logging.getLogger(__name__)
router = Router(name="media")

REPO_HELP = (
    "<b>Репозиторий</b>\n"
    "<code>/repo</code> — показать ссылку\n"
    "<code>/repo new</code> — создать новый в организации\n"
    "<code>/repo attach ссылка</code> — прикрепить существующий\n"
    "<code>/repo push</code> — залить документы и README\n"
    "<code>/repo detach</code> — отвязать"
)


async def _download(bot: Bot, file_id: str) -> bytes | None:
    try:
        buffer = await bot.download(file_id)
    except Exception as exc:
        log.warning("download failed: %s", exc)
        return None
    if buffer is None:
        return None
    return buffer.read() if isinstance(buffer, io.IOBase) else bytes(buffer)


@router.message(Command("doc", "док", "файл"))
async def cmd_doc(message: Message, command: CommandObject, bot: Bot) -> None:
    """Attach files from this message or from the one it replies to."""
    if not await require_editor(message, bot):
        return

    attachments = collect_attachments(message) + collect_attachments(message.reply_to_message)
    if not attachments:
        await message.reply(
            "Приложи файл к команде или ответь ей на сообщение с файлом.\n"
            "Тегнуть меня в реплае на вложение — тоже работает."
        )
        return

    caption = (command.args or "").strip() or message_text(message.reply_to_message)

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        saved: list[str] = []
        for item in attachments:
            blob = await _download(bot, item.file_id)
            if blob is None:
                continue
            doc = await add_doc(
                session, hack,
                file_name=item.file_name, payload=blob,
                tg_file_id=item.file_id, mime=item.mime,
                caption=truncate(caption, 300) or None,
                uploaded_by=message.from_user.id if message.from_user else None,
            )
            saved.append(doc.file_name)
        if saved:
            await audit(
                session, hack, action="doc_add",
                actor=message.from_user.full_name if message.from_user else "",
                tg_user_id=message.from_user.id if message.from_user else None,
                details={"files": saved},
            )
            await refresh_card(bot, session, hack)
        has_repo = bool(hack.github_repo)

    if not saved:
        await message.reply("Не скачалось. Telegram отдаёт файлы только до 20 МБ.")
        return

    tail = "\n\nЗалить в репозиторий: <code>/repo push</code>" if has_repo else ""
    files = "\n".join(f"• <code>{esc(name)}</code>" for name in saved)
    await message.reply(f"📎 <b>Сохранил</b>\n{files}{tail}")


@router.message(Command("docs", "доки", "документы"))
async def cmd_docs(message: Message) -> None:
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        docs = await list_docs(session, hack.id)
        repo_url = hack.github_url
        tz = hack_tz(hack)

    if not docs:
        await message.reply("Документов нет. Кидай файл с <code>/doc</code>.")
        return

    lines = ["📎 <b>Документы</b>", ""]
    for doc in docs:
        size = f" · {doc.size // 1024} КБ" if doc.size else ""
        row = f"• <code>{esc(doc.file_name)}</code>{size}"
        if doc.github_path and repo_url:
            row += f' · <a href="{esc(repo_url)}/blob/HEAD/{esc(doc.github_path)}">в репо</a>'
        lines.append(row)
        details = [fmt_dt_short(doc.created_at, tz)]
        if doc.caption:
            details.append(truncate(doc.caption, 90))
        lines.append(f"     <i>{esc(' · '.join(details))}</i>")
    await message.reply("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("repo", "репо"))
async def cmd_repo(message: Message, command: CommandObject, bot: Bot) -> None:
    action, _, argument = (command.args or "").strip().partition(" ")
    action = action.casefold()
    argument = argument.strip()

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        hack_id, title, year = hack.id, hack.title, hack.year
        current_repo, current_url = hack.github_repo, hack.github_url
        description = hack.description

    if not action:
        if current_url:
            await message.reply(
                f'🐙 <a href="{esc(current_url)}">{esc(current_repo or "репозиторий")}</a>',
                disable_web_page_preview=False,
            )
        else:
            await message.reply("Репозиторий не привязан.\n\n" + REPO_HELP)
        return

    if not get_settings().github_enabled:
        await message.reply("GITHUB_TOKEN не настроен, интеграция выключена.")
        return
    if not await require_editor(message, bot):
        return

    if action in {"detach", "отвязать"}:
        async with session_scope() as session:
            hack = await require_hack(session, message)
            if hack is None:
                return
            hack.github_repo = hack.github_url = None
            await refresh_card(bot, session, hack)
        await message.reply("Отвязал. Сам репозиторий на месте.")
        return

    status = await message.reply("🐙 Работаю с GitHub…")

    try:
        if action in {"new", "создать", "create"}:
            repo = await create_repo(title, year, description=description)
            await set_topics(repo, ["hackathon", str(year)])
        elif action in {"attach", "прикрепить", "add"}:
            if not argument:
                await status.edit_text(
                    "Укажи репозиторий: <code>/repo attach Mojarung/tender_hack_2026</code>\n"
                    "или полной ссылкой."
                )
                return
            repo = await attach_repo(argument)
        elif action in {"push", "залить"}:
            if not current_repo:
                await status.edit_text(
                    "Сначала <code>/repo new</code> или <code>/repo attach</code>."
                )
                return
            repo = await attach_repo(current_repo)
        else:
            await status.edit_text(REPO_HELP)
            return
    except GitHubError as exc:
        await status.edit_text(f"GitHub отказал: {esc(exc.message)}")
        return
    except Exception:
        log.exception("github call failed")
        await status.edit_text("Не смог достучаться до GitHub. Попробуй ещё раз.")
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        hack.github_repo = repo.full_name
        hack.github_url = repo.html_url
        await audit(
            session, hack, action=f"repo_{action}",
            actor=message.from_user.full_name if message.from_user else "",
            tg_user_id=message.from_user.id if message.from_user else None,
            details={"repo": repo.full_name},
        )
        await refresh_card(bot, session, hack)

    if action in {"push", "залить", "new", "создать", "create"}:
        pushed = await _push_everything(hack_id, repo)
        await status.edit_text(
            f'🐙 <a href="{esc(repo.html_url)}">{esc(repo.full_name)}</a>\n'
            f"Залил README и документов: {pushed}"
        )
    else:
        await status.edit_text(
            f'🐙 Прикрепил <a href="{esc(repo.html_url)}">{esc(repo.full_name)}</a>\n'
            "<i>Теперь ссылка всегда под рукой: /repo</i>"
        )


async def _push_everything(hack_id: int, repo: RepoInfo) -> int:
    """README plus every stored document that is not in the repo yet."""
    async with session_scope() as session:
        from hackbot.db.models import Hackathon

        hack = await session.get(Hackathon, hack_id)
        if hack is None:
            return 0
        events = await list_events(session, hack.id)
        docs = await list_docs(session, hack.id)
        readme = build_readme(hack, events, docs)

        try:
            await put_file(repo, "README.md", readme, message="docs: обновить таймлайн")
        except GitHubError as exc:
            log.warning("readme push failed: %s", exc)

        count = 0
        for doc in docs:
            if doc.github_path:
                continue
            try:
                if await push_doc(repo, doc):
                    count += 1
            except GitHubError as exc:
                log.warning("doc push failed for %s: %s", doc.file_name, exc)
        return count


@router.message(Command("ics", "календарь"))
async def cmd_ics(message: Message) -> None:
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        events = await list_events(session, hack.id)
        if not events:
            await message.reply("Нечего экспортировать, этапов нет. /timeline")
            return
        payload = build_calendar(hack, events)
        name = ics_filename(hack)
        subscribe = feed_url(hack)

    caption = "📥 Кидай в календарь, все этапы приедут разом."
    if subscribe:
        caption += (
            f"\n\nА лучше подпишись на ленту, тогда правки таймлайна дойдут сами:\n"
            f"<code>{esc(subscribe)}</code>"
        )
    await message.reply_document(
        BufferedInputFile(payload, filename=name), caption=caption
    )
