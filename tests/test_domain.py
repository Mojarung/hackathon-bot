"""Unit tests for the pure logic: date parsing, text handling, LLM output repair."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hackbot.agent.schemas import ExtractedEvent, ExtractedHackathon, ExtractedLink
from hackbot.bot.handlers.timeline import _split_title_and_date
from hackbot.domain.enums import EventKind, HackStatus
from hackbot.domain.services.ingest import build_plan
from hackbot.domain.textutils import find_urls, progress_bar, repo_name, safe_filename, slugify
from hackbot.domain.timeutils import (
    countdown,
    fmt_dt,
    humanize_delta,
    n_plural,
    parse_dt,
    parse_iso,
)

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)  # понедельник, 18:00 MSK


# ---------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-20 18:00", "вс, 20 сентября 2026, 18:00"),
        ("20.09.2026 18:00", "вс, 20 сентября 2026, 18:00"),
        ("20.09 18:00", "вс, 20 сентября 2026, 18:00"),
        ("20 сентября 18:00", "вс, 20 сентября 2026, 18:00"),
        ("20 сент 18:00", "вс, 20 сентября 2026, 18:00"),
        ("завтра 18:00", "вт, 25 августа 2026, 18:00"),
        ("сегодня 23:30", "пн, 24 августа 2026, 23:30"),
        ("пт 18:00", "пт, 28 августа 2026, 18:00"),
    ],
)
def test_parse_dt_formats(text: str, expected: str) -> None:
    parsed = parse_dt(text, TZ, NOW)
    assert parsed is not None, text
    assert fmt_dt(parsed.dt, TZ, with_year=True) == expected


def test_parse_dt_prefers_date_over_dotted_time() -> None:
    """`01.03` is 1 March, not 01:03 - the right bias for a calendar bot."""
    parsed = parse_dt("01.03", TZ, NOW)
    assert parsed is not None
    assert not parsed.has_time
    assert (parsed.dt.astimezone(TZ).day, parsed.dt.astimezone(TZ).month) == (1, 3)


def test_parse_dt_bare_time_rolls_to_tomorrow_when_past() -> None:
    parsed = parse_dt("09:00", TZ, NOW)  # 09:00 MSK already gone at 18:00
    assert parsed is not None
    assert parsed.dt.astimezone(TZ).day == 25


def test_parse_dt_rejects_nonsense() -> None:
    assert parse_dt("мусор без даты", TZ, NOW) is None
    assert parse_dt("", TZ, NOW) is None


def test_parse_iso_treats_naive_as_local() -> None:
    moment = parse_iso("2026-09-22T18:00", TZ)
    assert moment is not None
    assert moment == datetime(2026, 9, 22, 15, 0, tzinfo=UTC)


def test_humanize_and_plural() -> None:
    assert humanize_delta(2 * 86400 + 14 * 3600) == "2 дня 14 ч"
    assert humanize_delta(3 * 3600 + 40 * 60) == "3 ч 40 мин"
    assert humanize_delta(30) == "меньше минуты"
    assert [n_plural(n, "день", "дня", "дней") for n in (1, 3, 5, 11, 21)] == [
        "1 день", "3 дня", "5 дней", "11 дней", "21 день",
    ]
    assert countdown(9 * 3600 + 42 * 60 + 15) == "09:42:15"
    assert countdown(2 * 86400 + 14 * 3600 + 22 * 60) == "2 д 14:22"


# ---------------------------------------------------------------- text


def test_slug_and_repo_name() -> None:
    assert slugify("Тендер Хак Нижний") == "tender_hak_nizhniy"
    assert repo_name("ТендерХак", 2026) == "tenderhak_2026"
    assert slugify("!!!") == "hackathon"


def test_safe_filename_strips_paths() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename('bad:name*.pdf') == "bad_name_.pdf"
    assert safe_filename("") == "file"


def test_find_urls_and_progress_bar() -> None:
    urls = find_urls("см. https://a.example/x, и https://b.example.")
    assert urls == ["https://a.example/x", "https://b.example"]
    assert progress_bar(0.0) == "░" * 10
    assert progress_bar(1.0) == "▓" * 10
    assert progress_bar(2.0) == "▓" * 10  # clamped


def test_event_kind_guess() -> None:
    assert EventKind.guess("код-фриз") is EventKind.CODE_FREEZE
    assert EventKind.guess("Загрузка решения") is EventKind.SUBMISSION
    assert EventKind.guess("Питчи проектов") is EventKind.DEFENSE
    assert EventKind.guess("Объявление итогов") is EventKind.RESULTS
    assert EventKind.guess("Просто встреча") is EventKind.OTHER


# ---------------------------------------------------------------- /add parsing


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("Защита 22.09 20:00", "Защита"),
        ("Сдача решения 2026-09-22 18:00", "Сдача решения"),
        ("Код-фриз завтра 18:00", "Код-фриз"),
        ("22.09 20:00", ""),
    ],
)
def test_split_title_and_date(text: str, title: str) -> None:
    got_title, moment = _split_title_and_date(text, TZ)
    assert got_title == title
    assert moment is not None


def test_split_returns_none_without_date() -> None:
    title, moment = _split_title_and_date("Митап без даты", TZ)
    assert moment is None
    assert title == "Митап без даты"


# ---------------------------------------------------------------- ingest repair


def _extracted(**kwargs) -> ExtractedHackathon:
    base = {"title": "ТендерХак", "year": 2026, "city": "Нижний Новгород"}
    return ExtractedHackathon(**(base | kwargs))


def test_ingest_collapses_deadline_interval() -> None:
    """`до 22.09 18:00` arrives as midnight..18:00; the instant is the end."""
    data = _extracted(
        events=[
            ExtractedEvent(
                title="Загрузка решения", kind="submission",
                starts_at="2026-09-22T00:00", ends_at="2026-09-22T18:00",
            )
        ]
    )
    plan = build_plan(data, TZ)
    event = plan.events[0]
    assert event.ends_at is None
    assert event.starts_at.astimezone(TZ).hour == 18


def test_ingest_drops_echoed_end_time() -> None:
    data = _extracted(
        events=[
            ExtractedEvent(
                title="Открытие", kind="start",
                starts_at="2026-09-20T10:00", ends_at="2026-09-20T10:00",
            )
        ]
    )
    assert build_plan(data, TZ).events[0].ends_at is None


def test_ingest_keeps_real_interval() -> None:
    data = _extracted(
        events=[
            ExtractedEvent(
                title="Защита", kind="defense",
                starts_at="2026-09-22T20:00", ends_at="2026-09-22T23:00",
            )
        ]
    )
    event = build_plan(data, TZ).events[0]
    assert event.ends_at is not None
    assert event.ends_at.astimezone(TZ).hour == 23


def test_ingest_overrides_mandatory_flag() -> None:
    """The model sprays is_mandatory around; the kind is the reliable signal."""
    data = _extracted(
        events=[
            ExtractedEvent(title="Чек-поинт", kind="checkpoint",
                           starts_at="2026-09-21T12:00", is_mandatory=True),
            ExtractedEvent(title="Сдача", kind="submission",
                           starts_at="2026-09-22T18:00", is_mandatory=False),
        ]
    )
    by_kind = {e.kind: e for e in build_plan(data, TZ).events}
    assert by_kind[EventKind.CHECKPOINT].is_mandatory is False
    assert by_kind[EventKind.SUBMISSION].is_mandatory is True


def test_ingest_dedupes_near_duplicates() -> None:
    data = _extracted(
        events=[
            ExtractedEvent(title="Защита", kind="defense", starts_at="2026-09-22T20:00"),
            ExtractedEvent(title="Защита проектов", kind="defense",
                           starts_at="2026-09-22T20:30"),
        ]
    )
    assert len(build_plan(data, TZ).events) == 1


def test_ingest_swaps_reversed_hackathon_dates() -> None:
    data = _extracted(starts_at="2026-09-22T18:00", ends_at="2026-09-20T10:00")
    plan = build_plan(data, TZ)
    assert plan.fields["starts_at"] < plan.fields["ends_at"]
    assert plan.notes


def test_ingest_strips_year_and_city_from_title() -> None:
    data = _extracted(title="ТендерХак Нижний Новгород 2026")
    assert build_plan(data, TZ).fields["title"] == "ТендерХак"


def test_ingest_never_overwrites_known_value_with_none() -> None:
    class Existing:
        title = "ТендерХак"
        year = 2026
        city = "Нижний Новгород"
        organizer = "Портал поставщиков"
        description = None
        starts_at = None
        ends_at = None
        reg_deadline = None
        is_online = False

    data = _extracted(organizer=None, starts_at="2026-09-20T10:00")
    plan = build_plan(data, TZ, existing=Existing())  # type: ignore[arg-type]
    assert "organizer" not in plan.fields
    assert "starts_at" in plan.fields


def test_ingest_classifies_links() -> None:
    # The second link bypasses validation on purpose: the schema now pins `kind`
    # to a Literal, so this exercises the defensive fallback in build_plan for
    # the day a model returns something outside the enum anyway.
    rogue = ExtractedLink.model_construct(kind="bogus", url="https://t.me/x", title=None)
    data = _extracted(
        links=[ExtractedLink(kind="rules", url="tenderhack.ru/rules"), rogue]
    )
    plan = build_plan(data, TZ)
    kinds = [kind.value for kind, _, _ in plan.links]
    assert kinds == ["rules", "other"]
    assert plan.links[0][1].startswith("https://")  # bare host gets a scheme


def test_ingest_drops_undated_events() -> None:
    data = _extracted(
        events=[ExtractedEvent(title="Что-то", kind="other", starts_at="непонятно когда")]
    )
    assert build_plan(data, TZ).events == []


# ---------------------------------------------------------------- render safety


def test_card_renders_for_an_empty_hackathon() -> None:
    """A brand new hackathon has almost no data; the card must still be valid."""
    from hackbot.db.models import Hackathon
    from hackbot.render.card import render_card

    hack = Hackathon(
        id=1, slug="x", title="Хакатон", year=2026, tz="Europe/Moscow",
        chat_id=-1, thread_id=None, status=HackStatus.DRAFT, is_online=False,
    )
    hack.links = []
    text = render_card(hack, [], [], now=NOW)
    assert "ХАКАТОН" in text
    assert text.count("<b>") == text.count("</b>")
    assert "None" not in text


def test_card_renders_a_running_hackathon() -> None:
    from hackbot.db.models import Event, Hackathon
    from hackbot.render.card import render_card

    hack = Hackathon(
        id=1, slug="x", title="ТендерХак", year=2026, tz="Europe/Moscow",
        chat_id=-1, thread_id=7, status=HackStatus.RUNNING, is_online=False,
        starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(hours=9),
    )
    hack.links = []
    events = [
        Event(id=1, hackathon_id=1, kind=EventKind.SUBMISSION, title="Сдача решения",
              starts_at=NOW + timedelta(hours=9)),
    ]
    text = render_card(hack, events, [], now=NOW)
    assert "до сдачи решения" in text
    assert "%" in text
    assert text.count("<b>") == text.count("</b>")


# ---------------------------------------------------------------- reminders


def test_reminder_ladder_is_the_agreed_one() -> None:
    from hackbot.domain.services.events import STANDARD_LADDER, default_offsets

    assert STANDARD_LADDER == (4320, 1440, 180, 60, 15)          # 3 дня, сутки, 3 ч, час, 15 мин
    assert default_offsets(EventKind.CHECKPOINT) == STANDARD_LADDER
    # The submission deadline earns one extra nudge.
    assert 30 in default_offsets(EventKind.SUBMISSION)


@pytest.mark.parametrize(
    ("title", "filler"),
    [
        ("Ужин", True),
        ("Завтрак (Ресторан «CINEMA»)", True),
        ("Работа над проектом", True),
        ("Кофе-брейк", True),
        ("Защита проектов", False),
        ("Код-фриз", False),
        ("Чек-поинт", False),
    ],
)
def test_filler_events_get_no_reminders(title: str, filler: bool) -> None:
    """A printed programme lists meals; nobody wants a 3-day warning about dinner."""
    from hackbot.domain.services.events import is_filler

    assert is_filler(title) is filler


def test_placeholder_participants() -> None:
    from hackbot.db.models import Participant
    from hackbot.domain.services.participants import placeholder_id

    stub = Participant(
        hackathon_id=1, tg_user_id=placeholder_id("@fsfs192"),
        username="fsfs192", full_name="", role="судья",
    )
    real = Participant(hackathon_id=1, tg_user_id=555, username="kirill", full_name="Кирилл")
    assert stub.is_placeholder and not real.is_placeholder
    assert stub.mention_html == "@fsfs192"          # no tg:// link for a made-up id
    assert 'tg://user?id=555' in real.mention_html
    assert placeholder_id("@fsfs192") == placeholder_id("fsfs192")   # stable, handle-agnostic
