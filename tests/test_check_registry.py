"""The registry is the only place with author-facing check texts."""

import re
from pathlib import Path

import pytest

from tools.check_registry import (
    REGISTRY,
    STAGES,
    CheckError,
    icon,
    outcome,
)

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def test_every_entry_is_complete() -> None:
    """Ids are unique and every check has a stage, a title, a problem and a fix."""
    assert len(REGISTRY) == len({check.id for check in REGISTRY.values()})
    for check_id, check in REGISTRY.items():
        assert check.id == check_id and re.fullmatch(r"[a-z_]+", check_id)
        assert check.stage in STAGES
        assert check.title.strip() and check.problem.strip() and check.fix.strip()
        assert isinstance(check.required, bool)


@pytest.mark.parametrize(
    ("check_id", "result", "expected"),
    [
        ("source_commit", "pass", "✅"),
        ("source_commit", "fail", "❌"),
        ("source_commit", "not_run", "❌"),
        ("category_alias", "pass", "✅"),
        ("category_alias", "fail", "⚠️"),
        ("category_alias", "not_run", "⚠️"),
    ],
)
def test_icon_table(check_id: str, result: str, expected: str) -> None:
    """Required: ❌ on fail and not_run; optional: ⚠️; pass is always ✅."""
    assert icon(check_id, result) == expected


def test_outcome_rejects_unknown_ids_and_bounds_detail() -> None:
    """Typos fail early, and untrusted detail stays bounded."""
    with pytest.raises(ValueError):
        outcome("no_such_check")
    assert len(outcome("build", "fail", "x" * 5000)["detail"]) == 2000
    assert str(CheckError("build", "boom")) == "build: boom"


def test_every_used_id_is_registered_and_none_is_unused() -> None:
    """Ids used in tools/ exist in the registry, and each entry is used somewhere."""
    text = {
        path.name: path.read_text(encoding="utf-8")
        for path in TOOLS.glob("*.py")
        if path.name != "check_registry.py"
    }
    used = set()
    pattern = re.compile(r'(?:CheckError|outcome|fail)\(\s*"([a-z_]+)"', re.MULTILINE)
    # an image rule is a tuple `((width, height), "check_id", max_bytes)`
    png_rules = re.compile(r'\(\([0-9, ]+\), "([a-z_]+)", ')
    for content in text.values():
        used.update(pattern.findall(content))
        used.update(png_rules.findall(content))
    assert used <= set(REGISTRY), used - set(REGISTRY)
    assert set(REGISTRY) - used == set(), set(REGISTRY) - used


def test_checks_are_unique_and_in_known_stages() -> None:
    """A repeated id would silently replace an earlier check in the registry."""
    from tools.check_registry import CHECKS, REGISTRY, STAGES

    assert len(REGISTRY) == len(CHECKS)
    assert {check.stage for check in CHECKS} <= set(STAGES)
