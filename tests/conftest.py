"""Every test gets its own empty database.

Without this the suite would open the developer's real `data/hackbot.db`: tests
that touch a handler would leave rows behind, and a fresh clone with no database
file at all would fail on a missing table rather than on the thing being tested.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hackbot.db import base
from hackbot.db.models import Base  # noqa: F401 - imports register the mappers


@pytest_asyncio.fixture(autouse=True)
async def isolated_db():
    previous = (base._engine, base._session_factory)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    base._engine = engine
    # A single connection: an in-memory database belongs to the connection that
    # opened it, so a pooled second one would not see the tables.
    base._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine
    finally:
        await engine.dispose()
        base._engine, base._session_factory = previous
