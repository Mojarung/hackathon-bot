"""Calendar export.

Two shapes are produced from the same data: a one-off `.ics` file to send into
the chat, and a stable feed URL people can subscribe to so later edits to the
timeline reach their calendar without anyone re-sending a file.
"""

from __future__ import annotations

from datetime import timedelta

from icalendar import Calendar
from icalendar import Event as IcsEvent

from hackbot.config import get_settings
from hackbot.db.models import Event, Hackathon
from hackbot.domain.services.hackathons import hack_tz
from hackbot.domain.textutils import slugify

PRODID = "-//hackbot//hackathon timeline//RU"
_DEFAULT_DURATION = timedelta(hours=1)


def _uid(hack: Hackathon, event: Event) -> str:
    return f"hackbot-{hack.id}-{event.id}@hackathon.bot"


def build_calendar(hack: Hackathon, events: list[Event]) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", f"{hack.title} {hack.year}")
    cal.add("x-wr-timezone", hack.tz)

    tz = hack_tz(hack)

    for event in sorted(events, key=lambda e: e.starts_at):
        item = IcsEvent()
        item.add("uid", _uid(hack, event))
        item.add("summary", f"{event.kind.emoji} {event.title}")
        item.add("dtstart", event.starts_at.astimezone(tz))
        item.add("dtend", (event.ends_at or event.starts_at + _DEFAULT_DURATION).astimezone(tz))
        item.add("dtstamp", event.created_at)
        item.add("sequence", 0)

        description: list[str] = [f"{hack.title} {hack.year}"]
        if event.notes:
            description.append(event.notes)
        if event.is_mandatory:
            description.append("Обязательное участие")
        item.add("description", "\n\n".join(description))

        if event.place:
            item.add("location", event.place)
        if event.url:
            item.add("url", event.url)

        # A calendar alarm is free redundancy if someone mutes the chat.
        if event.kind.is_critical:
            from icalendar import Alarm

            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", f"Скоро: {event.title}")
            alarm.add("trigger", timedelta(minutes=-60))
            item.add_component(alarm)

        cal.add_component(item)

    return cal.to_ical()


def ics_filename(hack: Hackathon) -> str:
    return f"{slugify(hack.title)}_{hack.year}.ics"


def feed_url(hack: Hackathon) -> str:
    """Public subscription URL, empty when WEB_PUBLIC_URL is not configured."""
    settings = get_settings()
    return settings.public_url(f"ics/{hack.slug}.ics")


def google_calendar_link(hack: Hackathon, event: Event) -> str:
    """One-click add for a single event, handy under a reminder."""
    from urllib.parse import quote_plus

    start = event.starts_at.strftime("%Y%m%dT%H%M%SZ")
    end = (event.ends_at or event.starts_at + _DEFAULT_DURATION).strftime("%Y%m%dT%H%M%SZ")
    details = quote_plus(f"{hack.title} {hack.year}")
    text = quote_plus(event.title)
    location = quote_plus(event.place or "")
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={text}&dates={start}/{end}&details={details}&location={location}"
    )
