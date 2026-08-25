"""History trimming: keep whole turns, drop pictures, never split a tool pair."""

from __future__ import annotations

from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from hackbot.agent import history


def turn(n: int, *, with_tool: bool = False) -> list:
    """One user turn: the question, an optional tool round trip, the answer."""
    out: list = [ModelRequest(parts=[UserPromptPart(content=f"вопрос {n}")])]
    if with_tool:
        out += [
            ModelResponse(
                parts=[ToolCallPart(tool_name="get_state", args={}, tool_call_id=f"c{n}")]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="get_state", content="ok", tool_call_id=f"c{n}")]
            ),
        ]
    out.append(ModelResponse(parts=[TextPart(content=f"ответ {n}")]))
    return out


def user_prompts(messages: list) -> list[str]:
    return [
        part.content
        for msg in messages
        if isinstance(msg, ModelRequest)
        for part in msg.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def test_short_history_is_untouched() -> None:
    messages = turn(1) + turn(2)
    assert history.trim(messages) == messages


def test_keeps_only_the_last_turns() -> None:
    messages = [m for i in range(20) for m in turn(i)]
    kept = history.trim(messages, max_turns=3)
    assert user_prompts(kept) == ["вопрос 17", "вопрос 18", "вопрос 19"]


def test_cut_lands_on_a_turn_boundary_so_tool_pairs_survive() -> None:
    messages = [m for i in range(6) for m in turn(i, with_tool=True)]
    kept = history.trim(messages, max_turns=2)

    calls = {
        part.tool_call_id
        for msg in kept if isinstance(msg, ModelResponse)
        for part in msg.parts if isinstance(part, ToolCallPart)
    }
    returns = {
        part.tool_call_id
        for msg in kept if isinstance(msg, ModelRequest)
        for part in msg.parts if isinstance(part, ToolReturnPart)
    }
    assert calls == returns, "orphaned tool call or return - the provider rejects that"
    assert isinstance(kept[0], ModelRequest)


def test_pictures_are_replaced_by_a_placeholder() -> None:
    picture = BinaryContent(data=b"\xff\xd8\xff" + b"x" * 5000, media_type="image/jpeg")
    messages = [
        ModelRequest(parts=[UserPromptPart(content=["вот афиша", picture])]),
        ModelResponse(parts=[TextPart(content="разобрал")]),
    ]
    kept = history.trim(messages)
    dumped = history.dump(kept)
    assert b"image/jpeg" not in dumped
    assert len(dumped) < 1000
    assert history._IMAGE_NOTE in str(kept[0].parts[0].content)


def test_oversized_history_falls_back_to_whole_turns() -> None:
    fat = "ю" * 5000
    messages = [
        m
        for i in range(10)
        for m in (
            ModelRequest(parts=[UserPromptPart(content=f"{fat} {i}")]),
            ModelResponse(parts=[TextPart(content=f"ответ {i}")]),
        )
    ]
    kept = history.trim(messages, max_chars=20_000)
    assert len(history.dump(kept)) <= 20_000
    assert user_prompts(kept), "a size cap must not empty a history of ordinary turns"
    assert user_prompts(kept)[-1].endswith(" 9")


def test_single_giant_turn_is_dropped_rather_than_resent_forever() -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart(content="я" * 100_000)]),
        ModelResponse(parts=[TextPart(content="ага")]),
    ]
    assert history.trim(messages) == []


def test_unreadable_stored_history_is_dropped_not_raised() -> None:
    assert history.load(None) == []
    assert history.load("") == []
    assert history.load("{not json") == []
