"""Read-only web face on port 9999.

Exists for two reasons: a link you can hand to an organiser or a teammate who is
not in the chat, and a stable `.ics` feed so calendar subscriptions keep picking
up timeline edits without anyone re-sending a file.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse

from hackbot.db.base import session_scope
from hackbot.domain.services.events import list_events
from hackbot.domain.services.hackathons import (
    get_by_slug,
    hack_tz,
    list_hackathons,
    primary_deadline,
    progress_ratio,
)
from hackbot.domain.services.ics import build_calendar
from hackbot.domain.timeutils import fmt_dt, humanize_delta, now_utc
from hackbot.web.templates import render_index, render_page

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="hackbot", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        async with session_scope() as session:
            hacks = await list_hackathons(session)
        return JSONResponse({"status": "ok", "hackathons": len(hacks)})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        async with session_scope() as session:
            hacks = await list_hackathons(session)
            rows = []
            for hack in hacks:
                tz = hack_tz(hack)
                rows.append(
                    {
                        "slug": hack.slug,
                        "title": hack.title,
                        "year": hack.year,
                        "status": hack.status,
                        "when": fmt_dt(hack.starts_at, tz, with_year=True)
                        if hack.starts_at
                        else "даты не заданы",
                        "result": hack.result_place,
                    }
                )
        return HTMLResponse(render_index(rows))

    @app.get("/h/{slug}", response_class=HTMLResponse)
    async def page(slug: str) -> HTMLResponse:
        async with session_scope() as session:
            hack = await get_by_slug(session, slug)
            if hack is None:
                raise HTTPException(status_code=404, detail="not found")
            events = await list_events(session, hack.id)
            tz = hack_tz(hack)
            now = now_utc()

            target = primary_deadline(hack, events)
            countdown = None
            if target is not None and target.starts_at > now:
                countdown = {
                    "title": target.title,
                    "left": humanize_delta((target.starts_at - now).total_seconds(), parts=3),
                }
            html = render_page(
                hack=hack,
                events=events,
                tz=tz,
                now=now,
                ratio=progress_ratio(hack, now),
                countdown=countdown,
            )
        return HTMLResponse(html)

    @app.get("/ics/{slug}.ics")
    async def calendar(slug: str) -> Response:
        async with session_scope() as session:
            hack = await get_by_slug(session, slug)
            if hack is None:
                raise HTTPException(status_code=404, detail="not found")
            events = await list_events(session, hack.id)
            payload = build_calendar(hack, events)
        return Response(
            content=payload,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="{slug}.ics"',
                # Calendar clients poll on their own schedule; a short TTL keeps
                # a moved deadline from sitting stale for hours.
                "Cache-Control": "public, max-age=300",
            },
        )

    return app
