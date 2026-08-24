"""HTML for the two public pages.

Inline rather than file-based: two templates do not justify a loader, and this
keeps the page self-contained with no static assets to serve.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from hackbot.db.models import Event, Hackathon
from hackbot.domain.textutils import esc
from hackbot.domain.timeutils import (
    MONTHS_GEN,
    WEEKDAYS_SHORT,
    fmt_time,
    humanize_delta,
    to_local,
)

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f6f4; --fg: #16161a; --muted: #6b6b76;
  --card: #ffffff; --line: #e3e3df; --accent: #2f6f4f;
  --done: #9a9aa4; --live: #b8452f;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#111114; --fg:#ececed; --muted:#9a9aa4;
          --card:#1a1a1f; --line:#2a2a31; --accent:#7fc9a0; --live:#ff8b6b; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2.5rem 1.25rem 5rem; background:var(--bg); color:var(--fg);
  font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Inter,sans-serif; }
.wrap { max-width: 46rem; margin: 0 auto; }
h1 { font-size: clamp(1.7rem, 1.1rem + 2.4vw, 2.6rem); line-height:1.1;
  letter-spacing:-.02em; margin:0 0 .35rem; }
.sub { color:var(--muted); margin:0 0 2rem; }
.badge { display:inline-block; padding:.2rem .6rem; border:1px solid var(--line);
  border-radius:2rem; font-size:.8rem; color:var(--muted); background:var(--card); }
.count { background:var(--card); border:1px solid var(--line); border-radius:.9rem;
  padding:1.1rem 1.25rem; margin-bottom:2rem; }
.count b { font-size:1.5rem; letter-spacing:-.02em; }
.bar { height:6px; background:var(--line); border-radius:99px; overflow:hidden; margin-top:.9rem; }
.bar span { display:block; height:100%; background:var(--accent); }
.day { margin:2rem 0 .75rem; font-weight:650; letter-spacing:-.01em; }
ol { list-style:none; margin:0; padding:0; }
li { display:grid; grid-template-columns:4.2rem 1fr; gap:.9rem;
  padding:.7rem 0; border-top:1px solid var(--line); }
li .t { color:var(--muted); font-variant-numeric:tabular-nums; padding-top:.1rem; }
li .n { font-weight:600; }
li .m { color:var(--muted); font-size:.9rem; }
li.done .n { color:var(--done); text-decoration:line-through; }
li.live .n { color:var(--live); }
a { color:inherit; text-underline-offset:.2em; }
.links { margin-top:2.5rem; display:flex; flex-wrap:wrap; gap:.5rem; }
.links a { text-decoration:none; padding:.45rem .85rem; border:1px solid var(--line);
  border-radius:.6rem; background:var(--card); font-size:.9rem; }
.foot { margin-top:3.5rem; color:var(--muted); font-size:.85rem; }
.empty { color:var(--muted); padding:2rem 0; }
"""


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


def render_index(rows: list[dict[str, Any]]) -> str:
    if not rows:
        body = "<h1>Хакатоны</h1><p class=empty>Пока пусто.</p>"
        return _shell("Хакатоны", body)

    items = []
    for row in rows:
        result = (
            f' <span class=badge>🏆 {esc(row["result"])}</span>' if row.get("result") else ""
        )
        items.append(
            f'<li><div class=t>{row["year"]}</div><div>'
            f'<div class=n><a href="/h/{esc(row["slug"])}">{esc(row["title"])}</a></div>'
            f'<div class=m>{row["status"].emoji} {esc(row["status"].label)} · '
            f'{esc(row["when"])}{result}</div></div></li>'
        )
    body = f"<h1>Хакатоны</h1><ol>{''.join(items)}</ol>"
    return _shell("Хакатоны", body)


def render_page(
    *,
    hack: Hackathon,
    events: list[Event],
    tz: ZoneInfo,
    now: datetime,
    ratio: float | None,
    countdown: dict[str, str] | None,
) -> str:
    header = [
        f"<h1>{esc(hack.title)}</h1>",
        '<p class=sub>'
        + esc(
            " · ".join(
                x
                for x in (
                    hack.city or ("онлайн" if hack.is_online else None),
                    str(hack.year),
                    hack.status.label,
                )
                if x
            )
        )
        + "</p>",
    ]

    if countdown:
        bar = (
            f'<div class=bar><span style="width:{ratio * 100:.0f}%"></span></div>'
            if ratio is not None
            else ""
        )
        header.append(
            "<div class=count>"
            f'<div class=m>до «{esc(countdown["title"].lower())}»</div>'
            f'<b>{esc(countdown["left"])}</b>{bar}</div>'
        )

    if not events:
        return _shell(hack.title, "".join(header) + "<p class=empty>Этапы ещё не заданы.</p>")

    chunks: list[str] = []
    current_day = None
    for event in sorted(events, key=lambda e: e.starts_at):
        local = to_local(event.starts_at, tz)
        if local.date() != current_day:
            if current_day is not None:
                chunks.append("</ol>")
            current_day = local.date()
            label = (
                f"{WEEKDAYS_SHORT[local.weekday()]}, "
                f"{local.day} {MONTHS_GEN[local.month - 1]}"
            )
            chunks.append(f"<div class=day>{esc(label)}</div><ol>")

        state = ""
        if event.ends_at and event.starts_at <= now < event.ends_at:
            state = " class=live"
        elif event.starts_at <= now:
            state = " class=done"

        meta: list[str] = []
        if event.ends_at:
            meta.append(f"до {fmt_time(event.ends_at, tz)}")
        if event.starts_at > now:
            meta.append(f"через {humanize_delta((event.starts_at - now).total_seconds())}")
        if event.place:
            meta.append(esc(event.place))
        meta_html = f'<div class=m>{" · ".join(meta)}</div>' if meta else ""

        name = esc(event.title)
        if event.url:
            name = f'<a href="{esc(event.url)}" rel=noopener>{name}</a>'

        chunks.append(
            f"<li{state}><div class=t>{fmt_time(event.starts_at, tz)}</div>"
            f"<div><div class=n>{event.kind.emoji} {name}</div>{meta_html}</div></li>"
        )
    chunks.append("</ol>")

    links = "".join(
        f'<a href="{esc(link.url)}" rel=noopener>{link.kind.emoji} '
        f"{esc(link.title or link.kind.label)}</a>"
        for link in hack.links
    )
    links += f'<a href="/ics/{esc(hack.slug)}.ics">📥 В календарь</a>'
    if hack.github_url:
        links += f'<a href="{esc(hack.github_url)}" rel=noopener>🐙 Репозиторий</a>'

    foot = f'<p class=foot>Обновлено {fmt_time(now, tz)} · {esc(tz.key)}</p>'
    return _shell(
        f"{hack.title} {hack.year}",
        "".join(header) + "".join(chunks) + f"<div class=links>{links}</div>" + foot,
    )
