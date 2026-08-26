"""`.env.example` is copied verbatim on a fresh deploy, so it has to parse cleanly.

python-dotenv - the loader pydantic-settings uses - strips a trailing `# ...`
from a value that has something in front of it, but keeps the whole comment as
the value when the value is empty. A setting documented as "leave blank to switch
this off" then arrives switched on, holding a comment: a blank `CHAT_API_KEY`
becomes a live garbage API key, every agent call 401s, and the failure looks like
a broken provider rather than a broken example file.

The rule is therefore mechanical - no comment may sit on the same line as an
empty value - and this test enforces it on the file the README tells people to
copy.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_no_blank_setting_swallows_its_own_comment() -> None:
    values = dotenv_values(EXAMPLE, encoding="utf-8")
    assert values, "пример конфига не разобрался вообще"

    poisoned = {key: value for key, value in values.items() if (value or "").startswith("#")}

    assert poisoned == {}, "комментарий у пустого значения становится значением"


def test_the_trap_is_real_and_not_a_superstition(tmp_path: Path) -> None:
    """Pins the dotenv behaviour the test above exists for.

    Without this the rule reads like cargo cult, and the next person moves a
    comment back onto the line "because it obviously works for the others".
    """
    env = tmp_path / "probe.env"
    env.write_text("FILLED=30       # a comment\nEMPTY=          # a comment\n", encoding="utf-8")

    values = dotenv_values(env, encoding="utf-8")

    assert values["FILLED"] == "30"
    assert values["EMPTY"] == "# a comment"
