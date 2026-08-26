"""What actually lands in the shared Google calendar.

Two things carry the whole design. The title has to name the hackathon, because
one calendar holds all of them and a bare "Защита" says nothing. And the event
id has to be reproducible from the database alone: there is no column to store a
Google id in, so the id is the only thing tying a row to its calendar entry -
across a resync, a restart, or a restore from backup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hackbot.config import get_settings
from hackbot.db.base import Base
from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import EventKind, HackStatus
from hackbot.domain.services import calsync, gcal, kv
from hackbot.domain.services import events as event_service
from hackbot.domain.timeutils import now_utc

TZ_NAME = "Europe/Moscow"
START = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)  # 18:00 по Москве
MOSCOW_OFFSET = timedelta(hours=3)

# base32hex, which is what Google accepts for a caller-supplied event id.
ALPHABET = set("abcdefghijklmnopqrstuv0123456789")


def _hack(**overrides: Any) -> Hackathon:
    fields: dict[str, Any] = {
        "id": 7,
        "slug": "tenderhack",
        "title": "ТендерХак",
        "year": 2026,
        "tz": TZ_NAME,
        "chat_id": -100,
        "thread_id": 5,
        "status": HackStatus.RUNNING,
        "is_online": False,
    }
    hack = Hackathon(**(fields | overrides))
    hack.links = []
    return hack


def _event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "id": 42,
        "hackathon_id": 7,
        "kind": EventKind.DEFENSE,
        "title": "Защита проектов",
        "starts_at": START,
    }
    return Event(**(fields | overrides))


# ---------------------------------------------------------------- event id


@pytest.mark.parametrize(("hack_id", "event_id"), [(1, 1), (7, 42), (13, 999), (2026, 31337)])
def test_the_event_id_stays_inside_the_allowed_alphabet(hack_id: int, event_id: int) -> None:
    value = calsync.gid(hack_id, event_id)
    assert len(value) >= 5, "Google требует минимум 5 символов"
    assert set(value) <= ALPHABET


def test_the_last_four_letters_are_the_whole_point() -> None:
    """base32hex stops at v: a w, x, y or z in the id is a 400, not a warning."""
    assert ALPHABET.isdisjoint("wxyz")
    assert not set(calsync.gid(7, 42)) & set("wxyz")


def test_the_id_is_reproducible_and_never_collides() -> None:
    assert calsync.gid(7, 42) == calsync.gid(7, 42)
    assert calsync.gid(7, 42) != calsync.gid(42, 7)


# ---------------------------------------------------------------- event body


def test_the_title_names_the_hackathon_and_not_only_the_event() -> None:
    body = calsync.build_body(_hack(), _event())

    assert "Защита проектов" in body["summary"]
    assert "ТендерХак" in body["summary"], "иначе в общем календаре не разобрать, чей это этап"
    assert EventKind.DEFENSE.emoji in body["summary"]


def test_an_open_ended_event_gets_an_hour() -> None:
    body = calsync.build_body(_hack(), _event(ends_at=None))

    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert end - start == timedelta(hours=1)


def test_a_real_interval_survives() -> None:
    body = calsync.build_body(_hack(), _event(ends_at=START + timedelta(hours=3)))

    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert end == START + timedelta(hours=3)


def test_times_are_written_in_the_zone_of_the_hackathon() -> None:
    """The database is UTC; a calendar showing 15:00 for an 18:00 pitch is a bug."""
    body = calsync.build_body(_hack(), _event())

    start = datetime.fromisoformat(body["start"]["dateTime"])
    assert body["start"]["timeZone"] == TZ_NAME
    assert body["end"]["timeZone"] == TZ_NAME
    assert start.utcoffset() == MOSCOW_OFFSET
    assert start.hour == 18
    assert start == START


def test_a_critical_event_brings_its_own_reminder() -> None:
    body = calsync.build_body(_hack(), _event(kind=EventKind.SUBMISSION, title="Сдача решения"))

    assert body["reminders"]["useDefault"] is False
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 60}]


def test_an_ordinary_event_leaves_the_defaults_alone() -> None:
    body = calsync.build_body(_hack(), _event(kind=EventKind.MENTOR, title="Менторская"))

    assert body["reminders"] == {"useDefault": True}


def test_the_event_is_tagged_with_the_rows_it_came_from() -> None:
    """The tags are what lets a later sync find the leftovers of one hackathon."""
    body = calsync.build_body(_hack(), _event())

    private = body["extendedProperties"]["private"]
    assert private["hackbot_hack"] == "7"
    assert private["hackbot_event"] == "42"
    assert body["id"] == calsync.gid(7, 42)


def test_the_event_is_always_confirmed() -> None:
    """Writing `confirmed` is what revives an entry someone deleted by hand."""
    assert calsync.build_body(_hack(), _event())["status"] == "confirmed"


def test_the_description_carries_what_the_title_cannot() -> None:
    event = _event(
        notes="Регламент 5 минут",
        url="https://example.org/pitch",
        place="Зал А",
        is_mandatory=True,
    )
    body = calsync.build_body(_hack(), event)

    description = body["description"]
    assert "ТендерХак" in description
    assert "2026" in description
    assert "Регламент 5 минут" in description
    assert "Обязательное участие" in description
    assert "https://example.org/pitch" in description
    assert body["location"] == "Зал А"


# ---------------------------------------------------------------- pushing a hackathon


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _stored_hackathon(session) -> Hackathon:
    hack = Hackathon(
        slug="tenderhack",
        title="ТендерХак",
        year=2026,
        tz=TZ_NAME,
        chat_id=-100,
        thread_id=5,
        status=HackStatus.RUNNING,
        events=[
            Event(kind=EventKind.START, title="Открытие", starts_at=START),
            Event(
                kind=EventKind.SUBMISSION,
                title="Сдача решения",
                starts_at=START + timedelta(hours=3),
            ),
            Event(kind=EventKind.DEFENSE, title="Защита", starts_at=START + timedelta(hours=5)),
        ],
    )
    session.add(hack)
    await session.commit()
    return hack


class FakeGoogle:
    """Stands in for the whole transport module: records instead of sending.

    Both import styles are patched, so it does not matter whether calsync kept a
    reference to the module or pulled the names in directly.
    """

    def __init__(self, *, present: set[str] | None = None, broken: set[str] | None = None) -> None:
        self.present = set(present or ())
        self.broken = set(broken or ())
        self.upserted: dict[str, dict] = {}
        self.deleted: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fakes = {
            "enabled": lambda: True,
            "upsert_event": self._upsert,
            "delete_event": self._delete,
            "list_event_ids": self._list,
        }
        for name, fake in fakes.items():
            monkeypatch.setattr(gcal, name, fake)
            if hasattr(calsync, name):
                monkeypatch.setattr(calsync, name, fake)

    async def _upsert(self, gid: str, body: dict) -> None:
        if gid in self.broken:
            raise gcal.GCalError(503, "Backend Error")
        self.upserted[gid] = body

    async def _delete(self, gid: str) -> None:
        self.deleted.append(gid)

    async def _list(self, hack_id: int) -> set[str]:
        return set(self.present)


async def test_every_event_of_the_hackathon_is_written(session, monkeypatch) -> None:
    hack = await _stored_hackathon(session)
    google = FakeGoogle()
    google.install(monkeypatch)

    written = await calsync.push_hackathon(session, hack)

    assert written == len(hack.events)
    assert set(google.upserted) == {calsync.gid(hack.id, e.id) for e in hack.events}


async def test_events_gone_from_the_database_are_removed_from_google(
    session, monkeypatch
) -> None:
    """Deleting an event in the bot has to reach the calendar, or it stays a lie."""
    hack = await _stored_hackathon(session)
    alive = {calsync.gid(hack.id, e.id) for e in hack.events}
    orphan = calsync.gid(hack.id, 9999)
    google = FakeGoogle(present=alive | {orphan})
    google.install(monkeypatch)

    await calsync.push_hackathon(session, hack)

    assert google.deleted == [orphan]
    assert set(google.upserted) == alive


async def test_one_broken_event_does_not_sink_the_rest(session, monkeypatch) -> None:
    hack = await _stored_hackathon(session)
    victim = calsync.gid(hack.id, hack.events[1].id)
    google = FakeGoogle(broken={victim})
    google.install(monkeypatch)

    written = await calsync.push_hackathon(session, hack)

    assert written == len(hack.events) - 1
    assert victim not in google.upserted
    assert len(google.upserted) == len(hack.events) - 1


class _DeadNetwork:
    """aiohttp that never gets anywhere, counting how far the caller still got."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.attempts = 0

    def __call__(self, *_a: Any, **_kw: Any) -> _DeadNetwork:
        return self

    async def __aenter__(self) -> _DeadNetwork:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def request(self, *_a: Any, **_kw: Any) -> Any:
        self.attempts += 1
        raise self._error


