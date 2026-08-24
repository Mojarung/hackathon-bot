"""Turn a poster, a PDF page or a wall of chat text into structured hackathon data."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from hackbot.agent.llm import LLMUnavailableError, chat_model, model_settings, vision_model
from hackbot.agent.schemas import ExtractedHackathon
from hackbot.domain.timeutils import WEEKDAYS_FULL, now_utc, to_local

log = logging.getLogger(__name__)

_INSTRUCTIONS = """\
Ты извлекаешь данные о хакатоне из текста и изображений (афиши, скриншоты условий, PDF).

Правила:
- Отвечай ТОЛЬКО тем, что реально есть в источнике. Ничего не выдумывай.
- Если данных нет - оставляй поле null, а не придумывай правдоподобное значение.
- Все даты в ISO-8601 БЕЗ смещения, в местном времени хакатона: 2026-09-22T18:00
- Если год не указан явно - выбирай ближайший будущий относительно текущей даты.
- В events клади ВСЕ временные точки: регистрация, старт, чек-поинты, код-фриз,
  сдача решения, защита, объявление результатов.

КРИТИЧНО для каждого элемента events:
- starts_at ОБЯЗАТЕЛЕН и всегда заполнен реальной датой и временем.
- kind ОБЯЗАТЕЛЕН и берётся строго из списка допустимых значений.
- Интервал - это ОДИН этап с starts_at и ends_at. Никогда не создавай отдельные
  этапы со словами «начало» и «окончание» в названии.
- ends_at заполняй ТОЛЬКО если в источнике явно названо время окончания
  («с 15:00 до 18:00», «20:00-23:00»). Иначе ends_at = null.
  НИКОГДА не копируй значение starts_at в ends_at.
- Дедлайн («до 18:00», «не позднее 18:00», «загрузка до 22.09 18:00») - это момент,
  а не интервал: starts_at = время дедлайна, ends_at = null.
- is_mandatory = true только для code_freeze, submission и defense. Для остальных false.
- Если время не указано, а только день - ставь T00:00 и задай вопрос в questions.

Примеры правильных элементов events:
{"title": "Защита проектов", "kind": "defense", "starts_at": "2026-09-22T20:00",
 "ends_at": "2026-09-22T23:00", "place": "Главный зал", "url": null, "is_mandatory": true}
{"title": "Сдача решения", "kind": "submission", "starts_at": "2026-09-22T18:00",
 "ends_at": null, "place": null, "url": null, "is_mandatory": true}
{"title": "Открытие и старт", "kind": "start", "starts_at": "2026-09-20T10:00",
 "ends_at": null, "place": "Академия Маяк", "url": null, "is_mandatory": false}

Остальное:
- links.kind тоже строго из списка: site, rules, chat, channel, stream, table, form, repo, other.
- starts_at и ends_at верхнего уровня - это начало и конец САМОГО хакатона.
- title - только имя хакатона, без города и без года: они идут в отдельные поля.
- Часовой пояс по умолчанию всегда московский. Заполняй timezone только если в
  источнике ЯВНО указан другой пояс. НИКОГДА не спрашивай про часовой пояс в
  questions - это заранее известно.
- В questions задай короткие уточняющие вопросы на русском про то, чего не хватает
  для полного тайминга. Максимум 4 вопроса. Если всё есть - пустой список.
- description: 1-3 предложения о сути хакатона, треках, призовом фонде.
"""


def _context_block(tz: ZoneInfo) -> str:
    local = to_local(now_utc(), tz)
    weekday = WEEKDAYS_FULL[local.weekday()]
    return (
        f"Сейчас: {local:%Y-%m-%d %H:%M} ({weekday}), часовой пояс {tz.key}. "
        f"Текущий год {local.year}."
    )


def _build_agent(*, with_vision: bool) -> Agent[None, ExtractedHackathon]:
    model = vision_model() if with_vision else chat_model()
    return Agent(
        model,
        output_type=ExtractedHackathon,
        instructions=_INSTRUCTIONS,
        model_settings=model_settings(max_tokens=4096),
        retries=2,
    )


async def extract_hackathon(
    text: str,
    images: list[bytes] | None = None,
    *,
    tz: ZoneInfo,
) -> ExtractedHackathon:
    """Extract whatever is knowable. Raises LLMUnavailableError when unconfigured."""
    images = images or []
    agent = _build_agent(with_vision=bool(images))

    prompt: list[object] = [_context_block(tz)]
    if text.strip():
        prompt.append(f"Текст из сообщения:\n{text.strip()}")
    for blob in images[:4]:  # more than a few images blows the token budget
        prompt.append(BinaryContent(data=blob, media_type="image/jpeg"))
    if not text.strip() and not images:
        raise ValueError("nothing to extract from")

    try:
        result = await agent.run(prompt)
    except UnexpectedModelBehavior as exc:
        log.warning("extraction failed: %s", exc)
        raise
    log.info("extraction used %s", result.usage)
    return result.output


async def extract_from_text(text: str, *, tz: ZoneInfo) -> ExtractedHackathon:
    return await extract_hackathon(text, [], tz=tz)


def summarize_extraction(data: ExtractedHackathon, tz: ZoneInfo) -> str:
    """Compact debug/preview string, useful in logs and tests."""
    parts = [f"title={data.title!r}"]
    if data.starts_at:
        parts.append(f"start={data.starts_at}")
    if data.ends_at:
        parts.append(f"end={data.ends_at}")
    parts.append(f"events={len(data.events)}")
    parts.append(f"links={len(data.links)}")
    if data.questions:
        parts.append(f"questions={len(data.questions)}")
    return " ".join(parts)


__all__ = [
    "ExtractedHackathon",
    "LLMUnavailableError",
    "extract_from_text",
    "extract_hackathon",
    "summarize_extraction",
]


