"""Structured shapes the LLM fills in.

Dates stay strings here on purpose: models emit all sorts of ISO-ish text, and
`parse_iso` is far more forgiving than pydantic's datetime coercion. Validation
happens on the way into the domain, not on the way out of the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LinkKindLiteral = Literal[
    "site", "rules", "chat", "channel", "stream", "table", "form", "repo", "other"
]
EventKindLiteral = Literal[
    "registration", "tech_check", "start", "checkpoint", "mentor", "code_freeze",
    "submission", "defense", "results", "afterparty", "other",
]


class ExtractedLink(BaseModel):
    kind: LinkKindLiteral = "other"
    url: str
    title: str | None = None


class ExtractedEvent(BaseModel):
    """One point on the timeline.

    `starts_at` is required on purpose: an optional date field gets skipped by the
    model far too often, and an event without a time is useless to a timeline bot.
    """

    title: str = Field(description="Короткое название этапа на русском, без слов начало/окончание")
    kind: EventKindLiteral = Field(description="Тип этапа")
    starts_at: str = Field(description="Начало, ISO-8601 местного времени: 2026-09-22T18:00")
    ends_at: str | None = Field(
        default=None,
        description=(
            "Окончание в том же формате. Используй его для интервалов, "
            "а не заводи отдельный этап"
        ),
    )
    place: str | None = Field(default=None, description="Площадка или зал, если указаны")
    url: str | None = None
    is_mandatory: bool = Field(default=False, description="True для сдачи, защиты и код-фриза")


class ExtractedHackathon(BaseModel):
    """Everything a poster, a PDF or a chat message might reveal about a hackathon."""

    title: str | None = Field(default=None, description="Название хакатона без слова хакатон")
    year: int | None = None
    organizer: str | None = None
    city: str | None = Field(default=None, description="Город проведения, null если онлайн")
    is_online: bool | None = None
    description: str | None = Field(
        default=None, description="1-3 предложения: о чём хакатон, треки, призовой фонд"
    )
    timezone: str | None = Field(
        default=None, description="IANA-имя, например Europe/Moscow. null если не указан"
    )

    starts_at: str | None = None
    ends_at: str | None = None
    reg_deadline: str | None = None

    events: list[ExtractedEvent] = Field(default_factory=list)
    links: list[ExtractedLink] = Field(default_factory=list)

    questions: list[str] = Field(
        default_factory=list,
        description="Уточняющие вопросы, если чего-то важного в источнике нет",
    )

    def is_empty(self) -> bool:
        return not any(
            (self.title, self.starts_at, self.ends_at, self.reg_deadline, self.events, self.links)
        )


class AgentAnswer(BaseModel):
    """Reply from the conversational agent."""

    reply: str = Field(description="Ответ пользователю на русском, готовый к отправке в чат")
    changed: bool = Field(
        default=False, description="True, если данные хакатона были изменены инструментами"
    )
