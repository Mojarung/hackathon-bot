"""Naming a hackathon out loud, and dropping one the team changed its mind about.

Deleting cascades to every stage, reminder, link, document and roster entry, so
the interesting question is not whether it deletes - it is whether the right one
was found. Resolving a spoken name to exactly one row, or refusing, is the whole
safety story here.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hackbot.db.base import Base
from hackbot.db.models import Event, Hackathon
from hackbot.domain.enums import EventKind, HackStatus
from hackbot.domain.services import hackathons
from hackbot.domain.timeutils import now_utc

CHAT = -100


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _hack(session, title: str, *, slug: str = "", chat_id: int = CHAT, stages: int = 0):
    hack = Hackathon(
        slug=slug or title.casefold().replace(" ", "-"),
        title=title, year=2026, chat_id=chat_id, thread_id=None,
        status=HackStatus.DRAFT,
        events=[
            Event(kind=EventKind.OTHER, title=f"Этап {i}", starts_at=now_utc())
            for i in range(stages)
        ],
    )
    session.add(hack)
    await session.commit()
    return hack


# ---------------------------------------------------------------- finding


async def test_an_exact_title_wins(session) -> None:
    await _hack(session, "Тендерхак")
    await _hack(session, "ЛЦТ")

    found, _ = await hackathons.find_by_title(session, CHAT, "тендерхак")
    assert found.title == "Тендерхак"


async def test_a_unique_fragment_is_enough(session) -> None:
    await _hack(session, "PostCode Hack")
    await _hack(session, "ЛЦТ")

    found, _ = await hackathons.find_by_title(session, CHAT, "postcode")
    assert found.title == "PostCode Hack"


async def test_an_ambiguous_name_refuses_and_hands_back_the_candidates(session) -> None:
    """Dropping a hackathon is not the place to pick the first plausible row."""
    await _hack(session, "Хакатон МТС")
    await _hack(session, "Хакатон ВТБ")

    found, candidates = await hackathons.find_by_title(session, CHAT, "хакатон")
    assert found is None
    assert {h.title for h in candidates} == {"Хакатон МТС", "Хакатон ВТБ"}


async def test_a_hackathon_of_another_chat_is_not_reachable(session) -> None:
    await _hack(session, "Чужой", chat_id=-777)

    found, candidates = await hackathons.find_by_title(session, CHAT, "Чужой")
    assert found is None and candidates == []


async def test_an_empty_name_matches_nothing(session) -> None:
    await _hack(session, "Тендерхак")

    assert await hackathons.find_by_title(session, CHAT, "  ") == (None, [])


# ---------------------------------------------------------------- dropping


async def test_dropping_takes_the_stages_with_it(session) -> None:
    hack = await _hack(session, "Передумали", stages=3)
    keep = await _hack(session, "Остаётся", stages=2)

    await hackathons.delete(session, hack)
    await session.commit()

    left = list(await session.scalars(select(Event)))
    assert {e.hackathon_id for e in left} == {keep.id}
    assert [h.title for h in await hackathons.list_hackathons(session, chat_id=CHAT)] == [
        "Остаётся"
    ]
