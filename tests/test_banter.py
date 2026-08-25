"""The unprompted reply: what it remembers, what it refuses to say, what it skips."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hackbot.agent.banter import MAX_REPLY_CHARS, build_prompt, clean
from hackbot.bot import recent
from hackbot.bot.utils import has_attachment


@pytest.fixture(autouse=True)
def _clean_buffer():
    recent.clear()
    yield
    recent.clear()


# ---------------------------------------------------------------- buffer


def test_records_and_returns_the_tail() -> None:
    for i in range(5):
        recent.record(-100, 7, author="Кирилл", user_id=1, text=f"строка {i}")
    tail = recent.tail(-100, 7, 3)
    assert [line.text for line in tail] == ["строка 2", "строка 3", "строка 4"]


def test_topics_do_not_bleed_into_each_other() -> None:
    recent.record(-100, 7, author="Кирилл", user_id=1, text="про хакатон")
    recent.record(-100, 8, author="Саня", user_id=2, text="про другое")
    assert [line.text for line in recent.tail(-100, 7, 5)] == ["про хакатон"]
    assert [line.text for line in recent.tail(-100, 8, 5)] == ["про другое"]
    assert recent.tail(-999, None, 5) == []


def test_buffer_is_bounded_per_topic() -> None:
    for i in range(recent.MAX_LINES * 3):
        recent.record(-100, None, author="Кирилл", user_id=1, text=f"строка {i}")
    assert len(recent.tail(-100, None, 1000)) == recent.MAX_LINES


def test_oldest_topic_is_evicted_when_too_many_are_tracked() -> None:
    for chat in range(recent.MAX_TOPICS + 5):
        recent.record(chat, None, author="кто-то", user_id=1, text="привет всем тут")
    assert recent.tail(0, None, 5) == []          # the first chat fell off
    assert recent.tail(recent.MAX_TOPICS + 4, None, 5)


def test_recording_touches_a_topic_so_it_survives_eviction() -> None:
    recent.record(1, None, author="Кирилл", user_id=1, text="первое сообщение")
    for chat in range(10, 10 + recent.MAX_TOPICS - 1):
        recent.record(chat, None, author="кто-то", user_id=2, text="шум в другом чате")
    recent.record(1, None, author="Кирилл", user_id=1, text="ещё сообщение")
    for chat in range(1000, 1000 + 5):
        recent.record(chat, None, author="кто-то", user_id=2, text="ещё шум")
    assert recent.tail(1, None, 5), "недавно активный чат не должен вытесняться"


def test_blank_text_is_not_recorded() -> None:
    recent.record(-100, None, author="Кирилл", user_id=1, text="   \n  ")
    assert recent.tail(-100, None, 5) == []


def test_long_lines_are_truncated() -> None:
    recent.record(-100, None, author="Кирилл", user_id=1, text="я" * 5000)
    assert len(recent.tail(-100, None, 1)[0].text) == recent.MAX_LINE_CHARS


# ---------------------------------------------------------------- output


@pytest.mark.parametrize(
    "raw",
    ["ПРОПУСК", "пропуск", "  ПРОПУСК  ", "", "   ", "я" * (MAX_REPLY_CHARS + 1)],
)
def test_refuses_to_speak(raw: str) -> None:
    assert clean(raw) is None


def test_strips_the_transcript_prefix_models_like_to_add() -> None:
    assert clean("ТЫ: да ладно") == "да ладно"
    assert clean("ты:   ну и хуйня  ") == "ну и хуйня"


def test_collapses_whitespace() -> None:
    assert clean("две\n\nстроки   в   одну") == "две строки в одну"


# ---------------------------------------------------------------- prompt


def test_prompt_marks_the_bots_own_lines_and_carries_profiles() -> None:
    lines = [
        recent.Line(author="Кирилл", user_id=1, text="го писать бэк"),
        recent.Line(author="Качок", user_id=99, text="ага, конечно", is_bot=True),
    ]
    prompt = build_prompt(lines, {1: "Кирилл — бэкендер"}, "Качок", 99)
    assert "Кирилл: го писать бэк" in prompt
    assert "ТЫ: ага, конечно" in prompt
    assert "Кирилл — бэкендер" in prompt


def test_another_bot_is_not_mistaken_for_this_one() -> None:
    """A second bot in the chat writes as itself, never as «ТЫ»."""
    lines = [
        recent.Line(author="ЧужойБот", user_id=42, text="напоминаю про дедлайн", is_bot=True),
        recent.Line(author="Качок", user_id=99, text="да помню я", is_bot=True),
    ]
    prompt = build_prompt(lines, {}, "Качок", 99)
    assert "ЧужойБот: напоминаю про дедлайн" in prompt
    assert "ТЫ: да помню я" in prompt


def test_prompt_without_profiles_has_no_empty_section() -> None:
    lines = [recent.Line(author="Кирилл", user_id=1, text="привет")]
    assert "знаешь про этих людей" not in build_prompt(lines, {}, "Качок", 99)


# ---------------------------------------------------------------- guards


def _message(**kwargs) -> SimpleNamespace:
    fields = dict.fromkeys(
        (
            "photo", "document", "video", "animation", "audio", "voice",
            "video_note", "sticker", "paid_media",
        )
    )
    return SimpleNamespace(**(fields | kwargs))


@pytest.mark.parametrize(
    "field",
    [
        "photo", "document", "video", "animation", "audio", "voice",
        "video_note", "sticker", "paid_media",
    ],
)
def test_any_attachment_stops_it(field: str) -> None:
    assert has_attachment(_message(**{field: object()})) is True


def test_plain_text_message_passes() -> None:
    assert has_attachment(_message()) is False


def test_cooldown_window() -> None:
    from hackbot.bot.handlers import banter

    key = (-100, 7)
    clock = banter._last_spoken
    clock.clear()
    try:
        assert banter._on_cooldown(clock, key, 1000.0, 300) is False   # never spoken here
        clock[key] = 1000.0
        assert banter._on_cooldown(clock, key, 1100.0, 300) is True    # 100s later, quiet
        assert banter._on_cooldown(clock, key, 1301.0, 300) is False   # past the window
    finally:
        clock.clear()


def test_reactions_and_replies_have_separate_cooldowns() -> None:
    """A reaction must not silence a butt-in, and the other way round."""
    from hackbot.bot.handlers import banter

    key = (-100, 7)
    banter._last_spoken.clear()
    banter._last_reacted.clear()
    try:
        banter._claim(banter._last_reacted, key, 1000.0)
        assert banter._on_cooldown(banter._last_reacted, key, 1010.0, 60) is True
        assert banter._on_cooldown(banter._last_spoken, key, 1010.0, 300) is False
    finally:
        banter._last_spoken.clear()
        banter._last_reacted.clear()


def test_claim_is_bounded_and_evicts_the_oldest_topic() -> None:
    from hackbot.bot.handlers import banter

    banter._last_spoken.clear()
    try:
        for chat in range(banter.MAX_TRACKED_TOPICS + 10):
            banter._claim(banter._last_spoken, (chat, None), float(chat))
        assert len(banter._last_spoken) == banter.MAX_TRACKED_TOPICS
        assert (0, None) not in banter._last_spoken
        assert (banter.MAX_TRACKED_TOPICS + 9, None) in banter._last_spoken
    finally:
        banter._last_spoken.clear()


def test_releasing_a_slot_lets_the_next_message_try_again() -> None:
    from hackbot.bot.handlers import banter

    key = (-100, 7)
    banter._last_spoken.clear()
    try:
        banter._claim(banter._last_spoken, key, 1000.0)
        assert banter._on_cooldown(banter._last_spoken, key, 1010.0, 300) is True
        banter._release(banter._last_spoken, key)
        assert banter._on_cooldown(banter._last_spoken, key, 1010.0, 300) is False
    finally:
        banter._last_spoken.clear()
