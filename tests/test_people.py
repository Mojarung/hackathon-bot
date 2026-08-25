"""Profiles: identity is recorded automatically, facts only when told."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hackbot.db.base import Base
from hackbot.domain.services import people


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def test_touch_creates_then_updates(session) -> None:
    first = await people.touch(
        session, tg_user_id=7, username="kir", full_name="Кирилл", chat_id=-1
    )
    assert first.messages == 1

    again = await people.touch(session, tg_user_id=7, username="kir_new", full_name="Кирилл")
    assert again.id == first.id
    assert again.messages == 2
    assert again.username == "kir_new"      # renamed accounts follow the id
    assert again.chat_id == -1              # first sighting is not overwritten


async def test_touch_keeps_name_when_telegram_omits_it(session) -> None:
    await people.touch(session, tg_user_id=7, username="kir", full_name="Кирилл")
    blank = await people.touch(session, tg_user_id=7, username=None, full_name="")
    assert blank.full_name == "Кирилл"
    assert blank.username == "kir"


async def test_facts_merge_instead_of_overwriting(session) -> None:
    person = await people.touch(session, tg_user_id=7, full_name="Кирилл")
    await people.remember(session, person, about="бэкендер")
    await people.remember(session, person, about="учится в вышке")
    assert person.about == "бэкендер; учится в вышке"

    await people.remember(session, person, about="бэкендер")  # already known
    assert person.about == "бэкендер; учится в вышке"


async def test_facts_stay_within_the_limit(session) -> None:
    person = await people.touch(session, tg_user_id=7, full_name="Кирилл")
    for i in range(60):
        await people.remember(session, person, about=f"факт номер {i} про этого человека")
    assert len(person.about) <= people.FIELD_LIMIT
    assert "факт номер 59" in person.about   # the newest fact always survives
    assert "факт номер 0 " not in person.about


async def test_find_by_nick_name_and_self(session) -> None:
    kirill = await people.touch(session, tg_user_id=7, username="kir", full_name="Кирилл Иванов")
    sanya = await people.touch(session, tg_user_id=8, username="sanya", full_name="Саня")

    assert (await people.find(session, "@kir")).id == kirill.id
    assert (await people.find(session, "Кирилл Иванов")).id == kirill.id
    assert (await people.find(session, "иванов")).id == kirill.id     # fragment, any case
    assert (await people.find(session, "8")).id == sanya.id           # bare telegram id
    assert (await people.find(session, "я", speaker_id=8)).id == sanya.id
    assert await people.find(session, "кого-то-нет") is None


async def test_roster_skips_people_with_nothing_recorded(session) -> None:
    await people.touch(session, tg_user_id=7, full_name="Кирилл", chat_id=-1)
    sanya = await people.touch(session, tg_user_id=8, full_name="Саня", chat_id=-1)
    await people.remember(session, sanya, about="фронтендер", traits="спокойный")

    listed = await people.roster(session, chat_id=-1)
    assert [p.tg_user_id for p in listed] == [8]
    assert listed[0].summary() == "Саня — фронтендер; характер: спокойный"

    assert await people.roster(session, chat_id=-1, exclude=8) == []


async def test_forget_clears_facts_but_keeps_the_person(session) -> None:
    person = await people.touch(session, tg_user_id=7, full_name="Кирилл")
    await people.remember(session, person, about="бэкендер", traits="злой", note="любит кофе")

    assert await people.forget(session, person, "traits") == "traits"
    assert person.traits is None
    assert person.about == "бэкендер"

    await people.forget(session, person)
    assert not person.facts
    assert (await people.get(session, 7)).full_name == "Кирилл"


@pytest.mark.parametrize("word", ["я", "меня", "себя", "Я"])
async def test_self_words(session, word: str) -> None:
    await people.touch(session, tg_user_id=7, full_name="Кирилл")
    assert (await people.find(session, word, speaker_id=7)).tg_user_id == 7
