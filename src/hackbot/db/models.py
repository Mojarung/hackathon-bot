"""ORM schema.

One hackathon is bound to exactly one forum topic (chat_id + thread_id); every
other table hangs off it and cascades on delete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hackbot.db.base import Base, EnumType
from hackbot.domain.enums import EventKind, HackStatus, LinkKind, RsvpStatus
from hackbot.domain.timeutils import now_utc


class Hackathon(Base):
    __tablename__ = "hackathon"
    __table_args__ = (
        Index("ix_hackathon_topic", "chat_id", "thread_id"),
        Index("ix_hackathon_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(200))
    year: Mapped[int] = mapped_column(Integer, default=lambda: now_utc().year)

    organizer: Mapped[str | None] = mapped_column(String(200), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    description: Mapped[str | None] = mapped_column(Text, default=None)

    starts_at: Mapped[datetime | None] = mapped_column(default=None)
    ends_at: Mapped[datetime | None] = mapped_column(default=None)
    reg_deadline: Mapped[datetime | None] = mapped_column(default=None)

    status: Mapped[HackStatus] = mapped_column(EnumType(HackStatus, 24), default=HackStatus.DRAFT)

    # Telegram binding
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger, default=None)

    # GitHub
    github_repo: Mapped[str | None] = mapped_column(String(200), default=None)
    github_url: Mapped[str | None] = mapped_column(String(400), default=None)

    # Outcome, filled in once the dust settles
    result_place: Mapped[str | None] = mapped_column(String(80), default=None)
    result_note: Mapped[str | None] = mapped_column(Text, default=None)

    last_digest_on: Mapped[str | None] = mapped_column(String(10), default=None)  # local ISO date
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)

    events: Mapped[list[Event]] = relationship(
        back_populates="hackathon", cascade="all, delete-orphan", lazy="selectin"
    )
    links: Mapped[list[Link]] = relationship(
        back_populates="hackathon", cascade="all, delete-orphan", lazy="selectin"
    )
    participants: Mapped[list[Participant]] = relationship(
        back_populates="hackathon", cascade="all, delete-orphan", lazy="selectin"
    )
    docs: Mapped[list[Doc]] = relationship(
        back_populates="hackathon", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_live(self) -> bool:
        return self.status not in {HackStatus.FINISHED, HackStatus.ARCHIVED}


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (Index("ix_event_hack_start", "hackathon_id", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hackathon_id: Mapped[int] = mapped_column(
        ForeignKey("hackathon.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[EventKind] = mapped_column(EnumType(EventKind, 24), default=EventKind.OTHER)
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(index=True)
    ends_at: Mapped[datetime | None] = mapped_column(default=None)
    place: Mapped[str | None] = mapped_column(String(200), default=None)
    url: Mapped[str | None] = mapped_column(String(400), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_rsvp: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=now_utc)

    hackathon: Mapped[Hackathon] = relationship(back_populates="events")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    rsvps: Mapped[list[Rsvp]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )


class Reminder(Base):
    """A single scheduled ping. `fire_at` is denormalised from the event so the
    scheduler can find due work with one indexed query."""

    __tablename__ = "reminder"
    __table_args__ = (
        UniqueConstraint("event_id", "offset_minutes", name="uq_reminder_event_offset"),
        Index("ix_reminder_pending", "sent_at", "fire_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    offset_minutes: Mapped[int] = mapped_column(Integer)
    fire_at: Mapped[datetime] = mapped_column(index=True)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    message_id: Mapped[int | None] = mapped_column(BigInteger, default=None)

    event: Mapped[Event] = relationship(back_populates="reminders")


class Participant(Base):
    __tablename__ = "participant"
    __table_args__ = (
        UniqueConstraint("hackathon_id", "tg_user_id", name="uq_participant_hack_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hackathon_id: Mapped[int] = mapped_column(
        ForeignKey("hackathon.id", ondelete="CASCADE"), index=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str | None] = mapped_column(String(64), default=None)
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    joined_at: Mapped[datetime] = mapped_column(default=now_utc)

    hackathon: Mapped[Hackathon] = relationship(back_populates="participants")

    @property
    def is_placeholder(self) -> bool:
        """Added by name; the real Telegram id is not known yet."""
        return self.tg_user_id < 0

    @property
    def display(self) -> str:
        if self.full_name:
            return self.full_name
        if self.username:
            return f"@{self.username}"
        return "участник" if self.is_placeholder else str(self.tg_user_id)

    @property
    def mention_html(self) -> str:
        """tg://user links work even for people without a username - but only for
        real ids, so someone added by name falls back to a plain @handle."""
        if self.is_placeholder:
            return f"@{self.username}" if self.username else self.display
        return f'<a href="tg://user?id={self.tg_user_id}">{self.display}</a>'


class Rsvp(Base):
    __tablename__ = "rsvp"
    __table_args__ = (UniqueConstraint("event_id", "tg_user_id", name="uq_rsvp_event_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[RsvpStatus] = mapped_column(EnumType(RsvpStatus, 12))
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)

    event: Mapped[Event] = relationship(back_populates="rsvps")


class Link(Base):
    __tablename__ = "link"

    id: Mapped[int] = mapped_column(primary_key=True)
    hackathon_id: Mapped[int] = mapped_column(
        ForeignKey("hackathon.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[LinkKind] = mapped_column(EnumType(LinkKind, 24), default=LinkKind.OTHER)
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    url: Mapped[str] = mapped_column(String(600))
    created_at: Mapped[datetime] = mapped_column(default=now_utc)

    hackathon: Mapped[Hackathon] = relationship(back_populates="links")


class Doc(Base):
    __tablename__ = "doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    hackathon_id: Mapped[int] = mapped_column(
        ForeignKey("hackathon.id", ondelete="CASCADE"), index=True
    )
    file_name: Mapped[str] = mapped_column(String(300))
    tg_file_id: Mapped[str | None] = mapped_column(String(300), default=None)
    local_path: Mapped[str | None] = mapped_column(String(600), default=None)
    mime: Mapped[str | None] = mapped_column(String(120), default=None)
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    github_path: Mapped[str | None] = mapped_column(String(400), default=None)
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_utc)

    hackathon: Mapped[Hackathon] = relationship(back_populates="docs")


class AuditLog(Base):
    """Every mutation is announced in the topic; this is the durable copy."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    hackathon_id: Mapped[int | None] = mapped_column(
        ForeignKey("hackathon.id", ondelete="CASCADE"), index=True, default=None
    )
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    actor: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_utc, index=True)