async def _canned_token() -> str:
    """Auth is not what this test is about; only the calendar calls are."""
    return "test-token"


async def test_a_network_blackout_does_not_abort_the_rest_of_the_hackathon(
    session, monkeypatch
) -> None:
    """The per-event guard has to cover a timeout, not only an HTTP status.

    A raw TimeoutError escaping `except GCalError` costs far more than the one
    stage it belongs to: it takes the remaining stages, and in a full pass every
    hackathon queued behind this one - after sync_due has already cleared the
    dirty set and stamped the pass as done, so nothing retries for half an hour.
    """
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(get_settings(), "google_calendar_id", "cal@group.calendar.google.com")
    monkeypatch.setattr(gcal, "enabled", lambda: True)
    monkeypatch.setattr(gcal, "_access_token", _canned_token)
    network = _DeadNetwork(TimeoutError("45s and nothing"))
    monkeypatch.setattr(aiohttp, "ClientSession", network)

    assert await calsync.push_hackathon(session, hack) == 0
    assert network.attempts == len(hack.events), "каждый этап обязан получить свою попытку"


async def test_a_disabled_calendar_costs_nothing(session, monkeypatch) -> None:
    """The integration is off by default: the bot has to behave exactly as before."""
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(get_settings(), "google_calendar_id", "")
    monkeypatch.setattr(aiohttp, "ClientSession", _no_network)

    assert await calsync.push_hackathon(session, hack) == 0


