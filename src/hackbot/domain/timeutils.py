"""Time handling and Russian-language formatting.

Everything is stored in UTC and rendered in the hackathon's own timezone. Naive
datetimes are treated as UTC, because that is the only thing SQLite hands back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS_NOM = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAYS_FULL = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)

# ---------------------------------------------------------------- basics


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """Normalise anything into an aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return as_utc(dt).astimezone(tz)


def local_naive_to_utc(dt: datetime, tz: ZoneInfo) -> datetime:
    """Interpret a naive wall-clock datetime as local time in `tz`."""
    return dt.replace(tzinfo=tz).astimezone(UTC)


# ---------------------------------------------------------------- plurals


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural selection: 1 час / 2 часа / 5 часов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def n_plural(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural_ru(n, one, few, many)}"


# ---------------------------------------------------------------- formatting


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    return to_local(dt, tz).strftime("%H:%M")


def fmt_date(dt: datetime, tz: ZoneInfo, *, with_year: bool = False) -> str:
    d = to_local(dt, tz)
    out = f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"
    if with_year:
        out += f" {d.year}"
    return out


def fmt_dt(dt: datetime, tz: ZoneInfo, *, with_year: bool = False) -> str:
    return f"{fmt_date(dt, tz, with_year=with_year)}, {fmt_time(dt, tz)}"


def fmt_dt_short(dt: datetime, tz: ZoneInfo) -> str:
    return to_local(dt, tz).strftime("%d.%m %H:%M")


def fmt_range(start: datetime, end: datetime | None, tz: ZoneInfo) -> str:
    """A same-day range collapses to one date with two times."""
    if end is None:
        return fmt_dt(start, tz)
    s, e = to_local(start, tz), to_local(end, tz)
    if s.date() == e.date():
        return f"{fmt_dt(start, tz)} – {fmt_time(end, tz)}"
    return f"{fmt_dt(start, tz)} – {fmt_dt(end, tz)}"


