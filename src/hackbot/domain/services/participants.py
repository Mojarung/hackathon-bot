"""Team roster and attendance confirmations."""

from __future__ import annotations

import zlib
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.db.models import Event, Hackathon, Participant, Rsvp
from hackbot.domain.enums import RsvpStatus
from hackbot.domain.timeutils import now_utc

ROLES = (
    "капитан", "бэкенд", "фронтенд", "ML", "аналитик", "дизайн", "презентация", "девопс",
)


async def list_participants(
    session: AsyncSession, hackathon_id: int, *, active_only: bool = True
) -> list[Participant]:
    stmt = select(Participant).where(Participant.hackathon_id == hackathon_id)
    if active_only:
        stmt = stmt.where(Participant.is_active.is_(True))
    stmt = stmt.order_by(Participant.is_captain.desc(), Participant.joined_at)
    return list(await session.scalars(stmt))


async def get_participant(
    session: AsyncSession, hackathon_id: int, tg_user_id: int
) -> Participant | None:
    stmt = select(Participant).where(
        Participant.hackathon_id == hackathon_id, Participant.tg_user_id == tg_user_id
    )
    return (await session.scalars(stmt)).first()


def placeholder_id(name: str) -> int:
    """A stable negative id for someone added by name.

    Telegram will not resolve a @username to a user id unless that person has
    interacted with the bot, but the roster still has to hold them so roles and
    attendance make sense. Negative ids can never collide with real ones, and
    `join` upgrades the row in place once the real person shows up.
    """
    digest = zlib.crc32(name.casefold().strip().lstrip("@").encode("utf-8"))
    return -(digest & 0x7FFFFFFF) - 1


async def find_by_name(
    session: AsyncSession, hackathon_id: int, needle: str
) -> Participant | None:
    """Match a person by @username, display name or a fragment of either."""
    needle = needle.strip().lstrip("@").casefold()
    if not needle:
        return None
    people = await list_participants(session, hackathon_id, active_only=False)
    for person in people:
        if (person.username or "").casefold() == needle:
            return person
    for person in people:
        if person.full_name.casefold() == needle:
            return person
    for person in people:
        if needle in person.full_name.casefold() or needle in (person.username or "").casefold():
            return person
    return None


async def add_by_name(
    session: AsyncSession, hack: Hackathon, name: str, role: str | None = None
) -> tuple[Participant, bool]:
    """Add someone the bot has never seen speak. Returns (participant, created)."""
    name = name.strip()
    existing = await find_by_name(session, hack.id, name)
    if existing is not None:
        if role:
            existing.role = role
        existing.is_active = True
        await session.flush()
        return existing, False

    is_handle = name.startswith("@")
    participant = Participant(
        hackathon_id=hack.id,
        tg_user_id=placeholder_id(name),
        username=name.lstrip("@") if is_handle else None,
        full_name="" if is_handle else name,
        role=role,
    )
    session.add(participant)
    await session.flush()
    return participant, True


async def remove(session: AsyncSession, hack: Hackathon, needle: str) -> Participant | None:
    person = await find_by_name(session, hack.id, needle)
    if person is None:
        return None
    await session.delete(person)
    await session.flush()
    return person


async def join(
    session: AsyncSession,
    hack: Hackathon,
    *,
    tg_user_id: int,
    username: str | None,
    full_name: str,
    role: str | None = None,
) -> tuple[Participant, bool]:
    """Idempotent. Returns (participant, created)."""
    existing = await get_participant(session, hack.id, tg_user_id)

    # Someone added by name earlier is upgraded rather than duplicated.
    if existing is None and username:
        stub = await find_by_name(session, hack.id, username)
        if stub is not None and stub.is_placeholder:
            stub.tg_user_id = tg_user_id
            stub.full_name = full_name or stub.full_name
            stub.is_active = True
            if role:
                stub.role = role
            await session.flush()
            return stub, True

    if existing:
        existing.username = username
        existing.full_name = full_name or existing.full_name
        if role:
            existing.role = role
        was_inactive = not existing.is_active
        existing.is_active = True
        await session.flush()
        return existing, was_inactive

    participant = Participant(
        hackathon_id=hack.id,
        tg_user_id=tg_user_id,
        username=username,
        full_name=full_name,
        role=role,
        is_captain=not await list_participants(session, hack.id),
    )
    session.add(participant)
    await session.flush()
    return participant, True


async def leave(session: AsyncSession, hack: Hackathon, tg_user_id: int) -> bool:
    participant = await get_participant(session, hack.id, tg_user_id)
    if not participant or not participant.is_active:
        return False
    participant.is_active = False
    await session.flush()
    return True


async def set_role(
    session: AsyncSession, hack: Hackathon, tg_user_id: int, role: str | None
) -> Participant | None:
    participant = await get_participant(session, hack.id, tg_user_id)
    if participant:
        participant.role = role
        await session.flush()
    return participant


async def set_captain(session: AsyncSession, hack: Hackathon, tg_user_id: int) -> bool:
    people = await list_participants(session, hack.id)
    found = False
    for person in people:
        should_be = person.tg_user_id == tg_user_id
        found = found or should_be
        person.is_captain = should_be
    await session.flush()
    return found


# ---------------------------------------------------------------- rsvp


@dataclass(slots=True)
class RsvpSummary:
    counts: Counter[RsvpStatus] = field(default_factory=Counter)
    by_status: dict[RsvpStatus, list[Participant]] = field(default_factory=dict)
    pending: list[Participant] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return sum(self.counts.values())

    @property
    def going(self) -> int:
        return self.counts[RsvpStatus.YES] + self.counts[RsvpStatus.LATE]


async def set_rsvp(
    session: AsyncSession, event: Event, tg_user_id: int, status: RsvpStatus
) -> Rsvp:
    stmt = select(Rsvp).where(Rsvp.event_id == event.id, Rsvp.tg_user_id == tg_user_id)
    existing = (await session.scalars(stmt)).first()
    if existing:
        existing.status = status
        existing.updated_at = now_utc()
        await session.flush()
        return existing
    row = Rsvp(event_id=event.id, tg_user_id=tg_user_id, status=status)
    session.add(row)
    await session.flush()
    return row


async def rsvp_summary(session: AsyncSession, event: Event) -> RsvpSummary:
    people = await list_participants(session, event.hackathon_id)
    by_user = {p.tg_user_id: p for p in people}

    stmt = select(Rsvp).where(Rsvp.event_id == event.id)
    rows = list(await session.scalars(stmt))

    summary = RsvpSummary()
    answered_ids: set[int] = set()
    for row in rows:
        person = by_user.get(row.tg_user_id)
        if person is None:
            continue  # answered but has since left the team
        summary.counts[row.status] += 1
        summary.by_status.setdefault(row.status, []).append(person)
        answered_ids.add(row.tg_user_id)

    summary.pending = [p for p in people if p.tg_user_id not in answered_ids]
    return summary