def _no_network(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("выключенная интеграция полезла в сеть")


# ---------------------------------------------------------------- dirty marking


@pytest.fixture
def clean_dirty():
    """The dirty set is module state; a leaked hackathon id would fake a pass."""
    calsync._dirty.clear()
    yield calsync._dirty
    calsync._dirty.clear()


async def test_adding_a_stage_marks_the_hackathon_for_the_next_tick(
    session, monkeypatch, clean_dirty
) -> None:
    """Timeline edits arrive from six different call sites.

    They are marked inside `events.add_event` rather than at each of those sites
    precisely so that a new one cannot forget: a stage that never marks anything
    waits up to google_calendar_sync_minutes before the user sees it in Google.
    """
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(gcal, "enabled", lambda: True)

    await event_service.add_event(
        session, hack, title="Кофе-брейк", starts_at=START + timedelta(hours=1)
    )

    assert clean_dirty == {hack.id}


async def test_moving_a_stage_marks_it(session, monkeypatch, clean_dirty) -> None:
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(gcal, "enabled", lambda: True)

    await event_service.update_event(
        session, hack.events[0], {"starts_at": START + timedelta(days=1)}
    )

    assert clean_dirty == {hack.id}


async def test_deleting_a_stage_marks_it(session, monkeypatch, clean_dirty) -> None:
    """The owner has to be read before the delete flushes, or there is nothing to mark."""
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(gcal, "enabled", lambda: True)

    await event_service.delete_event(session, hack.events[0])

    assert clean_dirty == {hack.id}


async def test_an_unchanged_edit_marks_nothing(session, monkeypatch, clean_dirty) -> None:
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(gcal, "enabled", lambda: True)

    await event_service.update_event(session, hack.events[0], {"starts_at": START})

    assert clean_dirty == set()


async def test_a_disabled_calendar_collects_no_dirty_hackathons(
    session, monkeypatch, clean_dirty
) -> None:
    """Off by default means the set must not grow in a bot that has no calendar."""
    hack = await _stored_hackathon(session)
    monkeypatch.setattr(get_settings(), "google_calendar_id", "")

    await event_service.add_event(session, hack, title="Кофе-брейк", starts_at=START)

    assert clean_dirty == set()


async def test_sync_due_pushes_exactly_what_was_marked(
    session, monkeypatch, clean_dirty
) -> None:
    """The fast half of a tick: a fresh edit, without waiting for the full sweep."""
    hack = await _stored_hackathon(session)
    # Stamp the full pass as just done, so the dirty branch is the one under test.
    await kv.set_value(session, calsync.FULL_SYNC_KEY, now_utc().isoformat())
    google = FakeGoogle()
    google.install(monkeypatch)

    event = await event_service.add_event(
        session, hack, title="Кофе-брейк", starts_at=START + timedelta(hours=1)
    )
    await session.commit()
    assert clean_dirty == {hack.id}

    written = await calsync.sync_due(session)

    assert written == len(hack.events) + 1
    assert calsync.gid(hack.id, event.id) in google.upserted
    assert clean_dirty == set(), "разобранный набор не должен переезжать в следующий тик"


async def test_a_sweep_tick_does_not_swallow_an_edit_the_sweep_will_skip(
    session, monkeypatch, clean_dirty
) -> None:
    """The two halves of a tick disagree about scope, and the set must not pay for it.

    `push_all` walks live hackathons only, but a finished one is still editable -
    `/move` and `/set` resolve it fine and mark it dirty. Clearing the set on the way
    into the sweep therefore dropped that edit for good: the sweep skips the
    hackathon, and no later sweep ever revisits it either.
    """
    hack = await _stored_hackathon(session)
    hack.status = HackStatus.FINISHED
    await session.commit()
    google = FakeGoogle()
    google.install(monkeypatch)
    # No stamp in kv, so this tick takes the full-pass branch - the one that used
    # to clear the set before a sweep that cannot see this hackathon.
    calsync.mark_dirty(hack.id)

    written = await calsync.sync_due(session)

    assert written == len(hack.events), "правка завершённого хакатона обязана доехать"
    assert set(google.upserted) == {calsync.gid(hack.id, e.id) for e in hack.events}
    assert clean_dirty == set()


async def test_a_stage_created_mid_pass_is_not_mistaken_for_an_orphan(
    session, monkeypatch, clean_dirty
) -> None:
    """`alive` is a snapshot taken before the upserts, and the timeline moves on.

    /gcal, the agent's tool and the scheduler tick share one event loop, so another
    path can create a stage while this pass is still walking. Trusting the stale
    snapshot would delete a real event and log it as cleanup.
    """
    hack = await _stored_hackathon(session)
    google = FakeGoogle()
    google.install(monkeypatch)

    async def _list_after_a_concurrent_add(hack_id: int) -> set[str]:
        # Exactly what a /add landing mid-pass leaves behind: a row in the database
        # and an entry in Google, neither of which the snapshot knows about.
        await event_service.add_event(
            session, hack, title="Внезапный митап", starts_at=START + timedelta(hours=9)
        )
        await session.commit()
        rows = await event_service.list_events(session, hack.id)
        return {calsync.gid(hack.id, row.id) for row in rows}

    monkeypatch.setattr(gcal, "list_event_ids", _list_after_a_concurrent_add)

    await calsync.push_hackathon(session, hack)

    assert google.deleted == [], "чужой свежий этап удалять нельзя"
