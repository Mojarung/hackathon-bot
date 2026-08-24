"""Domain vocabularies. Every enum carries its own Russian label and emoji so the
render layer never has to keep a parallel translation table."""

from __future__ import annotations

from enum import StrEnum


class HackStatus(StrEnum):
    DRAFT = "draft"
    ANNOUNCED = "announced"
    REGISTRATION = "registration"
    RUNNING = "running"
    JUDGING = "judging"
    FINISHED = "finished"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        return _HACK_LABELS[self][0]

    @property
    def emoji(self) -> str:
        return _HACK_LABELS[self][1]


_HACK_LABELS: dict[HackStatus, tuple[str, str]] = {
    HackStatus.DRAFT: ("черновик", "📝"),
    HackStatus.ANNOUNCED: ("анонсирован", "📢"),
    HackStatus.REGISTRATION: ("идёт регистрация", "🟡"),
    HackStatus.RUNNING: ("идёт", "🟢"),
    HackStatus.JUDGING: ("защита и судейство", "⚖️"),
    HackStatus.FINISHED: ("завершён", "🏁"),
    HackStatus.ARCHIVED: ("в архиве", "📦"),
}


class EventKind(StrEnum):
    REGISTRATION = "registration"
    TECH_CHECK = "tech_check"
    START = "start"
    CHECKPOINT = "checkpoint"
    MENTOR = "mentor"
    CODE_FREEZE = "code_freeze"
    SUBMISSION = "submission"
    DEFENSE = "defense"
    RESULTS = "results"
    AFTERPARTY = "afterparty"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _EVENT_LABELS[self][0]

    @property
    def emoji(self) -> str:
        return _EVENT_LABELS[self][1]

    @property
    def is_critical(self) -> bool:
        """Critical events always ping by name, even in quiet mode."""
        return self in {EventKind.SUBMISSION, EventKind.DEFENSE, EventKind.CODE_FREEZE}

    @classmethod
    def guess(cls, text: str) -> EventKind:
        """Best-effort mapping of free-form Russian/English wording onto a kind."""
        t = (text or "").casefold()
        for kind, needles in _EVENT_HINTS:
            if any(n in t for n in needles):
                return kind
        return cls.OTHER


_EVENT_LABELS: dict[EventKind, tuple[str, str]] = {
    EventKind.REGISTRATION: ("регистрация", "📋"),
    EventKind.TECH_CHECK: ("тех-чек", "🔌"),
    EventKind.START: ("старт", "🚀"),
    EventKind.CHECKPOINT: ("чек-поинт", "📍"),
    EventKind.MENTOR: ("менторская", "🧑‍🏫"),
    EventKind.CODE_FREEZE: ("код-фриз", "🧊"),
    EventKind.SUBMISSION: ("сдача решения", "📤"),
    EventKind.DEFENSE: ("защита", "🎤"),
    EventKind.RESULTS: ("результаты", "🏆"),
    EventKind.AFTERPARTY: ("афтепати", "🎉"),
    EventKind.OTHER: ("этап", "▫️"),
}

# Order matters: the first match wins, so specific wording precedes generic.
_EVENT_HINTS: list[tuple[EventKind, tuple[str, ...]]] = [
    (EventKind.CODE_FREEZE, ("код-фриз", "код фриз", "codefreeze", "code freeze", "фриз")),
    (
        EventKind.SUBMISSION,
        (
            "сдача", "сдать", "дедлайн реш", "submission", "submit", "upload",
            "отправка реш", "загрузка реш", "загрузить реш", "заливка реш",
            "приём работ", "прием работ",
        ),
    ),
    (EventKind.DEFENSE, ("защит", "питч", "pitch", "презентац", "демо", "demo day", "финал")),
    (EventKind.RESULTS, ("результат", "итог", "награжд", "объявлен", "results", "winners")),
    (EventKind.REGISTRATION, ("регистрац", "заявк", "registration", "reg deadline")),
    (EventKind.TECH_CHECK, ("тех-чек", "тех чек", "техчек", "tech check", "прогон", "репетиц")),
    (EventKind.MENTOR, ("ментор", "консультац", "mentor", "q&a", "вопрос-ответ")),
    (EventKind.CHECKPOINT, ("чек-поинт", "чекпоинт", "чек поинт", "checkpoint", "контрольн")),
    (EventKind.START, ("старт", "открыт", "kickoff", "kick-off", "начало", "opening")),
    (EventKind.AFTERPARTY, ("афтепати", "афтепаті", "afterparty", "вечеринк")),
]


class RsvpStatus(StrEnum):
    YES = "yes"
    LATE = "late"
    NO = "no"

    @property
    def label(self) -> str:
        return {"yes": "буду", "late": "опоздаю", "no": "не смогу"}[self.value]

    @property
    def emoji(self) -> str:
        return {"yes": "✅", "late": "⏰", "no": "❌"}[self.value]


class LinkKind(StrEnum):
    SITE = "site"
    RULES = "rules"
    CHAT = "chat"
    CHANNEL = "channel"
    STREAM = "stream"
    TABLE = "table"
    FORM = "form"
    REPO = "repo"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _LINK_LABELS[self][0]

    @property
    def emoji(self) -> str:
        return _LINK_LABELS[self][1]


_LINK_LABELS: dict[LinkKind, tuple[str, str]] = {
    LinkKind.SITE: ("сайт", "🌐"),
    LinkKind.RULES: ("правила / условия", "📜"),
    LinkKind.CHAT: ("чат", "💬"),
    LinkKind.CHANNEL: ("канал", "📣"),
    LinkKind.STREAM: ("трансляция", "📺"),
    LinkKind.TABLE: ("таблица", "📊"),
    LinkKind.FORM: ("форма сдачи", "📝"),
    LinkKind.REPO: ("репозиторий", "🐙"),
    LinkKind.OTHER: ("ссылка", "🔗"),
}
