"""Being addressed is not a reason to have no opinion.

The first version only reacted to messages nobody sent to the bot, so anyone
testing the feature the obvious way - by talking to it - saw nothing happen.
This pins the fix: a mention gets a reaction too.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SetMessageReaction, TelegramMethod
from aiogram.types import Chat, Message, MessageEntity, Update, User

from hackbot.bot import reactions
from hackbot.bot.handlers import agent as agent_handler

BOT_ID = 999
BOT_USERNAME = "test_bot"
CHAT_ID = -1001234
TOPIC_ID = 7


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[TelegramMethod] = []

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
    b._me = User(id=BOT_ID, is_bot=True, first_name="Качок", username=BOT_USERNAME)
    return b


@pytest.fixture(scope="module")
def dp() -> Dispatcher:
    d = Dispatcher()
    d.include_router(agent_handler.router)
    return d


@pytest.fixture(autouse=True)
def _clean_state():
    reactions._last_reacted.clear()
    yield
    reactions._last_reacted.clear()


def mention(text: str) -> Update:
    handle = f"@{BOT_USERNAME}"
    entity = MessageEntity(type="mention", offset=text.index(handle), length=len(handle))
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Кирилл"),
        message_thread_id=TOPIC_ID,
        is_topic_message=True,
        text=text,
        entities=[entity],
    )
    return Update(update_id=1, message=message)


@pytest.fixture
def patched(monkeypatch):
    """No model call may escape: both the agent and the reaction picker are fakes."""
    seen: dict[str, object] = {}

    async def fake_run_agent(deps, prompt, images=None, history=None):
        seen["prompt"] = prompt
        return "ответ", b"[]"

    async def fake_pick(lines, target):
        seen["reaction_target"] = target
        return "🤡"

    monkeypatch.setattr(agent_handler, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_handler, "llm_available", lambda: True)
    monkeypatch.setattr(reactions, "pick_reaction", fake_pick)
    monkeypatch.setattr(reactions.random, "random", lambda: 0.0)  # always roll in
    return seen


async def test_a_mention_gets_a_reaction_too(bot, dp, patched) -> None:
    await dp.feed_update(bot, mention(f"@{BOT_USERNAME} я задеплою в прод в пятницу"))
    await asyncio.sleep(0.05)  # the reaction is deliberately detached

    calls = [c for c in bot.session.calls if isinstance(c, SetMessageReaction)]
    assert len(calls) == 1, "на обращение к боту реакция тоже должна ставиться"
    assert [r.emoji for r in calls[0].reaction] == ["🤡"]
    assert calls[0].message_id == 1


async def test_the_reaction_judges_what_was_typed_not_the_mention(bot, dp, patched) -> None:
    await dp.feed_update(bot, mention(f"@{BOT_USERNAME} я задеплою в прод в пятницу"))
    await asyncio.sleep(0.05)

    assert patched["reaction_target"] == "я задеплою в прод в пятницу"
    assert BOT_USERNAME not in str(patched["reaction_target"])


async def test_cooldown_is_shared_with_the_uninvited_path(bot, dp, patched) -> None:
    """One reaction per topic per window, whoever put it there."""
    await dp.feed_update(bot, mention(f"@{BOT_USERNAME} первое сообщение сюда"))
    await asyncio.sleep(0.05)

    assert reactions.on_cooldown((CHAT_ID, TOPIC_ID), time.monotonic(), 60)
