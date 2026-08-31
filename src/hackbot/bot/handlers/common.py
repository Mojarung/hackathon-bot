"""Entry points: /start, /help, /hacks."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from hackbot.bot.handlers._helpers import find_hack
from hackbot.bot.utils import topic_id
from hackbot.db.base import session_scope
from hackbot.domain.services.hackathons import hack_tz, list_hackathons
from hackbot.domain.textutils import esc
from hackbot.domain.timeutils import fmt_date

router = Router(name="common")

HELP = """\
🤖 <b>Что умею</b>

Веду таймлайн хакатона в этой теме: слежу за дедлайнами, напоминаю, \
собираю документы и складываю всё в репозиторий.

<b>Начать</b>
/new Название — завести хакатон в этой теме
/new + фото условий — вытащу даты с афиши сам
/info — карточка хакатона
/timeline — все этапы

<b>Этапы</b>
/add Название 22.09 18:00 — добавить этап
/move id 19:30 — перенести
/rm id — удалить
/template — предложить стандартный набор этапов

<b>Данные</b>
/set поле значение — начало, конец, регистрация, город, тз, описание
/link сайт https://… — ссылки: сайт, правила, чат, канал, форма, таблица
/status — пересчитать статус
/result 2 место — записать итог

<b>Команда</b>
/join — записаться, /leave — выйти
/team — состав
/ping — тегнуть команду (единственное место, где я тегаю людей)

<b>Файлы и репозиторий</b>
/doc — приложить файл (или ответь на сообщение с файлом, упомянув меня)
/docs — список документов
/repo new — создать репозиторий в организации
/repo attach ссылка — прикрепить существующий
/repo — показать ссылку

<b>Календарь</b>
/ics — файл для импорта в календарь
/gcal — синхронизировать с общим Google Calendar
/drop Название — снести хакатон целиком

<b>Кто есть кто</b>
/whois — что я про тебя помню
/whois @ник — что помню про человека
/forgetme — стереть, что я про тебя записал

<b>Разное</b>
/wisdom — мудрость дня
/wisdom @кто-то — попросить мудрость у человека

<b>Или просто тегни меня</b>
И скажи словами: «перенеси защиту на 19:30», «добавь код-фриз в пятницу 18:00», \
«сколько осталось до сдачи», «кто не подтвердил защиту», «дай совет», «запомни: я бэкендер». \
Чего не хватит — переспрошу. Кинешь скриншот расписания — разберу и заполню сам.
"""


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with session_scope() as session:
        hack = await find_hack(session, message)
    if hack is not None:
        await message.reply(
            f"Веду <b>{esc(hack.title)}</b>.\n"
            f"/info — карточка, /timeline — этапы, /help — остальное."
        )
        return
    if message.chat.type == "private":
        await message.reply(
            "Веду таймлайны хакатонов в темах группового чата.\n\n"
            "Закинь меня в группу админом (нужны права закреплять сообщения "
            "и управлять темами), зайди в нужную тему и напиши <code>/new Название</code>.\n\n"
            "/help — что умею."
        )
        return
    await message.reply(HELP)


@router.message(Command("help", "помощь"))
async def cmd_help(message: Message) -> None:
    await message.reply(HELP, disable_web_page_preview=True)


@router.message(Command("hacks", "хаки"))
async def cmd_hacks(message: Message) -> None:
    """Every hackathon this chat has ever run - the team's track record."""
    async with session_scope() as session:
        hacks = await list_hackathons(session, chat_id=message.chat.id)
        if not hacks:
            await message.reply("В этом чате хакатонов ещё не было.")
            return

        current_thread = topic_id(message)
        lines = ["🗂 <b>Хакатоны этого чата</b>", ""]
        for hack in hacks:
            tz = hack_tz(hack)
            when = fmt_date(hack.starts_at, tz, with_year=True) if hack.starts_at else "даты нет"
            here = " ← эта тема" if hack.thread_id == current_thread else ""
            row = f"{hack.status.emoji} <b>{esc(hack.title)}</b> · {esc(when)}{here}"
            if hack.result_place:
                row += f"\n     🏆 {esc(hack.result_place)}"
            lines.append(row)
        await message.reply("\n".join(lines), disable_web_page_preview=True)
