"""The joke generator: one piece of absurd deadpan life advice, as plain text.

Two things make or break this feature. First, the humour has to be deadpan
absurd rather than "haha random" - the joke is that the advice is delivered with
total sincerity, in the voice of a motivational card. Second, an LLM asked the
same question twice tells the same joke twice, so every call is seeded with
random anchors and told which motifs came up recently.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from hackbot.agent import prompts
from hackbot.agent.llm import LLMUnavailableError, fun_model, model_settings

log = logging.getLogger(__name__)

HEADERS = (
    "Мудрость дня",
    "Совет, который изменит твою жизнь",
    "Правило, которое я хотел бы знать в 20 лет",
    "Инсайт дня",
    "Практика на сегодня",
    "Привычка успешного человека",
    "Секрет продуктивности",
    "Шаг к внутренней гармонии",
)

# Random anchors. The model gets a handful each time, which pushes the joke into
# a different corner instead of orbiting the same three punchlines.
DOMAINS = (
    "быт и кухня", "общественный транспорт", "офисная работа", "домашние животные",
    "погода", "супермаркет", "ремонт", "спорт", "документы и бюрократия",
    "соседи", "гардероб", "техника в доме", "дача", "поликлиника", "лифт",
    "почта", "банки и кредиты", "отпуск", "школа", "стройка", "автосервис",
    "парикмахерская", "библиотека", "аэропорт", "гараж", "балкон", "подъезд",
)
DEVICES = (
    "обращаться к неодушевлённому предмету как к коллеге",
    "выполнять бытовое действие в неправильном порядке",
    "приписывать обычной вещи юридический статус",
    "вести формальную отчётность о бессмысленном",
    "заключать договорённости с явлением природы",
    "относиться к абстракции как к товару",
    "путать масштаб: планировать мелочь как государственный проект",
    "давать вещам должности и звания",
    "измерять неизмеримое в конкретных единицах",
    "просить разрешения там, где оно не требуется",
    "здороваться и прощаться с процессами",
    "готовиться к событию, которого не будет",
)

_FALLBACK: tuple[tuple[str, str], ...] = (
    ("Перед сном благодарить холодильник", "за проделанную за день работу"),
    ("Называть понедельник", "по имени и отчеству"),
    ("Заваривать чай, но", "не сообщать ему об этом заранее"),
    ("Раз в неделю проводить", "короткую планёрку с обувью"),
    ("Просить у лифта", "обратную связь по итогам поездки"),
    ("Оставлять зонту", "право на особое мнение"),
    ("Перед едой обязательно здороваться", "с вилкой"),
    ("Хранить в кармане камень", "на случай важных переговоров"),
    ("Каждое утро выбирать случайный предмет и", "считать его начальником"),
    ("Никогда не садиться на стул, пока не", "попросишь у него разрешения"),
)


class Advice(BaseModel):
    """One line of advice, split so the punchline can be emphasised."""

    text: str = Field(description="Завязка совета, звучит буднично и серьёзно")
    accent: str = Field(description="Панчлайн в конце - именно в нём живёт абсурд")

    def joined(self) -> str:
        return f"{self.text} {self.accent}".strip()


DEFAULT_WISDOM_PROMPT = """\
Ты пишешь ОДИН совет в жанре пародии на мотивационные карточки из соцсетей
(«7 решений, которые изменят твою жизнь»).

Суть шутки: совет звучит как настоящий лайфхак для саморазвития - тот же
уверенный тон, тот же командный инфинитив - но по содержанию это спокойный
абсурд. Никаких подмигиваний, никаких «лол», никаких смайликов и никаких
пояснений, что это шутка. Полная серьёзность подачи и есть юмор.

Требования:
- Ровно ОДИН совет. Не список.
- Начинай с глагола в инфинитиве: «Считать», «Обменять», «Никогда не садиться».
- Совет делится на две части: text - завязка, accent - панчлайн в конце.
  Абсурд живёт в accent, а завязка звучит буднично.
- ГЛАВНОЕ: text и accent, склеенные через пробел, обязаны быть ОДНОЙ грамотной
  русской фразой. Проверь падежи, род и число перед ответом. Фраза должна
  читаться гладко, как будто её написал живой человек, а не собрал из кусков.
