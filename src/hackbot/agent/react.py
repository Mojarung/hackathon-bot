"""The conversational agent.

Its tools are thin wrappers over the same services the slash commands use, so a
feature written once is immediately available three ways: command, button and
plain Russian. The agent is told to ask rather than guess, because a hallucinated
deadline is worse than a follow-up question.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from hackbot.agent import prompts
from hackbot.agent.defaults import DEFAULT_PERSONA
from hackbot.agent.llm import chat_model, model_settings, vision_model
from hackbot.config import get_settings
from hackbot.db.base import session_scope
from hackbot.db.models import Hackathon
from hackbot.domain.enums import EventKind, HackStatus, LinkKind
from hackbot.domain.services.events import (
    add_event,
    delete_event,
    get_event,
    list_events,
    propose_timeline,
    update_event,
)
from hackbot.domain.services.github import GitHubError, attach_repo, create_repo
from hackbot.domain.services.hackathons import (
    EDITABLE_FIELDS,
    create,
    hack_tz,
    missing_fields,
    primary_deadline,
    set_link,
    update_fields,
)
from hackbot.domain.services.participants import (
    add_by_name,
    find_by_name,
    list_participants,
    remove,
    rsvp_summary,
)
from hackbot.domain.timeutils import (
    WEEKDAYS_FULL,
    fmt_dt,
    humanize_delta,
    now_utc,
    parse_dt,
    parse_iso,
    to_local,
)

log = logging.getLogger(__name__)



@dataclass(slots=True)
class AgentDeps:
    chat_id: int
    thread_id: int | None
    hack_id: int | None
    user_id: int
    actor: str
    tz: ZoneInfo
    # Things the agent cannot do itself because they need the Bot instance:
    # sending a rendered timeline, attaching an .ics file, tagging the team.
    # Tools append a marker here and the handler performs it after the run.
    outbox: list[str] = field(default_factory=list)


# The model is resolved per run, not here: building it at import time would
# raise on a deployment that has no LLM key and take the whole bot down with it.
agent: Agent[AgentDeps, str] = Agent(
    deps_type=AgentDeps,
    output_type=str,
    retries=2,
)


@agent.instructions
def _persona(ctx: RunContext[AgentDeps]) -> str:
    """Read from prompts/persona.md on every run, so edits apply immediately."""
    return prompts.load("persona", DEFAULT_PERSONA)


@agent.instructions
def _time_context(ctx: RunContext[AgentDeps]) -> str:
    local = to_local(now_utc(), ctx.deps.tz)
    return (
        f"Сейчас {local:%Y-%m-%d %H:%M} ({WEEKDAYS_FULL[local.weekday()]}), "
        f"часовой пояс {ctx.deps.tz.key}. Обращается: {ctx.deps.actor or 'участник'}."
    )


async def _load(deps: AgentDeps, session) -> Hackathon | None:
    if deps.hack_id is None:
        return None
    return await session.get(Hackathon, deps.hack_id)


def _resolve_moment(raw: str, tz: ZoneInfo) -> datetime | None:
    """Accept both strict ISO and the loose phrasings a model might echo back."""
    moment = parse_iso(raw, tz)
    if moment is not None:
        return moment
    parsed = parse_dt(raw, tz)
    return parsed.dt if parsed else None


@agent.tool
async def get_state(ctx: RunContext[AgentDeps]) -> str:
    """Текущее состояние хакатона: поля, все этапы с id, ссылки, команда."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "В этой теме хакатон ещё не заведён. Предложи создать через /new Название."

        tz = hack_tz(hack)
        events = await list_events(session, hack.id)
        people = await list_participants(session, hack.id)

        rows = [
            f"Хакатон: {hack.title} ({hack.year}), статус {hack.status.label}",
            f"Город: {hack.city or ('онлайн' if hack.is_online else 'не задан')}",
            f"Начало: {fmt_dt(hack.starts_at, tz) if hack.starts_at else 'не задано'}",
            f"Конец: {fmt_dt(hack.ends_at, tz) if hack.ends_at else 'не задан'}",
            f"Регистрация до: "
            f"{fmt_dt(hack.reg_deadline, tz) if hack.reg_deadline else 'не задана'}",
        ]
        if hack.github_url:
            rows.append(f"Репозиторий: {hack.github_url}")

        rows.append("")
        rows.append("Этапы:" if events else "Этапов нет.")
        for event in events:
            line = (
                f"  id={event.id} [{event.kind.value}] {event.title}"
                f" — {fmt_dt(event.starts_at, tz)}"
            )
            if event.ends_at:
                line += f" до {fmt_dt(event.ends_at, tz)}"
            if event.place:
                line += f", место: {event.place}"
            if event.url:
                line += f", ссылка: {event.url}"
            rows.append(line)

        if hack.links:
            rows.append("")
            rows.append("Ссылки:")
            rows += [f"  {link.kind.value}: {link.url}" for link in hack.links]

        rows.append("")
        rows.append(f"В команде {len(people)}: " + ", ".join(p.display for p in people))

        target = primary_deadline(hack, events)
        if target and target.starts_at > now_utc():
            left = humanize_delta((target.starts_at - now_utc()).total_seconds(), parts=3)
            rows.append(f"До «{target.title}» осталось {left}.")

        gaps = missing_fields(hack, events)
        if gaps:
            rows.append("Не хватает: " + ", ".join(gaps))
        return "\n".join(rows)


