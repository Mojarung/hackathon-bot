"""Editing the hackathon record: /info, /set, /link, /status, /result, /bind."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from hackbot.bot.cards import refresh_card
from hackbot.bot.handlers._helpers import note_change, require_editor, require_hack
from hackbot.bot.utils import topic_id
from hackbot.db.base import session_scope
from hackbot.domain.enums import HackStatus, LinkKind
from hackbot.domain.services import calsync
from hackbot.domain.services.events import ensure_deadline_events, list_events
from hackbot.domain.services.hackathons import (
    derive_status,
    hack_tz,
    missing_fields,
    rebind_topic,
    remove_link,
    set_link,
    update_fields,
)
from hackbot.domain.services.participants import list_participants
from hackbot.domain.textutils import esc, normalize_url
from hackbot.domain.timeutils import fmt_dt, parse_dt
from hackbot.render.card import render_info
from hackbot.render.digest import render_missing

router = Router(name="manage")

# Russian field names people actually type, mapped onto model attributes.
FIELD_ALIASES: dict[str, str] = {
    "начало": "starts_at", "старт": "starts_at", "start": "starts_at",
    "конец": "ends_at", "финиш": "ends_at", "окончание": "ends_at", "end": "ends_at",
    "регистрация": "reg_deadline", "рег": "reg_deadline", "reg": "reg_deadline",
    "название": "title", "имя": "title", "title": "title",
    "год": "year", "year": "year",
    "город": "city", "city": "city",
    "онлайн": "is_online", "online": "is_online",
    "организатор": "organizer", "орг": "organizer",
    "описание": "description", "desc": "description",
    "тз": "tz", "таймзона": "tz", "часовой": "tz", "tz": "tz",
}
DATE_FIELDS = {"starts_at", "ends_at", "reg_deadline"}

LINK_ALIASES: dict[str, LinkKind] = {
    "сайт": LinkKind.SITE, "site": LinkKind.SITE,
    "правила": LinkKind.RULES, "условия": LinkKind.RULES, "rules": LinkKind.RULES,
    "чат": LinkKind.CHAT, "chat": LinkKind.CHAT,
    "канал": LinkKind.CHANNEL, "channel": LinkKind.CHANNEL,
    "трансляция": LinkKind.STREAM, "стрим": LinkKind.STREAM, "stream": LinkKind.STREAM,
    "таблица": LinkKind.TABLE, "table": LinkKind.TABLE,
    "форма": LinkKind.FORM, "form": LinkKind.FORM,
    "репо": LinkKind.REPO, "repo": LinkKind.REPO,
    "ссылка": LinkKind.OTHER, "other": LinkKind.OTHER,
}

_FIELD_LABELS = {
    "starts_at": "начало", "ends_at": "конец", "reg_deadline": "дедлайн регистрации",
    "title": "название", "year": "год", "city": "город", "is_online": "формат",
    "organizer": "организатор", "description": "описание", "tz": "часовой пояс",
}


@router.message(Command("info", "инфо"))
async def cmd_info(message: Message) -> None:
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        events = await list_events(session, hack.id)
        people = await list_participants(session, hack.id)
        text = render_info(hack, events, people)
        gaps = missing_fields(hack, events)
    if gaps:
        text += "\n\n" + render_missing(gaps)
    await message.reply(text, disable_web_page_preview=True)


@router.message(Command("set", "уст"))
async def cmd_set(message: Message, command: CommandObject, bot: Bot) -> None:
    """`/set начало 20.09 10:00`. Everything is optional, so anything can be set later."""
    if not await require_editor(message, bot):
        return

    args = (command.args or "").strip()
    if not args:
        known = ", ".join(sorted({v for v in FIELD_ALIASES}))
        await message.reply(
            "<b>Что задать?</b>\n"
            "<code>/set начало 20.09 10:00</code>\n"
            "<code>/set конец 22.09 18:00</code>\n"
            "<code>/set регистрация 17.09 23:59</code>\n"
            "<code>/set город Нижний Новгород</code>\n"
            "<code>/set описание Хакатон про закупки</code>\n\n"
            f"<i>Поля: {esc(known)}</i>"
        )
        return

    field_word, _, value = args.partition(" ")
    field = FIELD_ALIASES.get(field_word.strip().casefold().replace("ё", "е"))
    value = value.strip()
    if field is None:
        await message.reply(f"Не знаю поле «{esc(field_word)}». Посмотри <code>/set</code>.")
        return

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        tz = hack_tz(hack)

        if not value:
            if field in {"title", "year", "tz"}:
                await message.reply("Это поле не очистить, задавай значение.")
                return
            await update_fields(session, hack, {field: None})
            await note_change(
                session, bot, hack, message,
                action="очистил", detail=f"<b>{_FIELD_LABELS.get(field, field)}</b>",
                audit_action="set", payload={field: None},
            )
            return

        parsed: object
        if field in DATE_FIELDS:
            result = parse_dt(value, tz)
            if result is None:
                await message.reply(
                    "Не разобрал дату. Понимаю так: <code>20.09 18:00</code>, "
                    "<code>2026-09-20 18:00</code>, <code>завтра 18:00</code>, "
                    "<code>пт 18:00</code>."
                )
                return
            parsed = result.dt
            shown = fmt_dt(result.dt, tz, with_year=True)
        elif field == "year":
            if not value.isdigit():
                await message.reply("Год — это число, например <code>2026</code>.")
                return
            parsed, shown = int(value), value
        elif field == "is_online":
            parsed = value.casefold() in {"да", "yes", "true", "1", "онлайн", "+"}
            shown = "онлайн" if parsed else "офлайн"
        elif field == "tz":
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(value)
            except Exception:
                await message.reply(
                    "Не знаю такой пояс. Нужно IANA-имя: <code>Europe/Moscow</code>, "
                    "<code>Asia/Yekaterinburg</code>."
                )
                return
            parsed, shown = value, value
        else:
            parsed, shown = value, value

        changed = await update_fields(session, hack, {field: parsed})
        if not changed:
            await message.reply("Так и было.")
            return

        # Title, year and timezone are stamped onto every calendar entry of this
        # hackathon, so editing one of them restyles the whole timeline in Google.
        calsync.mark_dirty(hack.id)

        # A new start or registration date has to grow its own reminders.
        if field in {"starts_at", "reg_deadline"}:
            await ensure_deadline_events(session, hack)

        await note_change(
            session, bot, hack, message,
            action="изменил",
            detail=f"<b>{_FIELD_LABELS.get(field, field)}</b> → {esc(str(shown))}",
            audit_action="set", payload={field: str(parsed)},
        )


@router.message(Command("link", "ссылка"))
async def cmd_link(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return

    args = (command.args or "").strip()
    if not args:
        kinds = ", ".join(sorted({k for k in LINK_ALIASES if k.isalpha() and ord(k[0]) > 127}))
        await message.reply(
            "<b>Добавить ссылку</b>\n"
            "<code>/link сайт https://tenderhack.ru</code>\n"
            "<code>/link правила https://…/rules</code>\n"
            "<code>/link чат https://t.me/…</code>\n"
            "<code>/link форма https://forms…</code>\n\n"
            f"<i>Виды: {esc(kinds)}</i>\n"
            "Убрать: <code>/link сайт -</code>"
        )
        return

    kind_word, _, url = args.partition(" ")
    kind = LINK_ALIASES.get(kind_word.strip().casefold())
    url = url.strip()
    if kind is None:
        # Bare URL with no kind - store it as a generic link.
        kind, url = LinkKind.OTHER, args

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return

        if url in {"-", "—", "нет"}:
            removed = await remove_link(session, hack, kind)
            if not removed:
                await message.reply("Такой ссылки и не было.")
                return
            await note_change(
                session, bot, hack, message,
                action="убрал ссылку", detail=f"<b>{kind.label}</b>",
                audit_action="link_remove", payload={"kind": kind.value},
            )
            return

        url = normalize_url(url)
        if not url.startswith(("http://", "https://", "tg://")):
            await message.reply("Это не похоже на ссылку.")
            return

        await set_link(session, hack, kind, url)
        await note_change(
            session, bot, hack, message,
            action="добавил ссылку",
            detail=f'{kind.emoji} <a href="{esc(url)}">{esc(kind.label)}</a>',
            audit_action="link_set", payload={"kind": kind.value, "url": url},
        )


@router.message(Command("status", "статус"))
async def cmd_status(message: Message, command: CommandObject, bot: Bot) -> None:
    """No argument recomputes from the clock; an argument forces a state."""
    if not await require_editor(message, bot):
        return
    wanted = (command.args or "").strip().casefold()

    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        events = await list_events(session, hack.id)
        old = hack.status

        if wanted:
            match = next(
                (s for s in HackStatus if s.value == wanted or s.label.startswith(wanted)), None
            )
            if match is None:
                options = ", ".join(f"{s.label}" for s in HackStatus)
                await message.reply(f"Не знаю такой статус. Есть: {esc(options)}")
                return
            new_status = match
        else:
            new_status = derive_status(hack, events)

        if new_status == old:
            await message.reply(f"Статус и так «{esc(old.label)}».")
            return

        await update_fields(session, hack, {"status": new_status})
        await note_change(
            session, bot, hack, message,
            action="сменил статус",
            detail=f"<s>{esc(old.label)}</s> → <b>{esc(new_status.label)}</b>",
            audit_action="status", payload={"from": old.value, "to": new_status.value},
        )


@router.message(Command("result", "итог", "итоги"))
async def cmd_result(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await require_editor(message, bot):
        return
    args = (command.args or "").strip()
    if not args:
        await message.reply(
            "<code>/result 2 место</code> или <code>/result 2 место | взяли приз за UX</code>"
        )
        return

    place, _, note = args.partition("|")
    async with session_scope() as session:
        hack = await require_hack(session, message)
        if hack is None:
            return
        await update_fields(
            session,
            hack,
            {
                "result_place": place.strip()[:80],
                "result_note": note.strip() or None,
                "status": HackStatus.FINISHED,
            },
        )
        await note_change(
            session, bot, hack, message,
            action="записал результат", detail=f"🏆 <b>{esc(place.strip())}</b>",
            audit_action="result", payload={"place": place.strip()},
        )


@router.message(Command("bind", "привязать"))
async def cmd_bind(message: Message, command: CommandObject, bot: Bot) -> None:
    """Move an existing hackathon into this topic, by slug or title fragment."""
    if not await require_editor(message, bot):
        return
    query = (command.args or "").strip().casefold()
    if not query:
        await message.reply(
            "Укажи, какой хакатон привязать к этой теме: <code>/bind тендерхак</code>\n"
            "Список — /hacks"
        )
        return

    async with session_scope() as session:
        from hackbot.domain.services.hackathons import list_hackathons

        candidates = await list_hackathons(session, chat_id=message.chat.id)
        match = next(
            (h for h in candidates if query in h.slug.casefold() or query in h.title.casefold()),
            None,
        )
        if match is None:
            await message.reply("Нет такого в этом чате. Список — /hacks")
            return

        await rebind_topic(session, match, message.chat.id, topic_id(message))
        await refresh_card(bot, session, match, force_new=True)
        await message.reply(f"Привязал <b>{esc(match.title)}</b> к этой теме.")
