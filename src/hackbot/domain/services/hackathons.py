"""Hackathon lifecycle: lookup by topic, creation, field updates, status math."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.config import get_settings
from hackbot.db.models import AuditLog, Event, Hackathon, Link
from hackbot.domain.enums import EventKind, HackStatus, LinkKind
from hackbot.domain.textutils import normalize_url, slugify
from hackbot.domain.timeutils import now_utc

# Fields the LLM agent and the wizard are both allowed to write.
EDITABLE_FIELDS = frozenset(
    {
        "title", "year", "organizer", "city", "is_online", "tz", "description",
        "starts_at", "ends_at", "reg_deadline", "status", "result_place", "result_note",
    }
)


def hack_tz(hack: Hackathon) -> ZoneInfo:
    try:
        return ZoneInfo(hack.tz)
    except Exception:
        return get_settings().default_tz


async def get_by_topic(
    session: AsyncSession, chat_id: int, thread_id: int | None, *, live_only: bool = True
) -> Hackathon | None:
    """The hackathon bound to this forum topic, newest first."""
    stmt = select(Hackathon).where(
        Hackathon.chat_id == chat_id, Hackathon.thread_id.is_not_distinct_from(thread_id)
    )
    if live_only:
        stmt = stmt.where(Hackathon.status != HackStatus.ARCHIVED)
    stmt = stmt.order_by(Hackathon.created_at.desc())
    return (await session.scalars(stmt)).first()


async def get_by_id(session: AsyncSession, hack_id: int) -> Hackathon | None:
    return await session.get(Hackathon, hack_id)


async def get_by_slug(session: AsyncSession, slug: str) -> Hackathon | None:
    stmt = select(Hackathon).where(Hackathon.slug == slug).order_by(Hackathon.created_at.desc())
    return (await session.scalars(stmt)).first()


async def list_hackathons(
    session: AsyncSession, *, chat_id: int | None = None, live_only: bool = False
) -> list[Hackathon]:
    stmt = select(Hackathon)
    if chat_id is not None:
        stmt = stmt.where(Hackathon.chat_id == chat_id)
    if live_only:
        stmt = stmt.where(Hackathon.status.not_in([HackStatus.FINISHED, HackStatus.ARCHIVED]))
    return list(await session.scalars(stmt.order_by(Hackathon.starts_at.desc().nulls_last())))


async def list_bound(session: AsyncSession) -> list[Hackathon]:
    """Everything the scheduler should keep an eye on."""
    stmt = select(Hackathon).where(
        Hackathon.status.not_in([HackStatus.ARCHIVED]),
    )
    return list(await session.scalars(stmt))


async def create(
    session: AsyncSession,
    *,
    title: str,
    chat_id: int,
    thread_id: int | None,
    created_by: int | None = None,
    **fields: Any,
) -> Hackathon:
    settings = get_settings()
    year = fields.pop("year", None) or now_utc().year
    hack = Hackathon(
        slug=fields.pop("slug", None) or slugify(title),
        title=title.strip(),
        year=int(year),
        chat_id=chat_id,
        thread_id=thread_id,
        created_by=created_by,
        tz=fields.pop("tz", None) or settings.tz_default,
    )
    for key, value in fields.items():
        if key in EDITABLE_FIELDS:
            setattr(hack, key, value)
    session.add(hack)
    await session.flush()
    return hack


async def update_fields(
    session: AsyncSession, hack: Hackathon, fields: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Apply a partial update and report what actually changed, old -> new."""
    changed: dict[str, tuple[Any, Any]] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        old = getattr(hack, key, None)
        if old == value:
            continue
        setattr(hack, key, value)
        changed[key] = (old, value)
    if changed:
        hack.updated_at = now_utc()
        await session.flush()
    return changed


async def rebind_topic(
    session: AsyncSession, hack: Hackathon, chat_id: int, thread_id: int | None
) -> None:
    hack.chat_id = chat_id
    hack.thread_id = thread_id
    hack.card_message_id = None  # the old pinned card lives in another topic
    await session.flush()


# ---------------------------------------------------------------- links