@agent.tool
async def set_field(ctx: RunContext[AgentDeps], field: str, value: str) -> str:
    """Изменить поле хакатона.

    field: title, year, organizer, city, is_online, description, starts_at,
    ends_at, reg_deadline. Даты — ISO местного времени.
    """
    if field not in EDITABLE_FIELDS:
        return f"Поле {field} менять нельзя. Доступны: {', '.join(sorted(EDITABLE_FIELDS))}"

    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        tz = hack_tz(hack)

        parsed: object = value
        if field in {"starts_at", "ends_at", "reg_deadline"}:
            moment = _resolve_moment(value, tz)
            if moment is None:
                return f"Не разобрал дату {value!r}. Нужен формат 2026-09-22T18:00."
            parsed = moment
        elif field == "year":
            if not value.isdigit():
                return "Год должен быть числом."
            parsed = int(value)
        elif field == "is_online":
            parsed = value.casefold() in {"true", "да", "1", "онлайн", "yes"}

        changed = await update_fields(session, hack, {field: parsed})
        if not changed:
            return "Значение и так было таким, ничего не менял."

        if field in {"starts_at", "reg_deadline"}:
            from hackbot.domain.services.events import ensure_deadline_events

            await ensure_deadline_events(session, hack)
        return f"Поле {field} обновлено."


@agent.tool
async def add_timeline_event(
    ctx: RunContext[AgentDeps],
    title: str,
    starts_at: str,
    kind: str = "other",
    ends_at: str | None = None,
    place: str | None = None,
    url: str | None = None,
) -> str:
    """Добавить этап в таймлайн. starts_at обязателен, ISO местного времени."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        tz = hack_tz(hack)

        moment = _resolve_moment(starts_at, tz)
        if moment is None:
            return f"Не разобрал дату {starts_at!r}. Уточни у пользователя точное время."

        try:
            event_kind = EventKind(kind)
        except ValueError:
            event_kind = EventKind.guess(title)

        event = await add_event(
            session, hack, title=title, starts_at=moment, kind=event_kind,
            ends_at=_resolve_moment(ends_at, tz) if ends_at else None,
            place=place, url=url,
        )
        return f"Добавил этап id={event.id}: {event.title} на {fmt_dt(event.starts_at, tz)}."


@agent.tool
async def move_timeline_event(ctx: RunContext[AgentDeps], event_id: int, starts_at: str) -> str:
    """Перенести этап на другое время. event_id берётся из get_state."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        event = await get_event(session, event_id)
        if event is None or event.hackathon_id != hack.id:
            return f"Этапа id={event_id} нет. Загляни в get_state."

        tz = hack_tz(hack)
        moment = _resolve_moment(starts_at, tz)
        if moment is None:
            return f"Не разобрал дату {starts_at!r}."
        before = fmt_dt(event.starts_at, tz)
        await update_event(session, event, {"starts_at": moment})
        return f"Перенёс «{event.title}»: {before} → {fmt_dt(moment, tz)}."


@agent.tool
async def delete_timeline_event(ctx: RunContext[AgentDeps], event_id: int) -> str:
    """Удалить этап. Используй только если пользователь явно просит удалить."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        event = await get_event(session, event_id)
        if event is None or event.hackathon_id != hack.id:
            return f"Этапа id={event_id} нет."
        title = event.title
        await delete_event(session, event)
        return f"Удалил этап «{title}»."


@agent.tool
async def add_link(ctx: RunContext[AgentDeps], kind: str, url: str, title: str | None = None
                   ) -> str:
    """Добавить ссылку. kind: site, rules, chat, channel, stream, table, form, repo, other."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        try:
            link_kind = LinkKind(kind)
        except ValueError:
            link_kind = LinkKind.OTHER
        await set_link(session, hack, link_kind, url, title)
        return f"Ссылка {link_kind.label} сохранена."


