"""The application ID rule: valid, invalid and style-hint cases."""

import pytest

from tools.lib_app_id import MANIFEST_PATH, is_valid

VALID = [
    "app.busy.weather",
    "io.github.imalkav.chess_clock",
    "ru.malk-it.chess_clock",
    "app.vasya.useful-app",
    "a.b.c",
    "a_b.c-d.e_f",
    "a." + "b" * 59 + ".c",
]
INVALID = [
    "App.Busy.X",
    "io.GitHub.alice.clock",
    "imalkav.chess_clock",
    "app.busy",
    ".a.b.c",
    "a.b.c.",
    "a..b.c",
    "a.b c.d",
    "a.b.\u0441",
    "a/b.c.d",
    "a." + "b" * 60 + ".c",
    "",
]


@pytest.mark.parametrize("value", VALID)
def test_valid_ids(value: str) -> None:
    """Accept dotted IDs with at least three parts, up to 63 characters."""
    assert len(value) <= 63
    assert is_valid(value)
    assert MANIFEST_PATH.fullmatch(f"applications/{value}/manifest.yml")


@pytest.mark.parametrize("value", INVALID)
def test_invalid_ids(value: str) -> None:
    """Reject the wrong shapes, characters and lengths."""
    assert not is_valid(value)
    assert not MANIFEST_PATH.fullmatch(f"applications/{value}/manifest.yml")


def test_sixty_three_characters_is_the_limit() -> None:
    """Exactly 63 characters pass and 64 do not."""
    assert is_valid("a." + "b" * 59 + ".c") and len("a." + "b" * 59 + ".c") == 63
    assert not is_valid("a." + "b" * 60 + ".c")


def test_upper_case_reason_suggests_lower_case() -> None:
    """The detail for an upper-case ID gives the fixed ID."""
    from tools.lib_app_id import invalid_reason

    assert "app.busy.x" in invalid_reason("App.Busy.X")