async def set_link(
    session: AsyncSession, hack: Hackathon, kind: LinkKind, url: str, title: str | None = None
) -> Link:
    """One link per kind, except OTHER which may repeat."""
    url = normalize_url(url)
    existing = None
    if kind is not LinkKind.OTHER:
        stmt = select(Link).where(Link.hackathon_id == hack.id, Link.kind == kind)
        existing = (await session.scalars(stmt)).first()
    if existing:
        existing.url = url
        existing.title = title
        await session.flush()
        return existing
    link = Link(hackathon_id=hack.id, kind=kind, url=url, title=title)
    session.add(link)
    await session.flush()
    return link


async def remove_link(session: AsyncSession, hack: Hackathon, kind: LinkKind) -> int:
    stmt = select(Link).where(Link.hackathon_id == hack.id, Link.kind == kind)
    rows = list(await session.scalars(stmt))
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


# ---------------------------------------------------------------- status math


def primary_deadline(hack: Hackathon, events: list[Event]) -> Event | None:
    """The countdown target: submission if there is one, else defense, else nothing."""
    for kind in (EventKind.SUBMISSION, EventKind.DEFENSE, EventKind.CODE_FREEZE):
        matches = [e for e in events if e.kind is kind]
        if matches:
            return min(matches, key=lambda e: e.starts_at)
    return None


def next_event(events: list[Event], now: datetime | None = None) -> Event | None:
    now = now or now_utc()
    upcoming = [e for e in events if e.starts_at > now]
    return min(upcoming, key=lambda e: e.starts_at) if upcoming else None


def current_event(events: list[Event], now: datetime | None = None) -> Event | None:
    """An event that is happening right now, if it declared an end time."""
    now = now or now_utc()
    live = [e for e in events if e.ends_at and e.starts_at <= now < e.ends_at]
    return min(live, key=lambda e: e.starts_at) if live else None


def progress_ratio(hack: Hackathon, now: datetime | None = None) -> float | None:
    if not hack.starts_at or not hack.ends_at:
        return None
    now = now or now_utc()
    total = (hack.ends_at - hack.starts_at).total_seconds()
    if total <= 0:
        return None
    return min(1.0, max(0.0, (now - hack.starts_at).total_seconds() / total))


def derive_status(hack: Hackathon, events: list[Event], now: datetime | None = None) -> HackStatus:
    """Status the clock implies. Terminal states are never auto-reversed."""
    now = now or now_utc()
    if hack.status in {HackStatus.FINISHED, HackStatus.ARCHIVED, HackStatus.DRAFT}:
        return hack.status

    results = [e for e in events if e.kind is EventKind.RESULTS]
    if results and now >= max(e.starts_at for e in results):
        return HackStatus.FINISHED

    if hack.ends_at and now >= hack.ends_at:
        defense = [e for e in events if e.kind is EventKind.DEFENSE]
        if defense and now < max(e.ends_at or e.starts_at for e in defense):
            return HackStatus.JUDGING
        return HackStatus.JUDGING if results else HackStatus.FINISHED

    if hack.starts_at and now >= hack.starts_at:
        submission = [e for e in events if e.kind is EventKind.SUBMISSION]
        if submission and now >= max(e.starts_at for e in submission):
            return HackStatus.JUDGING
        return HackStatus.RUNNING

    if hack.reg_deadline and now < hack.reg_deadline:
        return HackStatus.REGISTRATION

    return HackStatus.ANNOUNCED if hack.starts_at else hack.status


def missing_fields(hack: Hackathon, events: list[Event]) -> list[str]:
    """Human-readable list of what still needs filling in."""
    gaps: list[str] = []
    if not hack.starts_at:
        gaps.append("дата начала")
    if not hack.ends_at:
        gaps.append("дата окончания")
    if not primary_deadline(hack, events):
        gaps.append("дедлайн сдачи решения")
    if not any(e.kind is EventKind.DEFENSE for e in events):
        gaps.append("защита")
    if not any(e.kind is EventKind.RESULTS for e in events):
        gaps.append("объявление результатов")
    if not hack.links:
        gaps.append("ссылки")
    return gaps


# ---------------------------------------------------------------- audit


async def audit(
    session: AsyncSession,
    hack: Hackathon | None,
    *,
    action: str,
    actor: str = "",
    tg_user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        hackathon_id=hack.id if hack else None,
        tg_user_id=tg_user_id,
        actor=actor,
        action=action,
        details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
    )
    session.add(entry)
    await session.flush()
    return entry