@agent.tool
async def who_is_going(ctx: RunContext[AgentDeps], event_id: int) -> str:
    """Кто подтвердил участие в этапе, а кто ещё не ответил."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        event = await get_event(session, event_id)
        if event is None or event.hackathon_id != hack.id:
            return f"Этапа id={event_id} нет."
        summary = await rsvp_summary(session, event)
        parts = [
            f"{status.label}: {', '.join(p.display for p in people)}"
            for status, people in summary.by_status.items()
        ]
        if summary.pending:
            parts.append("не ответили: " + ", ".join(p.display for p in summary.pending))
        return f"«{event.title}» — " + ("; ".join(parts) if parts else "никто ещё не ответил")


@agent.tool
async def create_hackathon(ctx: RunContext[AgentDeps], title: str) -> str:
    """Завести хакатон в этой теме. Используй, когда get_state говорит, что его нет."""
    if ctx.deps.hack_id is not None:
        return "В этой теме уже есть хакатон. Менять данные — через set_field."
    async with session_scope() as session:
        hack = await create(
            session,
            title=title.strip()[:200],
            chat_id=ctx.deps.chat_id,
            thread_id=ctx.deps.thread_id,
            created_by=ctx.deps.user_id,
        )
        ctx.deps.hack_id = hack.id
        ctx.deps.outbox.append("card")
        return (
            f"Завёл хакатон «{hack.title}» (id={hack.id}). "
            "Теперь можно задавать даты через set_field."
        )


@agent.tool
async def apply_standard_timeline(ctx: RunContext[AgentDeps]) -> str:
    """Добавить стандартный набор этапов. Работает только когда заданы начало и конец."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        if not hack.starts_at or not hack.ends_at:
            return "Сначала нужны даты начала и конца хакатона."

        existing = await list_events(session, hack.id)
        proposals = propose_timeline(hack, existing)
        if not proposals:
            return "Все стандартные этапы уже есть."
        for item in proposals:
            await add_event(
                session, hack, title=item.title, starts_at=item.starts_at,
                kind=item.kind, ends_at=item.ends_at,
            )
        ctx.deps.outbox.append("card")
        return "Добавил этапы: " + ", ".join(p.title for p in proposals)


