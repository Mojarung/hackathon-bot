"""A short in-memory tail of what has been said in each topic.

Kept in RAM on purpose. This is banter context - it is worth nothing an hour
later, and writing every message anyone types into the database would be a
storage and privacy cost with no payoff. A restart simply starts the buffer
empty and it fills up again within a few messages.

Both bounds matter: the per-topic deque keeps a single busy chat from growing
without limit, and the topic map is capped as well, because the bot can be added
to any number of chats and nothing else would ever evict them.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass

MAX_LINES = 20
MAX_TOPICS = 200
MAX_LINE_CHARS = 300

Key = tuple[int, int | None]


@dataclass(slots=True, frozen=True)
class Line:
    author: str
    user_id: int
    text: str
    is_bot: bool = False


_buffers: OrderedDict[Key, deque[Line]] = OrderedDict()


def record(
    chat_id: int,
    thread_id: int | None,
    *,
    author: str,
    user_id: int,
    text: str,
    is_bot: bool = False,
) -> None:
    text = " ".join(text.split())[:MAX_LINE_CHARS]
    if not text:
        return
    key: Key = (chat_id, thread_id)
    buffer = _buffers.get(key)
    if buffer is None:
        buffer = deque(maxlen=MAX_LINES)
        _buffers[key] = buffer
        while len(_buffers) > MAX_TOPICS:
            _buffers.popitem(last=False)  # drop the least recently touched topic
    else:
        _buffers.move_to_end(key)
    buffer.append(Line(author=author or "аноним", user_id=user_id, text=text, is_bot=is_bot))


def tail(chat_id: int, thread_id: int | None, limit: int) -> list[Line]:
    buffer = _buffers.get((chat_id, thread_id))
    if not buffer:
        return []
    return list(buffer)[-limit:]


def clear() -> None:
    _buffers.clear()
