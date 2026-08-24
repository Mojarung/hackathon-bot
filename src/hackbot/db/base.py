"""Engine, session factory and the UTC datetime type used across the schema."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar

from sqlalchemy import DateTime, String, event
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from hackbot.config import get_settings


class UtcDateTime(TypeDecorator[datetime]):
    """SQLite drops tzinfo silently, which turns every comparison into a coin flip.

    This decorator normalises to UTC on the way in and re-attaches UTC on the way
    out, so application code only ever sees aware datetimes.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class EnumType[E: Enum](TypeDecorator[E]):
    """Stores an enum by value and hands back the enum member, not a bare string.

    SQLAlchemy would happily bind a StrEnum into a plain String column, but the
    round trip returns `str`, and every `.label` / `.emoji` lookup would explode.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[E], length: int = 32) -> None:
        self.enum_cls = enum_cls
        super().__init__(length)

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value if isinstance(value, Enum) else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self.enum_cls(value)


class Base(AsyncAttrs, DeclarativeBase):
    # ClassVar keeps SQLAlchemy's convention without tripping the mutable-default lint.
    type_annotation_map: ClassVar[dict[object, object]] = {datetime: UtcDateTime}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _configure_sqlite(dbapi_conn: Any, _record: Any) -> None:
    """WAL survives in the file; the rest are per-connection and default to off."""
    cur = dbapi_conn.cursor()
    for pragma in (
        "journal_mode=WAL",
        "foreign_keys=ON",
        "busy_timeout=5000",
        "synchronous=NORMAL",
    ):
        cur.execute(f"PRAGMA {pragma}")
    cur.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.abs_db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(settings.db_url, echo=False, future=True)
        event.listens_for(_engine.sync_engine, "connect")(_configure_sqlite)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        # expire_on_commit=False: touching an attribute after commit would
        # otherwise trigger lazy IO outside the greenlet and raise MissingGreenlet.
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One session per unit of work. Never share a session between tasks."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    from hackbot.db import models  # noqa: F401  - registers mappers

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
