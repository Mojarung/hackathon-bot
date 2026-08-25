"""Recognising that a message is addressed to the bot.

The offsets are the whole story here. Telegram counts entity positions in UTF-16
code units; Python strings count characters. Every emoji outside the BMP is two
units and one character, so a naive slice drifts by one per emoji and the
mention quietly stops matching.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User

from hackbot.bot.filters import mentions_bot

USERNAME = "kachok_mojarung_bot"


class FakeBot:
    async def me(self) -> User:
        return User(id=999, is_bot=True, first_name="Качок", username=USERNAME)


def utf16_offset(text: str, needle: str) -> int:
    """Where Telegram would say the entity starts."""
    return text.encode("utf-16-le").index(needle.encode("utf-16-le")) // 2


def message_with_mention(text: str, *, as_caption: bool = False) -> Message:
    mention = f"@{USERNAME}"
    entity = MessageEntity(
        type="mention", offset=utf16_offset(text, mention), length=len(mention)
    )
    common = dict(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Кирилл"),
    )
    if as_caption:
        return Message(
            **common,
            photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
            caption=text,
            caption_entities=[entity],
        )
    return Message(**common, text=text, entities=[entity])


@pytest.mark.parametrize(
    "text",
    [
        f"@{USERNAME} глянь афишу",
        f"🔥 @{USERNAME} глянь афишу",
        f"🔥🔥 @{USERNAME} глянь афишу",
        f"смотри 👀 сюда @{USERNAME} когда дедлайн",
        f"🇷🇺👨‍👩‍👧‍👦 @{USERNAME}",
        f"привет 👋 @{USERNAME}",
    ],
)
async def test_mention_survives_any_emoji_before_it(text: str) -> None:
    assert await mentions_bot(message_with_mention(text), FakeBot()) is True


async def test_mention_in_a_photo_caption_counts() -> None:
    """Media put entities in caption_entities, and the offsets drift the same way."""
    text = f"🔥 @{USERNAME} разбери расписание"
    assert await mentions_bot(message_with_mention(text, as_caption=True), FakeBot()) is True


async def test_someone_elses_mention_is_not_ours() -> None:
    text = "@other_bot сделай что-нибудь"
    entity = MessageEntity(type="mention", offset=0, length=len("@other_bot"))
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Кирилл"),
        text=text,
        entities=[entity],
    )
    assert await mentions_bot(message, FakeBot()) is False


async def test_plain_text_without_entities_is_not_a_mention() -> None:
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Кирилл"),
        text=f"обсуждали @{USERNAME} вчера",
    )
    assert await mentions_bot(message, FakeBot()) is False
