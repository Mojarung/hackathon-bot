"""Bot and dispatcher assembly."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from hackbot.bot.middlewares.identity import IdentityMiddleware
from hackbot.bot.stickers import install as install_stickers
from hackbot.config import get_settings

log = logging.getLogger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("new", "завести хакатон в этой теме"),
    ("info", "карточка хакатона"),
    ("timeline", "все этапы"),
    ("add", "добавить этап"),
    ("move", "перенести этап"),
    ("rm", "удалить этап"),
    ("template", "стандартный набор этапов"),
    ("set", "задать поле: начало, конец, регистрация"),
    ("link", "добавить ссылку"),
    ("status", "пересчитать статус"),
    ("result", "записать итог"),
    ("join", "записаться в команду"),
    ("team", "состав команды"),
    ("who", "кто идёт на этап"),
    ("ping", "тегнуть команду"),
    ("doc", "приложить файл"),
    ("docs", "список документов"),
    ("repo", "репозиторий на GitHub"),
    ("ics", "выгрузить в календарь"),
    ("wisdom", "мудрость дня"),
    ("hacks", "все хакатоны чата"),
    ("whois", "что бот помнит о человеке"),
    ("forgetme", "стереть, что бот помнит о тебе"),
    ("help", "справка"),
]


def build_bot() -> Bot:
    settings = get_settings()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    install_stickers(bot)
    return bot


def build_dispatcher() -> Dispatcher:
    # Import here so a broken handler module surfaces at startup, not at import
    # of the config, and so the router order below is the single source of truth.
    from hackbot.bot.handlers import (
        agent,
        common,
        fun,
        intake,
        manage,
        media,
        queries,
        team,
        timeline,
        who,
    )

    dp = Dispatcher()
    # Outer, so the sender is recorded even for messages no handler wants.
    dp.message.outer_middleware(IdentityMiddleware())
    # Order matters: the free-form agent router is last, so every slash command
    # and every button wins the routing race before the LLM is ever consulted.
    for module in (common, intake, manage, timeline, team, media, fun, who, queries, agent):
        dp.include_router(module.router)
    return dp


async def publish_commands(bot: Bot) -> None:
    commands = [BotCommand(command=name, description=text) for name, text in COMMANDS]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(commands)
    except Exception as exc:
        log.warning("could not publish the command list: %s", exc)
