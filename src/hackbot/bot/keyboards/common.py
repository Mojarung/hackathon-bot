"""Inline keyboards. The card keyboard doubles as the bot's main menu."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from hackbot.bot.callbacks import CardCb, ConfirmCb, EventCb, PickCb, RsvpCb
from hackbot.config import get_settings
from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import RsvpStatus
from hackbot.domain.services.participants import ROLES


def card_kb(hack: Hackathon, *, has_docs: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Таймлайн", callback_data=CardCb(action="timeline", hack_id=hack.id))
    kb.button(text="👥 Команда", callback_data=CardCb(action="team", hack_id=hack.id))
    kb.button(text="ℹ️ Инфо", callback_data=CardCb(action="info", hack_id=hack.id))
    if hack.links:
        kb.button(text="🔗 Ссылки", callback_data=CardCb(action="links", hack_id=hack.id))
    if has_docs:
        kb.button(text="📎 Доки", callback_data=CardCb(action="docs", hack_id=hack.id))
    kb.button(text="📥 В календарь", callback_data=CardCb(action="ics", hack_id=hack.id))

    settings = get_settings()
    if settings.web_public_url:
        kb.button(text="🌐 Веб", url=settings.public_url(f"h/{hack.slug}"))
    if hack.github_url:
        kb.button(text="🐙 Репо", url=hack.github_url)

    kb.adjust(3, 3, 2)
    return kb.as_markup()


def rsvp_kb(event: Event) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for status in (RsvpStatus.YES, RsvpStatus.LATE, RsvpStatus.NO):
        kb.button(
            text=f"{status.emoji} {status.label.capitalize()}",
            callback_data=RsvpCb(event_id=event.id, status=status.value),
        )
    kb.button(text="👀 Кто идёт", callback_data=EventCb(action="who", event_id=event.id))
    kb.adjust(3, 1)
    return kb.as_markup()


def event_kb(event: Event, *, with_rsvp: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if with_rsvp and event.needs_rsvp:
        for status in (RsvpStatus.YES, RsvpStatus.LATE, RsvpStatus.NO):
            kb.button(
                text=status.emoji,
                callback_data=RsvpCb(event_id=event.id, status=status.value),
            )
        kb.adjust(3)
    if event.url:
        kb.row(InlineKeyboardButton(text="🔗 Открыть", url=event.url))
    return kb.as_markup()


def confirm_kb(
    token: str, *, yes: str = "✅ Сохранить", no: str = "✖️ Отмена"
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=yes, callback_data=ConfirmCb(action="apply", token=token))
    kb.button(text=no, callback_data=ConfirmCb(action="drop", token=token))
    kb.adjust(2)
    return kb.as_markup()


def roles_kb(hack_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for role in ROLES:
        kb.button(text=role, callback_data=PickCb(kind="role", value=role, ref=hack_id))
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


def link_url_kb(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])
