"""Typed callback payloads. Keeping them in one place stops the string soup."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class RsvpCb(CallbackData, prefix="rsvp"):
    event_id: int
    status: str


class CardCb(CallbackData, prefix="card"):
    action: str          # timeline | team | links | docs | info | ics | refresh
    hack_id: int


class EventCb(CallbackData, prefix="ev"):
    action: str          # show | who | del | remind
    event_id: int


class ConfirmCb(CallbackData, prefix="cfm"):
    action: str          # apply | drop
    token: str           # key into the pending-proposal store


class PickCb(CallbackData, prefix="pick"):
    kind: str            # role | link | status | tz
    value: str
    ref: int = 0