- Максимум 12 слов на обе части вместе. Одна строка. Одна мысль, а не две склеенные.
- Абсурд бытовой и приземлённый: предметы, животные, продукты, документы, погода.
  НЕ уходи в фэнтези, космос, магию и мистику - это сразу убивает шутку.
- Без политики, без чернухи, без обидного про людей и группы людей.
- В тексте совета не должно быть цифр, кавычек-ёлочек и служебных пометок.
- Пиши по-русски.

Образцы правильного результата:
text="Перед едой обязательно здороваться" | accent="с вилкой"
text="Никогда не садиться на стул, пока не" | accent="попросишь у него разрешения"
text="Обменять все свои носки" | accent="на редких рыб"
text="Каждый вечер благодарить зеркало за то," | accent="что оно тебя отражает"
text="Водить картошку на прогулку" | accent="по квартире"
text="Открывать банку консервов низким голосом," | accent="чётко произнося «открываю»"
"""


@dataclass(slots=True)
class WisdomSeed:
    header: str
    domain: str
    devices: tuple[str, ...]
    salt: int

    def as_prompt(self, avoid: list[str] | None = None) -> str:
        # The salt deliberately never reaches the prompt: an earlier version passed
        # it through as a "variation key" and the model helpfully appended the
        # number to the joke itself. Variation comes from the sampled domain,
        # the devices and the temperature instead.
        lines = [
            f"Бытовая область для этого совета: {self.domain}",
            "",
            "Можешь опереться на один из приёмов абсурда:",
            "\n".join(f"- {d}" for d in self.devices),
        ]
        if avoid:
            lines += [
                "",
                "Эти советы уже были - не повторяй их и не бери синонимы:",
                "\n".join(f"- {a}" for a in avoid[:15]),
            ]
        lines += ["", "Придумай то, чего ещё не было. Верни один совет."]
        return "\n".join(lines)


def make_seed(rng: random.Random | None = None) -> WisdomSeed:
    rng = rng or random.Random()
    return WisdomSeed(
        header=rng.choice(HEADERS),
        domain=rng.choice(DOMAINS),
        devices=tuple(rng.sample(DEVICES, k=3)),
        salt=rng.randrange(1000, 9999),
    )


_TRAILING_NOISE = re.compile(r"[\s,;:-]*\b\d{2,}\b[\s.]*$")


def _clean(part: str) -> str:
    part = _TRAILING_NOISE.sub("", part.strip())
    return part.strip().strip(".;")


def _normalize(advice: Advice) -> Advice:
    text = _clean(advice.text).rstrip(",") if advice.accent else _clean(advice.text)
    accent = _clean(advice.accent)
    # Models sometimes repeat the whole sentence in both fields.
    if accent and accent.casefold() in text.casefold():
        accent = ""
    # A comma before the accent belongs to the sentence, so put it back.
    if accent and advice.text.rstrip().endswith(","):
        text += ","
    return Advice(text=text, accent=accent)


def fallback_advice() -> Advice:
    text, accent = random.choice(_FALLBACK)
    return Advice(text=text, accent=accent)


async def generate_advice(
    avoid: list[str] | None = None, *, seed: WisdomSeed | None = None
) -> tuple[Advice, WisdomSeed]:
    """Generate one advice. Never raises: a canned line beats a broken command."""
    seed = seed or make_seed()
    try:
        agent = Agent(
            fun_model(),
            output_type=Advice,
            instructions=prompts.load("wisdom", DEFAULT_WISDOM_PROMPT),
            # Warm, but not so warm that the two halves stop agreeing grammatically:
            # variety comes from the sampled anchors, not from raw randomness.
            model_settings=model_settings(max_tokens=1024, temperature=0.95),
            retries=2,
        )
        result = await agent.run(seed.as_prompt(avoid))
    except LLMUnavailableError:
        return fallback_advice(), seed
    except Exception as exc:
        log.warning("wisdom generation failed: %s", exc)
        return fallback_advice(), seed

    advice = _normalize(result.output)
    if len(advice.joined()) < 12:
        log.info("model returned a too-short advice %r, using fallback", advice.joined())
        return fallback_advice(), seed
    return advice, seed
