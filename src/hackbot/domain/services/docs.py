"""Attachments: stored locally, mirrored into the repository's docs/ folder."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackbot.config import get_settings
from hackbot.db.models import Doc, Event, Hackathon
from hackbot.domain.services.github import RepoInfo, put_file
from hackbot.domain.services.hackathons import hack_tz
from hackbot.domain.textutils import safe_filename, slugify
from hackbot.domain.timeutils import fmt_dt, now_utc

log = logging.getLogger(__name__)


def storage_dir(hack: Hackathon) -> Path:
    root = get_settings().abs_files_dir / f"{hack.id}_{slugify(hack.title, max_len=24)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def unique_path(directory: Path, file_name: str) -> Path:
    """Never clobber an existing file: `rules.pdf` -> `rules_2.pdf`."""
    candidate = directory / safe_filename(file_name)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 100):
        alt = directory / f"{stem}_{index}{suffix}"
        if not alt.exists():
            return alt
    return directory / f"{stem}_{int(now_utc().timestamp())}{suffix}"


async def list_docs(session: AsyncSession, hackathon_id: int) -> list[Doc]:
    stmt = select(Doc).where(Doc.hackathon_id == hackathon_id).order_by(Doc.created_at)
    return list(await session.scalars(stmt))


async def add_doc(
    session: AsyncSession,
    hack: Hackathon,
    *,
    file_name: str,
    payload: bytes,
    tg_file_id: str | None = None,
    mime: str | None = None,
    caption: str | None = None,
    uploaded_by: int | None = None,
) -> Doc:
    # Disk work is offloaded so a large attachment cannot stall the event loop.
    path = await asyncio.to_thread(lambda: unique_path(storage_dir(hack), file_name))
    await asyncio.to_thread(path.write_bytes, payload)

    doc = Doc(
        hackathon_id=hack.id,
        file_name=path.name,
        tg_file_id=tg_file_id,
        local_path=str(path),
        mime=mime,
        size=len(payload),
        caption=caption,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    await session.flush()
    return doc


def _read_if_exists(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


async def push_doc(repo: RepoInfo, doc: Doc) -> str | None:
    """Upload one stored document into `docs/` of the repository."""
    if not doc.local_path:
        return None
    path = Path(doc.local_path)
    payload = await asyncio.to_thread(_read_if_exists, path)
    if payload is None:
        log.warning("doc %s missing on disk: %s", doc.id, path)
        return None
    target = f"docs/{doc.file_name}"
    url = await put_file(repo, target, payload, message=f"docs: {doc.file_name}")
    doc.github_path = target
    return url


def build_readme(hack: Hackathon, events: list[Event], docs: list[Doc]) -> bytes:
    """A README that is actually useful during the hackathon, not boilerplate."""
    tz = hack_tz(hack)
    lines: list[str] = [f"# {hack.title} {hack.year}", ""]

    facts: list[str] = []
    if hack.organizer:
        facts.append(f"**Организатор:** {hack.organizer}")
    if hack.city:
        facts.append(f"**Место:** {hack.city}")
    elif hack.is_online:
        facts.append("**Формат:** онлайн")
    if hack.starts_at:
        facts.append(f"**Начало:** {fmt_dt(hack.starts_at, tz, with_year=True)}")
    if hack.ends_at:
        facts.append(f"**Конец:** {fmt_dt(hack.ends_at, tz, with_year=True)}")
    if hack.reg_deadline:
        facts.append(f"**Регистрация до:** {fmt_dt(hack.reg_deadline, tz, with_year=True)}")
    if facts:
        lines += [*facts, ""]

    if hack.description:
        lines += [hack.description, ""]

    if events:
        lines += ["## Таймлайн", "", "| Когда | Этап | Где |", "|---|---|---|"]
        for event in sorted(events, key=lambda e: e.starts_at):
            when = fmt_dt(event.starts_at, tz)
            if event.ends_at:
                when += f" – {fmt_dt(event.ends_at, tz).split(', ')[-1]}"
            title = event.title
            if event.is_mandatory:
                title = f"**{title}**"
            place = event.place or ""
            if event.url:
                link_md = f"[ссылка]({event.url})"
                place = link_md if not place else f"{place} · {link_md}"
            lines.append(f"| {when} | {event.kind.emoji} {title} | {place} |")
        lines.append("")

    if hack.links:
        lines += ["## Ссылки", ""]
        for link in hack.links:
            lines.append(f"- {link.kind.emoji} [{link.title or link.kind.label}]({link.url})")
        lines.append("")

    if docs:
        lines += ["## Документы", ""]
        for doc in docs:
            target = doc.github_path or f"docs/{doc.file_name}"
            label = doc.caption or doc.file_name
            lines.append(f"- [{label}]({target})")
        lines.append("")

    if hack.result_place:
        lines += ["## Результат", "", f"🏆 {hack.result_place}", ""]
        if hack.result_note:
            lines += [hack.result_note, ""]

    lines += ["---", "", "_Таймлайн ведёт бот в теме хакатона._"]
    return "\n".join(lines).encode("utf-8")
