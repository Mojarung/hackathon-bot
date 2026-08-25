"""End-to-end wiring: a real dispatcher, a fake network, no LLM.

Unit tests cover the guards in isolation; this covers the thing they cannot -
that the router order, the filters and the outer middlewares actually put a
message where it belongs. Nothing here talks to Telegram or to a model.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Document, Message, PhotoSize, Update, User

from hackbot.bot import recent
from hackbot.bot.handlers import banter as banter_handler
from hackbot.bot.middlewares.recent import RecentMiddleware

BOT_ID = 999
CHAT_ID = -1001234
TOPIC_ID = 7


class FakeSession:
    """Swallows every outgoing call and remembers it."""

    def __init__(self) -> None:
        self.calls: list[TelegramMethod] = []

    # `timeout` is aiogram's session signature, not ours.
    async def __call__(self, bot: Bot, method: TelegramMethod, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout=None):  # noqa: ASYNC109
        return await self(bot, method, timeout)

    async def close(self) -> None:
        return None


@pytest.fixture
def bot() -> Bot:
    b = Bot(
        token="123456:TESTTESTTESTTESTTESTTESTTESTTESTTES",
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    b.session = FakeSession()
    b._me = User(id=BOT_ID, is_bot=True, first_name="Качок", username="test_bot")
    return b


# Module scope: a Router instance can only ever be attached to one Dispatcher,
# and the handler module owns a single global one.
@pytest.fixture(scope="module")
def dp() -> Dispatcher:
    d = Dispatcher()
    d.message.outer_middleware(RecentMiddleware())
    d.include_router(banter_handler.router)
    return d


@pytest.fixture(autouse=True)
def _clean_state():
    recent.clear()
    banter_handler._last_spoken.clear()
    yield
    recent.clear()
    banter_handler._last_spoken.clear()


def make_message(update_id: int, text: str, **extra) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Кирилл"),
        message_thread_id=TOPIC_ID,
        is_topic_message=True,
        text=text,
        **extra,
    )
    return Update(update_id=update_id, message=message)


async def test_plain_chatter_is_recorded_and_can_trigger(bot, dp, monkeypatch) -> None:
    spoken: list[str] = []

    async def fake_banter(lines, profiles, bot_name, bot_id):
        spoken.append(" | ".join(f"{line.author}: {line.text}" for line in lines))
        return "влезаю"

    monkeypatch.setattr(banter_handler, "make_banter", fake_banter)
    monkeypatch.setattr(banter_handler.random, "random", lambda: 0.0)   # always roll in
    monkeypatch.setattr(banter_handler, "llm_available", lambda: True)
    monkeypatch.setattr(
        banter_handler.get_settings(), "banter_everywhere", True, raising=False
    )

    await dp.feed_update(bot, make_message(1, "первое сообщение в теме"))
    assert not spoken, "одной реплики мало, чтобы влезать"

    await dp.feed_update(bot, make_message(2, "второе сообщение, уже разговор"))
    assert spoken, "на втором сообщении должен влезть"
    assert "первое сообщение в теме" in spoken[0], "контекст должен включать прошлые реплики"

    sent = [c for c in bot.session.calls if isinstance(c, SendMessage)]
    assert len(sent) == 1
    assert sent[0].text == "влезаю"
    assert sent[0].message_thread_id == TOPIC_ID, "ответ должен уйти в ту же тему"

    # Its own line goes into the buffer so the next butt-in sees what it said.
    assert [line.text for line in recent.tail(CHAT_ID, TOPIC_ID, 5)][-1] == "влезаю"


@pytest.mark.parametrize(
    ("label", "extra"),
    [
        ("документ", {"document": Document(file_id="f", file_unique_id="u")}),
        (
            "фото",
            {"photo": [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]},
        ),
    ],
)
async def test_never_butts_in_on_a_message_with_a_file(
    bot, dp, monkeypatch, label: str, extra: dict
) -> None:
    """The user's hard rule: no attachment is touched unless the bot was addressed."""
    called = False

    async def fake_banter(*args, **kwargs):
        nonlocal called
        called = True
        return "не должно случиться"

    monkeypatch.setattr(banter_handler, "make_banter", fake_banter)
    monkeypatch.setattr(banter_handler.random, "random", lambda: 0.0)
    monkeypatch.setattr(banter_handler, "llm_available", lambda: True)
    monkeypatch.setattr(
        banter_handler.get_settings(), "banter_everywhere", True, raising=False
    )

    await dp.feed_update(bot, make_message(1, "просто болтовня в чате"))
    await dp.feed_update(bot, make_message(2, "вот наши условия хакатона", **extra))

    assert not called, f"{label}: бот не должен влезать в сообщение с вложением"
    assert not [c for c in bot.session.calls if isinstance(c, SendMessage)]


async def test_commands_and_short_replies_are_left_alone(bot, dp, monkeypatch) -> None:
    called = False

    async def fake_banter(*args, **kwargs):
        nonlocal called
        called = True
        return "нет"

    monkeypatch.setattr(banter_handler, "make_banter", fake_banter)
    monkeypatch.setattr(banter_handler.random, "random", lambda: 0.0)
    monkeypatch.setattr(banter_handler, "llm_available", lambda: True)
    monkeypatch.setattr(
        banter_handler.get_settings(), "banter_everywhere", True, raising=False
    )

    await dp.feed_update(bot, make_message(1, "первое нормальное сообщение"))
    await dp.feed_update(bot, make_message(2, "/timeline"))
    await dp.feed_update(bot, make_message(3, "ок"))

    assert not called


async def test_caption_of_a_file_never_reaches_the_buffer(bot, dp) -> None:
    """The rule holds one message later too, not just on the media message itself."""
    await dp.feed_update(bot, make_message(1, "обычная реплика в чате"))
    await dp.feed_update(
        bot,
        make_message(
            2,
            "вот условия хакатона, дедлайн 20 сентября",
            document=Document(file_id="f", file_unique_id="u"),
        ),
    )

    stored = [line.text for line in recent.tail(CHAT_ID, TOPIC_ID, 10)]
    assert stored == ["обычная реплика в чате"]
    assert not any("дедлайн" in line for line in stored)