def relative_day(dt: datetime, tz: ZoneInfo, now: datetime | None = None) -> str | None:
    """Returns сегодня / завтра / послезавтра / вчера, else None."""
    now = now or now_utc()
    delta_days = (to_local(dt, tz).date() - to_local(now, tz).date()).days
    return {-1: "вчера", 0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(delta_days)


def fmt_when(dt: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """Human phrasing that prefers a relative day over a bare date."""
    rel = relative_day(dt, tz, now)
    if rel:
        return f"{rel} в {fmt_time(dt, tz)}"
    return fmt_dt(dt, tz)


def humanize_delta(seconds: float, *, parts: int = 2) -> str:
    """Renders as `2 дня 14 ч`, `3 ч 40 мин`, `12 мин`."""
    total = int(abs(seconds))
    if total < 60:
        return "меньше минуты"

    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    chunks: list[str] = []
    if days:
        chunks.append(n_plural(days, "день", "дня", "дней"))
    if hours:
        chunks.append(f"{hours} ч")
    if minutes and len(chunks) < parts:
        chunks.append(f"{minutes} мин")
    return " ".join(chunks[:parts]) or "меньше минуты"


def countdown(seconds: float) -> str:
    """Monospace countdown: `09:42:15` under a day, `2 д 14:22` above."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days} д {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def tg_time(dt: datetime, fallback: str, fmt: str = "r") -> str:
    """Bot API 10.3 self-updating timestamp. The inner text is what older clients
    render, so the fallback is never wasted."""
    unix = int(as_utc(dt).timestamp())
    return f'<tg-time unix="{unix}" format="{fmt}">{fallback}</tg-time>'


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True, slots=True)
class ParsedDt:
    dt: datetime          # aware, UTC
    has_time: bool        # False => only a date was given, time defaulted to 00:00


_TIME_RE = r"(?:в\s*)?(?P<h>[0-2]?\d)[:.](?P<m>[0-5]\d)(?!\s*[./]\s*\d)"
_DMY_RE = r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b"
_MONTH_RE = r"\b(\d{1,2})\s+([а-я]{3,})"

_MONTH_WORDS: dict[str, int] = {}
for _i, (_gen, _nom) in enumerate(zip(MONTHS_GEN, MONTHS_NOM, strict=True), start=1):
    _MONTH_WORDS[_gen[:3]] = _i
    _MONTH_WORDS[_nom[:3]] = _i


def _cut(text: str, match: re.Match[str]) -> str:
    return (text[: match.start()] + " " + text[match.end():]).strip(" ,")


def _extract_time(text: str) -> tuple[int, int, str] | None:
    """Pull a `HH:MM` (or `HH.MM`) out of the text and return the remainder.

    Runs only after date patterns have been removed, otherwise `20.09` in
    `20.09 18:00` would be swallowed as a time.
    """
    for m in re.finditer(_TIME_RE, text):
        h, mi = int(m.group("h")), int(m.group("m"))
        if h > 23:
            continue
        return h, mi, _cut(text, m)
    return None


def _extract_date(raw: str, now_local: datetime) -> tuple[int, int, int, str] | None:
    """Find an explicit calendar date and return (y, m, d, remaining_text)."""
    # dd.mm[.yyyy] - rejected unless it forms a real calendar date, so that a
    # dotted time like `18.00` falls through to the time parser.
    for m in re.finditer(_DMY_RE, raw):
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        if m.group(3):
            y = int(m.group(3))
            y += 2000 if y < 100 else 0
        else:
            y = now_local.year
            if (mo, d) < (now_local.month, now_local.day):
                y += 1  # a bare day.month already past means next year
        return y, mo, d, _cut(raw, m)

    # `20 сентября` / `20 сент`
    for m in re.finditer(_MONTH_RE, raw):
        mo = _MONTH_WORDS.get(m.group(2)[:3])
        if not mo:
            continue
        d = int(m.group(1))
        y = now_local.year
        if (mo, d) < (now_local.month, now_local.day):
            y += 1
        return y, mo, d, _cut(raw, m)

    return None


def parse_dt(text: str, tz: ZoneInfo, now: datetime | None = None) -> ParsedDt | None:
    """Parse the date formats a human would actually type into a chat.

    Understood: ISO, `20.09.2026 18:00`, `20.09 18:00`, `20 сентября 18:00`,
    `сегодня/завтра/послезавтра 18:00`, `пт 18:00`, and a bare `18:00`.
    Anything more exotic is the LLM agent job, not this function.

    Dates are resolved before times, so `01.03` reads as 1 March rather than
    01:03 - the right bias for a calendar bot.
    """
    if not text:
        return None
    raw = text.strip().casefold().replace("ё", "е")
    now_local = to_local(now or now_utc(), tz)

    # ISO first - unambiguous, so it short-circuits everything else.
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ](\d{1,2}):(\d{2}))?", raw)
    if iso:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if iso.group(4):
            return _build(y, mo, d, int(iso.group(4)), int(iso.group(5)), tz, True)
        return _build(y, mo, d, 0, 0, tz, False)

    explicit = _extract_date(raw, now_local)
    rest = explicit[3] if explicit else raw

    parsed_time = _extract_time(rest)
    if parsed_time:
        hh, mm, rest = parsed_time
        has_time = True
    else:
        hh, mm, has_time = 0, 0, False

    if explicit:
        y, mo, d, _ = explicit
        return _build(y, mo, d, hh, mm, tz, has_time)

    # relative days
    for offset, name in ((2, "послезавтра"), (1, "завтра"), (0, "сегодня")):
        if name in rest:
            target = now_local.date() + timedelta(days=offset)
            return _build(target.year, target.month, target.day, hh, mm, tz, has_time)

    # weekday name -> next occurrence
    for idx, short in enumerate(WEEKDAYS_SHORT):
        full = WEEKDAYS_FULL[idx]
        if re.search(rf"\b({short}|{full[:4]}[а-я]*)\b", rest):
            ahead = (idx - now_local.weekday()) % 7
            if ahead == 0 and (not has_time or (hh, mm) <= (now_local.hour, now_local.minute)):
                ahead = 7
            target = now_local.date() + timedelta(days=ahead)
            return _build(target.year, target.month, target.day, hh, mm, tz, has_time)

    # bare time -> today, or tomorrow if it already passed
    if has_time:
        target = now_local.date()
        if (hh, mm) <= (now_local.hour, now_local.minute):
            target += timedelta(days=1)
        return _build(target.year, target.month, target.day, hh, mm, tz, True)

    return None


def _build(
    y: int, mo: int, d: int, hh: int, mm: int, tz: ZoneInfo, has_time: bool
) -> ParsedDt | None:
    try:
        naive = datetime(y, mo, d, hh, mm)
    except ValueError:
        return None
    return ParsedDt(dt=local_naive_to_utc(naive, tz), has_time=has_time)


def parse_iso(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO-8601 string produced by the LLM. Naive input is local time."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        parsed = parse_dt(value, tz)
        return parsed.dt if parsed else None
    return as_utc(dt) if dt.tzinfo else local_naive_to_utc(dt, tz)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC half-open range covering one local calendar day."""
    start = local_naive_to_utc(datetime(day.year, day.month, day.day), tz)
    return start, start + timedelta(days=1)
