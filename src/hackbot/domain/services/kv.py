"""Tiny key/value store for operational odds and ends."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.db.models import KV
from hackbot.domain.timeutils import now_utc


async def get(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(KV, key)
    return row.value if row else default


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(KV, key)
    if row:
        row.value = value
        row.updated_at = now_utc()
    else:
        session.add(KV(key=key, value=value))
    await session.flush()


async def get_json(session: AsyncSession, key: str, default: Any = None) -> Any:
    raw = await get(session, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


async def set_json(session: AsyncSession, key: str, value: Any) -> None:
    await set_value(session, key, json.dumps(value, ensure_ascii=False, default=str))


async def push_capped(session: AsyncSession, key: str, item: str, *, cap: int = 20) -> list[str]:
    """Append to a bounded list. Used for the recent-jokes memory."""
    items: list[str] = await get_json(session, key, []) or []
    items.append(item)
    items = items[-cap:]
    await set_json(session, key, items)
    return items
