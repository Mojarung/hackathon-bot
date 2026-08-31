"""Chat lore: always-on core plus a searchable corpus.

The lore files are gitignored on purpose - they carry real names, birthdays and
health details, and the repository is public. So the first thing these tests pin
down is that a checkout without them still runs: every lookup has to answer
"no lore" rather than raise, or a fresh clone would take the agent down.

For the same reason the fixtures below are invented rather than lifted from the
real corpus - a test file is exactly as public as the repository it lives in.
"""

from __future__ import annotations

import pytest

from hackbot.agent import lore, prompts

CORPUS = """# Корпус

## Кто такой хостовой
Тимур полтора часа не мог понять, кто такой @hostovoy.
Финал: «он ушёл в закат ради команды».

## Леденцы
Неопытные. «нам нужны леденцы с горящими глазами».

## Мерч
Футболки с QR-кодом на спине.
"""

QUOTES = """# Цитаты

## Проверка на своего
> — За методы вжуха шаришь? Ну или хотя бы за конверты?
> — эх. ну надо подучить матчасть
"""


@pytest.fixture
def lore_dir(tmp_path, monkeypatch):
    """Point the prompt loader at a throwaway directory.

    The cache is keyed by name and validated by mtime, so it has to be cleared
    too: a real prompts/ read earlier in the session would otherwise leak in.
    """
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    prompts._cache.clear()
    yield tmp_path
    prompts._cache.clear()


def write(dirpath, name: str, text: str) -> None:
    (dirpath / f"{name}.md").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- absent lore


def test_a_checkout_without_lore_files_still_answers(lore_dir) -> None:
    assert lore.compact() == ""
    assert lore.installed() is False
    assert lore.search("хостовой") == ""


def test_the_agent_adds_no_lore_block_when_there_is_no_lore(lore_dir) -> None:
    """An empty instruction is what keeps a public checkout from talking nonsense."""
    from hackbot.agent.react import _lore

    assert _lore(None) == ""


# ---------------------------------------------------------------- with lore


def test_the_core_rides_along_verbatim(lore_dir) -> None:
    write(lore_dir, "lore", "# Ядро\nКоманда ходит по хакатонам с позапрошлого года.")
    assert "ходит по хакатонам" in lore.compact()
    assert lore.installed() is True


def test_search_returns_the_whole_section_not_a_line(lore_dir) -> None:
    """A quote torn out of its exchange loses the rhythm worth copying."""
    write(lore_dir, "lore_corpus", CORPUS)

    found = lore.search("хостовой")
    assert found.startswith("## Кто такой хостовой")
    assert "ушёл в закат" in found          # хвост раздела, а не только строка с совпадением
    assert "Леденцы" not in found


def test_search_looks_in_the_quote_bank_too(lore_dir) -> None:
    write(lore_dir, "lore_corpus", CORPUS)
    write(lore_dir, "lore_quotes", QUOTES)

    found = lore.search("вжуха конверты")
    assert "Проверка на своего" in found


def test_a_section_matching_more_of_the_question_wins(lore_dir) -> None:
    write(lore_dir, "lore_corpus", CORPUS)

    found = lore.search("леденцы горящими глазами")
    assert found.splitlines()[0] == "## Леденцы"


def test_nothing_relevant_says_so_instead_of_dumping_the_corpus(lore_dir) -> None:
    write(lore_dir, "lore_corpus", CORPUS)

    assert lore.search("квантовая хромодинамика") == ""
    assert lore.search("") == ""
    assert lore.search("а") == "", "слишком короткое слово не должно тянуть весь корпус"


def test_the_answer_stays_within_budget(lore_dir) -> None:
    """The result goes into a prompt, so an unbounded corpus cannot come back."""
    sections = "".join(
        "\n## Раздел {}\nхостовой {}\n".format(i, "текст " * 200) for i in range(10)
    )
    write(lore_dir, "lore_corpus", "# Корпус\n" + sections)

    found = lore.search("хостовой")
    assert 0 < len(found) <= lore.MAX_CHARS
    assert found.count("## Раздел") <= lore.MAX_HITS