class AgentThread(Base):
    """Serialised pydantic-ai history so a clarifying question survives a restart."""

    __tablename__ = "agent_thread"
    __table_args__ = (UniqueConstraint("chat_id", "thread_id", name="uq_agent_thread_topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    history: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)


class KV(Base):
    """Small operational counters that do not deserve their own table."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)


class ChatUser(Base):
    """Who the bot is talking to, remembered across topics and hackathons.

    Keyed by the Telegram user id rather than the @username, because a username
    can be changed or dropped at any moment while the id never moves. The three
    free-text fields are what the bot learns from conversation; identity and the
    counters are filled in passively from every message it sees.
    """

    __tablename__ = "chat_user"
    __table_args__ = (UniqueConstraint("tg_user_id", name="uq_chat_user_tg"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    # What the person prefers to be called, when that differs from full_name.
    alias: Mapped[str | None] = mapped_column(String(80), default=None)

    about: Mapped[str | None] = mapped_column(Text, default=None)   # кто он, чем занимается
    traits: Mapped[str | None] = mapped_column(Text, default=None)  # характер, манера речи
    notes: Mapped[str | None] = mapped_column(Text, default=None)   # всё остальное

    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(default=now_utc)
    last_seen: Mapped[datetime] = mapped_column(default=now_utc)

    @property
    def display(self) -> str:
        return self.alias or self.full_name or (f"@{self.username}" if self.username else "аноним")

    @property
    def handle(self) -> str:
        """Name plus @username, for lines the model reads."""
        return f"{self.display} (@{self.username})" if self.username else self.display

    @property
    def is_known(self) -> bool:
        """True once the bot has learned something beyond the Telegram identity."""
        return any((self.about, self.traits, self.notes, self.alias))

    @property
    def facts(self) -> str:
        """Everything learned about the person, without repeating their name."""
        bits = []
        if self.about:
            bits.append(self.about)
        if self.traits:
            bits.append(f"характер: {self.traits}")
        if self.notes:
            bits.append(self.notes)
        return "; ".join(bits)

    def summary(self) -> str:
        """One line about the person, for prompts that only need who they are."""
        facts = self.facts
        return f"{self.handle} — {facts}" if facts else self.handle

    def roster_line(self) -> str:
        """One line per person for the roster the agent reads on every run.

        Spells out the telegram id because everything the bot remembers hangs off
        it. A model that echoes back "Ярослав" sends `remember_person` hunting
        through names; one that echoes the id cannot land on the wrong person.
        """
        facts = self.facts
        head = f"{self.handle}, id {self.tg_user_id}"
        return f"{head} — {facts}" if facts else head
