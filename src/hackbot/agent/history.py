"""Keeping the conversation short enough to be cheap and long enough to be useful.

The model behind the bot holds 512K tokens, so overflowing the window is not a
real risk here - what a growing history actually costs is money and latency on
every single turn, because the whole thread is re-sent each time.

The old rule was "wipe everything past 60 000 characters", which traded a cost
problem for an amnesia problem: the bot would forget mid-conversation, right
after the longest and most involved exchanges. This trims by whole turns
instead, so what survives is always a coherent conversation.

Cutting at user-turn boundaries is not a stylistic choice. A model request that
carries a tool return whose matching tool call has been dropped is rejected by
the provider, and turn boundaries are the only places where no pair is split.
"""

from __future__ import annotations

import dataclasses
import logging

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

log = logging.getLogger(__name__)

# Twelve exchanges is far more than anyone refers back to in a chat, and still
# only a few thousand tokens once pictures are out of the way.
MAX_TURNS = 12
MAX_CHARS = 40_000

_IMAGE_NOTE = "[картинка, уже разобрана]"


def _is_turn_start(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def _strip_binary(message: ModelMessage) -> ModelMessage:
    """Replace image payloads with a placeholder.

    Re-uploading the same poster on every turn is the single most expensive
    thing this history can do, and the extraction pass has already read it.
    """
    if not isinstance(message, ModelRequest):
        return message
    parts = list(message.parts)
    changed = False
    for i, part in enumerate(parts):
        if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
            continue
        content = [
            _IMAGE_NOTE if isinstance(item, BinaryContent) else item for item in part.content
        ]
        if content != list(part.content):
            parts[i] = dataclasses.replace(part, content=content)
            changed = True
    return dataclasses.replace(message, parts=parts) if changed else message


def dump(messages: list[ModelMessage]) -> bytes:
    return ModelMessagesTypeAdapter.dump_json(messages)


def trim(
    messages: list[ModelMessage], *, max_turns: int = MAX_TURNS, max_chars: int = MAX_CHARS
) -> list[ModelMessage]:
    """The tail of the conversation, whole turns only, pictures stripped."""
    if not messages:
        return []
    kept = [_strip_binary(m) for m in messages]

    starts = [i for i, m in enumerate(kept) if _is_turn_start(m)]
    if len(starts) > max_turns:
        kept = kept[starts[-max_turns] :]

    # Then by size, in case a single turn carried a whole rulebook as text.
    while len(dump(kept)) > max_chars:
        starts = [i for i, m in enumerate(kept) if _is_turn_start(m)]
        if len(starts) < 2:
            # One oversized turn is all that is left; a fresh start beats
            # sending it forever.
            return []
        kept = kept[starts[1] :]
    return kept


def load(raw: str | None) -> list[ModelMessage]:
    """Parse a stored thread. Unreadable history is dropped, never raised."""
    if not raw:
        return []
    try:
        messages = ModelMessagesTypeAdapter.validate_json(raw)
    except Exception as exc:
        log.info("dropping unreadable agent history: %s", exc)
        return []
    return trim(messages)