@agent.tool
async def set_result(ctx: RunContext[AgentDeps], place: str, note: str | None = None) -> str:
    """Записать итог хакатона: занятое место и, если есть, короткий комментарий."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        await update_fields(
            session, hack,
            {"result_place": place[:80], "result_note": note, "status": HackStatus.FINISHED},
        )
        ctx.deps.outbox.append("card")
        return f"Записал результат: {place}."


@agent.tool
async def show_timeline(ctx: RunContext[AgentDeps]) -> str:
    """Показать пользователю красиво оформленный таймлайн отдельным сообщением."""
    if ctx.deps.hack_id is None:
        return "Хакатон в этой теме не заведён."
    ctx.deps.outbox.append("timeline")
    return "Таймлайн отправлен отдельным сообщением, пересказывать его не нужно."


@agent.tool
async def send_calendar_file(ctx: RunContext[AgentDeps]) -> str:
    """Прислать файл .ics со всеми этапами для импорта в календарь."""
    if ctx.deps.hack_id is None:
        return "Хакатон в этой теме не заведён."
    ctx.deps.outbox.append("ics")
    return "Файл календаря отправлен отдельным сообщением."


@agent.tool
async def ping_team(ctx: RunContext[AgentDeps], note: str | None = None) -> str:
    """Тегнуть всех участников поимённо. Только по явной просьбе — это шумно."""
    if ctx.deps.hack_id is None:
        return "Хакатон в этой теме не заведён."
    async with session_scope() as session:
        people = await list_participants(session, ctx.deps.hack_id)
    if not people:
        return "Команда пустая, некого пинговать. Пусть участники напишут /join."
    ctx.deps.outbox.append(f"ping:{note or ''}")
    return f"Пингую {len(people)} человек отдельным сообщением."


@agent.tool
async def github_repo(ctx: RunContext[AgentDeps], action: str, reference: str | None = None
                      ) -> str:
    """Работа с репозиторием. action: new (создать), attach (прикрепить существующий,
    нужен reference вида owner/name или ссылка), push (залить документы и README),
    show (показать ссылку)."""
    settings = get_settings()
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        title, year = hack.title, hack.year
        current, current_url = hack.github_repo, hack.github_url
        description = hack.description

    if action == "show" or not action:
        return f"Репозиторий: {current_url}" if current_url else "Репозиторий не привязан."
    if not settings.github_enabled:
        return "GITHUB_TOKEN не настроен, интеграция выключена."

    try:
        if action == "new":
            repo = await create_repo(title, year, description=description)
        elif action == "attach":
            if not reference:
                return "Нужна ссылка или owner/name репозитория."
            repo = await attach_repo(reference)
        elif action == "push":
            if not current:
                return "Сначала создай или прикрепи репозиторий."
            repo = await attach_repo(current)
        else:
            return "Неизвестное действие. Доступны: new, attach, push, show."
    except GitHubError as exc:
        return f"GitHub отказал: {exc.message}"

    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is not None:
            hack.github_repo = repo.full_name
            hack.github_url = repo.html_url
    ctx.deps.outbox.append("card")
    if action == "push":
        ctx.deps.outbox.append("push_docs")
        return f"Заливаю документы в {repo.full_name}."
    return f"Репозиторий {repo.full_name}: {repo.html_url}"


@agent.tool
async def share_wisdom(ctx: RunContext[AgentDeps], ask_someone: bool = False) -> str:
    """Прислать шуточный совет дня («дай совет», «расскажи имбу», «мудрость дня»).

    ask_someone=True, если пользователь просит спросить совет у кого-то из команды.
    НИКОГДА не придумывай совет сам — его пишет отдельная модель в нужном стиле.
    """
    ctx.deps.outbox.append("wisdom:team" if ask_someone else "wisdom:")
    return "Совет отправлен отдельным сообщением. Не пересказывай и не придумывай свой."


@agent.tool
async def list_team(ctx: RunContext[AgentDeps]) -> str:
    """Состав команды с ролями."""
    if ctx.deps.hack_id is None:
        return "Хакатон в этой теме не заведён."
    async with session_scope() as session:
        people = await list_participants(session, ctx.deps.hack_id)
    if not people:
        return "В команде пока никого."
    return "; ".join(
        f"{p.display}{' (капитан)' if p.is_captain else ''}"
        f"{f' — {p.role}' if p.role else ''}"
        for p in people
    )


@agent.tool
async def add_person(ctx: RunContext[AgentDeps], name: str, role: str | None = None) -> str:
    """Добавить человека в команду по имени или @нику, сразу с ролью, если названа.

    Работает и для тех, кто ещё ни разу не писал боту.
    """
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        person, created = await add_by_name(session, hack, name, role)
        ctx.deps.outbox.append("card")
        what = "Добавил" if created else "Обновил"
        suffix = f", роль: {person.role}" if person.role else ""
        return f"{what} {person.display} в команду{suffix}."


@agent.tool
async def set_person_role(ctx: RunContext[AgentDeps], name: str, role: str) -> str:
    """Назначить или сменить роль участника. name — имя или @ник из list_team."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        person = await find_by_name(session, hack.id, name)
        if person is None:
            # Nobody by that name yet, so create them with the role in one step.
            person, _ = await add_by_name(session, hack, name, role)
            ctx.deps.outbox.append("card")
            return f"Такого в команде не было — добавил {person.display} с ролью {role}."
        old = person.role
        person.role = role
        await session.flush()
        ctx.deps.outbox.append("card")
        return (
            f"Роль {person.display}: {old} → {role}." if old
            else f"Роль {person.display}: {role}."
        )


@agent.tool
async def remove_person(ctx: RunContext[AgentDeps], name: str) -> str:
    """Убрать человека из команды. Только по явной просьбе."""
    async with session_scope() as session:
        hack = await _load(ctx.deps, session)
        if hack is None:
            return "Хакатон в этой теме не заведён."
        person = await remove(session, hack, name)
        if person is None:
            return f"В команде нет никого похожего на {name!r}."
        ctx.deps.outbox.append("card")
        return f"Убрал {person.display} из команды."


# ---------------------------------------------------------------- runner


async def run_agent(
    deps: AgentDeps,
    prompt: str,
    images: list[bytes] | None = None,
    history: list[ModelMessage] | None = None,
) -> tuple[str, bytes]:
    """Run one turn. Returns the reply and the serialised history to persist."""
    parts: list[object] = [prompt]
    for blob in (images or [])[:3]:
        parts.append(BinaryContent(data=blob, media_type="image/jpeg"))

    # gpt-oss rejects image input outright (400 "this model does not support
    # image input"), so anything with a picture has to go to the vision model -
    # which also does tool calling, so the agent keeps all of its abilities.
    model = vision_model() if parts[1:] else chat_model()

    result = await agent.run(
        parts,
        deps=deps,
        message_history=history,
        model=model,
        model_settings=model_settings(max_tokens=3072, temperature=0.2),
    )
    log.info("agent run: %s", result.usage)
    return result.output, result.all_messages_json()


def load_history(raw: str | None) -> list[ModelMessage]:
    if not raw:
        return []
    try:
        return ModelMessagesTypeAdapter.validate_json(raw)
    except Exception as exc:
        log.info("dropping unreadable agent history: %s", exc)
        return []
