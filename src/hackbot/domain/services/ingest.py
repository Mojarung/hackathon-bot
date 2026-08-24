"""Bridge between what the LLM produced and what the domain will accept.

The model is good at reading a poster and bad at edge cases, so everything it
returns passes through deterministic repair here before it can touch the
database. Prompt wording is a hint; this module is the guarantee.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.agent.schemas import ExtractedHackathon
from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import EventKind, LinkKind
from hackbot.domain.services.events import add_event, list_events, update_event
from hackbot.domain.services.hackathons import set_link, update_fields
from hackbot.domain.textutils import normalize_url, slugify
from hackbot.domain.timeutils import parse_iso, to_local

log = logging.getLogger(__name__)

# Kinds that describe a moment rather than a span. If the model handed us an
# interval for one of these, the meaningful instant is the end of it.
_DEADLINE_KINDS = {EventKind.SUBMISSION, EventKind.CODE_FREEZE, EventKind.REGISTRATION}
_MAX_EVENT_HOURS = 24 * 14


@dataclass(slots=True)
class PlannedEvent:
    kind: EventKind
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    place: str | None = None
    url: str | None = None
    is_mandatory: bool = False
    needs_rsvp: bool = False


@dataclass(slots=True)
class IngestPlan:
    fields: dict[str, Any] = field(default_factory=dict)
    events: list[PlannedEvent] = field(default_factory=list)
    links: list[tuple[LinkKind, str, str | None]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.fields or self.events or self.links)


def _clean_title(title: str | None, *, year: int | None, city: str | None) -> str | None:
    """Models like to fold the year and city into the name; the schema has fields."""
    if not title:
        return None
    out = title.strip()
    if year:
        out = re.sub(rf"\b{year}\b", "", out)
    if city:
        out = re.sub(re.escape(city), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,-–—·")
    return out or title.strip()


def _sanitize_event(
    raw: Any, tz: ZoneInfo
) -> PlannedEvent | None:
    starts_at = parse_iso(raw.starts_at, tz)
    if starts_at is None:
        log.info("dropping event %r: unparseable start %r", raw.title, raw.starts_at)
        return None

    try:
        kind = EventKind(raw.kind)
    except ValueError:
        kind = EventKind.guess(raw.title)

    ends_at = parse_iso(raw.ends_at, tz) if raw.ends_at else None

    if ends_at is not None:
        if ends_at <= starts_at:
            # A duplicated or backwards end carries no information.
            ends_at = None
        elif ends_at - starts_at > timedelta(hours=_MAX_EVENT_HOURS):
            ends_at = None

    # `до 22.09 18:00` often arrives as midnight..18:00. For deadline-shaped kinds
    # the instant that matters is the end, so collapse the span onto it.
    if ends_at is not None and kind in _DEADLINE_KINDS:
        local_start = to_local(starts_at, tz)
        if (local_start.hour, local_start.minute) == (0, 0):
            starts_at, ends_at = ends_at, None
        else:
            ends_at = None

    title = (raw.title or kind.label).strip()
    title = re.sub(r"\s*\((начало|окончание|старт|конец)\)\s*$", "", title, flags=re.IGNORECASE)

    return PlannedEvent(
        kind=kind,
        title=title[:200],
        starts_at=starts_at,
        ends_at=ends_at,
        place=(raw.place or None),
        url=normalize_url(raw.url) if raw.url else None,
        # The model sprays is_mandatory around; the kind is the reliable signal.
        is_mandatory=kind.is_critical,
        needs_rsvp=kind.is_critical,
    )


def _dedupe(events: list[PlannedEvent]) -> list[PlannedEvent]:
    seen: dict[tuple[EventKind, str], PlannedEvent] = {}
    for ev in sorted(events, key=lambda e: e.starts_at):
        key = (ev.kind, ev.starts_at.isoformat())
        if key in seen:
            continue
        # same kind within an hour is almost certainly the same thing twice
        clash = next(
            (
                other
                for (other_kind, _), other in seen.items()
                if other_kind is ev.kind
                and abs((other.starts_at - ev.starts_at).total_seconds()) < 3600
            ),
            None,
        )
        if clash is not None and ev.kind is not EventKind.OTHER:
            continue
        seen[key] = ev
    return list(seen.values())


# Questions the operator has already answered globally. Asking them again is noise.
_POINTLESS_QUESTIONS = ("часов", "пояс", "таймзон", "timezone", "utc", "мск")


def _worth_asking(question: str) -> bool:
    text = question.casefold()
    return not any(needle in text for needle in _POINTLESS_QUESTIONS)


def build_plan(
    data: ExtractedHackathon, tz: ZoneInfo, *, existing: Hackathon | None = None
) -> IngestPlan:
    """Repair the model output and express it as a set of intended changes."""
    plan = IngestPlan(questions=[q for q in data.questions if _worth_asking(q)][:4])

    starts_at = parse_iso(data.starts_at, tz)
    ends_at = parse_iso(data.ends_at, tz)
    reg_deadline = parse_iso(data.reg_deadline, tz)

    if starts_at and ends_at and ends_at < starts_at:
        plan.notes.append("конец хакатона был раньше начала - поменял местами")
        starts_at, ends_at = ends_at, starts_at

    title = _clean_title(data.title, year=data.year, city=data.city)
    year = data.year or (to_local(starts_at, tz).year if starts_at else None)

    candidate: dict[str, Any] = {
        "title": title,
        "year": year,
        "organizer": data.organizer,
        "city": None if data.is_online else data.city,
        "is_online": data.is_online,
        "description": data.description,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "reg_deadline": reg_deadline,
    }
    if data.timezone:
        try:
            ZoneInfo(data.timezone)
            candidate["tz"] = data.timezone
        except Exception:
            log.info("ignoring unknown timezone %r", data.timezone)

    # Never overwrite a known value with nothing.
    for key, value in candidate.items():
        if value in (None, ""):
            continue
        if existing is not None and getattr(existing, key, None) == value:
            continue
        plan.fields[key] = value

    sanitized = [ev for raw in data.events if (ev := _sanitize_event(raw, tz)) is not None]
    plan.events = _dedupe(sanitized)
    if len(sanitized) != len(plan.events):
        plan.notes.append(f"схлопнул {len(sanitized) - len(plan.events)} дублей этапов")

    for link in data.links:
        url = normalize_url(link.url)
        if not url:
            continue
        try:
            kind = LinkKind(link.kind)
        except ValueError:
            kind = LinkKind.OTHER
        plan.links.append((kind, url, link.title))

    return plan


@dataclass(slots=True)
class IngestResult:
    changed_fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    added_events: list[Event] = field(default_factory=list)
    updated_events: list[Event] = field(default_factory=list)
    added_links: int = 0


async def apply_plan(session: AsyncSession, hack: Hackathon, plan: IngestPlan) -> IngestResult:
    """Write the plan. Existing events of the same kind are moved, not duplicated."""
    result = IngestResult()

    if plan.fields:
        result.changed_fields = await update_fields(session, hack, plan.fields)
        if "title" in plan.fields and not hack.slug:
            hack.slug = slugify(hack.title)

    existing = await list_events(session, hack.id)
    by_kind: dict[EventKind, Event] = {}
    for ev in existing:
        if ev.kind is not EventKind.OTHER:
            by_kind.setdefault(ev.kind, ev)

    for planned in plan.events:
        current = by_kind.get(planned.kind)
        if current is not None and planned.kind is not EventKind.OTHER:
            changed = await update_event(
                session,
                current,
                {
                    "title": planned.title,
                    "starts_at": planned.starts_at,
                    "ends_at": planned.ends_at,
                    "place": planned.place or current.place,
                    "url": planned.url or current.url,
                },
            )
            if changed:
                result.updated_events.append(current)
            continue

        created = await add_event(
            session,
            hack,
            title=planned.title,
            starts_at=planned.starts_at,
            kind=planned.kind,
            ends_at=planned.ends_at,
            place=planned.place,
            url=planned.url,
            is_mandatory=planned.is_mandatory,
            needs_rsvp=planned.needs_rsvp,
        )
        result.added_events.append(created)
        by_kind.setdefault(planned.kind, created)

    for kind, url, title in plan.links:
        await set_link(session, hack, kind, url, title)
        result.added_links += 1

    return result
