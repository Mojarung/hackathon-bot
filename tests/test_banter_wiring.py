"""End-to-end wiring: a real dispatcher, a fake network, no LLM.

Unit tests cover the guards in isolation; this covers the thing they cannot -
that the router order, the filters and the outer middlewares actually put a
message where it belongs. Nothing here talks to Telegram or to a model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SendMessage, SetMessageReaction, TelegramMethod
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
    _reset()
    yield
    _reset()


def _reset() -> None:
    recent.clear()
    banter_handler._last_spoken.clear()
    banter_handler._last_reacted.clear()


def patch_everything(monkeypatch, *, reply: str | None = "влезаю", reaction: str | None = None):
    """Cut every outside dependency: no model call may escape a test."""
    spoken: list[str] = []
    reacted: list[str] = []

    async def fake_banter(lines, profiles, bot_name, bot_id):
        spoken.append(" | ".join(f"{line.author}: {line.text}" for line in lines))
        return reply

    async def fake_reaction(lines, target):
        reacted.append(target)
        return reaction

    monkeypatch.setattr(banter_handler, "make_banter", fake_banter)
    monkeypatch.setattr(banter_handler, "pick_reaction", fake_reaction)
    monkeypatch.setattr(banter_handler.random, "random", lambda: 0.0)  # always roll in
    monkeypatch.setattr(banter_handler, "llm_available", lambda: True)
    monkeypatch.setattr(
        banter_handler.get_settings(), "banter_everywhere", True, raising=False
    )
    return spoken, reacted


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
    spoken, _ = patch_everything(monkeypatch)

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
async def test_never_touches_a_message_with_a_file(
    bot, dp, monkeypatch, label: str, extra: dict
) -> None:
    """The user's hard rule: no attachment is touched unless the bot was addressed."""
    spoken, reacted = patch_everything(monkeypatch, reaction="🤡")

    await dp.feed_update(bot, make_message(1, "просто болтовня в чате"))
    await dp.feed_update(bot, make_message(2, "вот наши условия хакатона", **extra))
    await asyncio.sleep(0.05)  # let any detached reaction task run

    assert not spoken, f"{label}: бот не должен влезать в сообщение с вложением"
    # The plain first message may well earn a reaction - the file must not.
    assert "вот наши условия хакатона" not in reacted, f"{label}: и реакцию не ставит"
    reactions = [c for c in bot.session.calls if isinstance(c, SetMessageReaction)]
    assert all(c.message_id != 2 for c in reactions), f"{label}: реакция ушла на файл"


async def test_commands_and_short_replies_are_left_alone(bot, dp, monkeypatch) -> None:
    spoken, reacted = patch_everything(monkeypatch, reaction="🤡")

    await dp.feed_update(bot, make_message(1, "первое нормальное сообщение"))
    await dp.feed_update(bot, make_message(2, "/timeline"))
    await dp.feed_update(bot, make_message(3, "ок"))
    await asyncio.sleep(0.05)

    assert not spoken
    assert reacted == ["первое нормальное сообщение"], "команды и «ок» не трогаем"


async def test_reaction_lands_on_the_message_that_earned_it(bot, dp, monkeypatch) -> None:
    _, reacted = patch_everything(monkeypatch, reply=None, reaction="🤡")

    await dp.feed_update(bot, make_message(1, "я задеплою прямо в прод в пятницу"))
    await asyncio.sleep(0.05)

    assert reacted == ["я задеплою прямо в прод в пятницу"]
    calls = [c for c in bot.session.calls if isinstance(c, SetMessageReaction)]
    assert len(calls) == 1
    assert calls[0].chat_id == CHAT_ID
    assert calls[0].message_id == 1
    assert [r.emoji for r in calls[0].reaction] == ["🤡"]


async def test_model_declining_leaves_no_reaction(bot, dp, monkeypatch) -> None:
    patch_everything(monkeypatch, reply=None, reaction=None)

    await dp.feed_update(bot, make_message(1, "обычное рабочее сообщение без эмоций"))
    await asyncio.sleep(0.05)

    assert not [c for c in bot.session.calls if isinstance(c, SetMessageReaction)]
