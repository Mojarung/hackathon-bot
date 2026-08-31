"""Who is in the chat: identity picked up passively, character learned on request.

Two different things live in this table and it helps to keep them apart. The
identity half (id, @username, display name, counters) is written for every
message the bot sees and is always true. The character half (`about`, `traits`,
`notes`) is only ever written when the agent is explicitly told something, so a
profile never fills up with the model's guesses about people.

Facts are merged rather than overwritten: someone who says "я бэкендер" today
and "учусь в вышке" tomorrow should end up with both, not the last one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.db.models import ChatUser
from hackbot.domain.timeutils import now_utc

# Per-field ceilings. A profile is injected into every prompt, so it has to stay
# small; the oldest fragments fall off the front when a field outgrows this.
FIELD_LIMIT = 400
NOTES_LIMIT = 600
# Raised along with dropping the "only people with facts" filter: entries are one
# short line each, and a roster that runs out before the quiet half of the chat
# would reintroduce the very blindness that filter caused.
ROSTER_LIMIT = 24
# Shorter fragments than this match too much to identify anybody.
FRAGMENT_MIN = 3

SELF_WORDS = frozenset(
    {"я", "меня", "мне", "себя", "себе", "мой", "моя", "моё", "мое", "self", "me"}
)


async def get(session: AsyncSession, tg_user_id: int) -> ChatUser | None:
    stmt = select(ChatUser).where(ChatUser.tg_user_id == tg_user_id)
    return (await session.scalars(stmt)).first()


async def touch(
    session: AsyncSession,
    *,
    tg_user_id: int,
    username: str | None = None,
    full_name: str = "",
    chat_id: int | None = None,
) -> ChatUser:
    """Record that this person exists and has just spoken. Never overwrites facts."""
    person = await get(session, tg_user_id)
    if person is None:
        person = ChatUser(
            tg_user_id=tg_user_id,
            username=username,
            full_name=full_name,
            chat_id=chat_id,
            # Column defaults land at flush time; this row is counted before that.
            messages=0,
        )
        session.add(person)
    else:
        # A renamed account should not keep answering to its old name, but an
        # empty value from a service message must not wipe a good one either.
        if username:
            person.username = username
        if full_name:
            person.full_name = full_name
        if chat_id and person.chat_id is None:
            person.chat_id = chat_id
    person.messages += 1
    person.last_seen = now_utc()
    await session.flush()
    return person


def _merge(old: str | None, new: str, limit: int) -> str:
    """Append a fact unless it is already there, keeping the newest within `limit`."""
    new = new.strip().strip(";,. ")
    if not new:
        return old or ""
    if old and new.casefold() in old.casefold():
        return old
    parts = [p.strip() for p in (old or "").split(";") if p.strip()]
    parts.append(new)
    while len(_join(parts)) > limit and len(parts) > 1:
        parts.pop(0)
    return _join(parts)[:limit]


def _join(parts: list[str]) -> str:
    return "; ".join(parts)


async def remember(
    session: AsyncSession,
    person: ChatUser,
    *,
    alias: str | None = None,
    about: str | None = None,
    traits: str | None = None,
    note: str | None = None,
) -> ChatUser:
    """Learn something about a person. Each field merges with what is already known."""
    if alias:
        person.alias = alias.strip()[:80]
    if about:
        person.about = _merge(person.about, about, FIELD_LIMIT)
    if traits:
        person.traits = _merge(person.traits, traits, FIELD_LIMIT)
    if note:
        person.notes = _merge(person.notes, note, NOTES_LIMIT)
    await session.flush()
    return person


async def forget(session: AsyncSession, person: ChatUser, field: str = "all") -> str:
    """Wipe learned facts. Identity stays: the bot still knows who is speaking."""
    field = field.strip().casefold()
    fields = {"about", "traits", "notes", "alias"} if field in {"all", "всё", "все"} else {field}
    wiped = []
    for name in ("alias", "about", "traits", "notes"):
        if name in fields and getattr(person, name):
            setattr(person, name, None)
            wiped.append(name)
    await session.flush()
    return ", ".join(wiped)


async def find(session: AsyncSession, needle: str, *, speaker_id: int | None = None
               ) -> ChatUser | None:
    """Resolve a person from whatever the model echoed back: id, @nick, or a name."""
    needle = needle.strip()
    if not needle:
        return None
    if speaker_id is not None and needle.casefold().strip("@") in SELF_WORDS:
        return await get(session, speaker_id)
    if needle.lstrip("-").isdigit():
        found = await get(session, int(needle))
        if found is not None:
            return found

    key = needle.lstrip("@").casefold()
    people = list(await session.scalars(select(ChatUser).order_by(ChatUser.last_seen.desc())))
    for person in people:
        if (person.username or "").casefold() == key:
            return person
    for person in people:
        if person.full_name.casefold() == key or (person.alias or "").casefold() == key:
            return person
    # Fragments last, and only when they point at exactly one person. Returning
    # the first of several matches is how the bot ends up confidently filing a
    # fact under the wrong participant, and a two-letter fragment matches half
    # the chat - a refusal the model can act on beats a silent wrong guess.
    if len(key) < FRAGMENT_MIN:
        return None
    matches = [
        person
        for person in people
        if key in f"{person.full_name} {person.alias or ''} {person.username or ''}".casefold()
    ]
    return matches[0] if len(matches) == 1 else None


async def roster(
    session: AsyncSession,
    *,
    chat_id: int | None = None,
    exclude: int | None = None,
    limit: int = ROSTER_LIMIT,
) -> list[ChatUser]:
    """Everyone the bot has seen in this chat, most recently active first.

    People it knows nothing about are listed too, and that is the point. An
    earlier version kept only those with learned facts, on the theory that bare
    names cost tokens and say nothing - which had it exactly backwards. Telling
    two participants apart *is* the job here, and a name with its @handle and id
    does that on its own; facts are a bonus on top. In the live chat that filter
    hid eight of the ten people in the room, so the model was left guessing who
    said what.
    """
    stmt = select(ChatUser).order_by(ChatUser.last_seen.desc())
    if chat_id is not None:
        stmt = stmt.where((ChatUser.chat_id == chat_id) | (ChatUser.chat_id.is_(None)))
    people = [p for p in await session.scalars(stmt) if p.tg_user_id != exclude]
    return people[:limit]
