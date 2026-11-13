"""Tests for offline catalog manifest validation."""

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import yaml

from tools.check_manifest import validate, validate_scope
from tools.check_registry import blocks


def test_manifest_validation(tmp_path: Path) -> None:
    """
    Accept a valid manifest and reject unsafe paths and unknown categories.

    No application source code is accessed or executed.
    """
    path = tmp_path / "app.busy.clock" / "manifest.yml"
    path.parent.mkdir()
    data = {
        "sourcecode": {
            "type": "git",
            "location": {
                "origin": "https://github.com/busy-app/clock.git",
                "commit_sha": "a" * 40,
                "subdir": ".",
            },
        },
        "category": "clocks",
        "short_description": "A small clock for the bar",
        "description": "@README.md",
        "changelog": "Initial release",
        "screenshots": ["screenshots/1.png"],
    }
    path.write_text(yaml.safe_dump(data))
    assert validate(path) == []
    data["category"] = "unknown"
    data["description"] = "@../secret"
    path.write_text(yaml.safe_dump(data))
    errors = validate(path)
    assert any(item["detail"].startswith("category") for item in errors)
    assert any(item["detail"].startswith("description") for item in errors)


def test_pr_scope(tmp_path: Path) -> None:
    """
    Accept one changed manifest and reject other or deleted files.

    Git output is supplied by a fake executor without network access.
    """
    manifest = tmp_path / "applications" / "app.busy.clock" / "manifest.yml"
    with patch(
        "tools.check_manifest.subprocess.run",
        return_value=CompletedProcess(
            [], 0, stdout=b"A\0applications/app.busy.clock/manifest.yml\0"
        ),
    ):
        assert validate_scope(tmp_path, "origin/main", manifest) == []
    with patch(
        "tools.check_manifest.subprocess.run",
        return_value=CompletedProcess(
            [], 0, stdout=b"D\0applications/app.busy.clock/manifest.yml\0"
        ),
    ):
        assert validate_scope(tmp_path, "origin/main", manifest)


def test_eleven_screenshots_block_and_a_missing_section_does_too(
    tmp_path: Path,
) -> None:
    """Screenshots stay required: 0 and 11 entries are required-check failures."""
    path = tmp_path / "app.busy.clock" / "manifest.yml"
    path.parent.mkdir()
    data = yaml.safe_load(
        (
            Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
        ).read_text()
    )
    for value in (["a.png"] * 11, None):
        data["screenshots"] = value
        if value is None:
            del data["screenshots"]
        path.write_text(yaml.safe_dump(data))
        findings = validate(path)
        assert any(item["id"] == "manifest_schema" for item in findings)
        assert any(blocks(item["id"], item["result"]) for item in findings)


def test_application_id_rule_and_style_hint(tmp_path: Path) -> None:
    """
    Reject a directory name that breaks the ID rule and hint at the style.

    The hint is a warning: it must not make the manifest invalid.
    """
    from tools.check_registry import blocks, get

    source = (
        Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
    ).read_text()

    def ids(name: str) -> list[dict[str, str]]:
        path = tmp_path / name / "manifest.yml"
        path.parent.mkdir()
        path.write_text(source)
        return validate(path)

    assert [item["id"] for item in ids("io.github.imalkav.chess_clock")] == []
    wrong = ids("imalkav.chess_clock")
    assert [item["id"] for item in wrong] == ["manifest_path"]
    assert "io.github.<your-github-user>.<app>" in get("manifest_path").fix
    assert [item["id"] for item in ids("App.Busy.X")] == ["manifest_path"]
    assert [item["id"] for item in ids("ru.malk-it.chess_clock")] == []
    # Any valid ID passes: the first part is not matched against a list of domains.
    assert [item["id"] for item in ids("info.truetug.knock")] == []
    assert [item["id"] for item in ids("weather.example.app")] == []


def test_changelog_must_be_inline_text_of_10_to_4000(tmp_path: Path) -> None:
    """A file reference or a longer text fails with `changelog_text`; plain text passes."""
    from tools.check_manifest import validate

    def findings(changelog: str) -> list[dict]:
        folder = tmp_path / "app.busy.clock"
        folder.mkdir(exist_ok=True)
        (folder / "manifest.yml").write_text(
            yaml.safe_dump(
                {
                    "sourcecode": {
                        "type": "git",
                        "location": {
                            "origin": "https://github.com/o/r",
                            "commit_sha": "a" * 40,
                        },
                    },
                    "category": "productivity",
                    "short_description": "A small clock for the bar",
                    "description": "A small clock for the BUSY Bar that shows the time and the date.",
                    "changelog": changelog,
                    "screenshots": ["a.png"],
                }
            )
        )
        return [
            item
            for item in validate(folder / "manifest.yml")
            if item["id"] == "changelog_text"
        ]

    assert findings("Initial version") == []
    assert findings("x" * 4000) == []
    assert findings("x" * 10) == []
    link = findings("@CHANGELOG.md")
    assert link and "not a file reference" in link[0]["detail"]
    long = findings("x" * 4001)
    assert long and "4001 characters, expected 10–4000" in long[0]["detail"]
    short = findings("x" * 9)
    assert short and "9 characters" in short[0]["detail"]


def test_inline_description_and_name_limits(tmp_path: Path) -> None:
    """Inline description 50–4000 and the card-design name rule."""
    from tools.check_package import name_problem

    assert name_problem("Chess Clock") == ""
    assert name_problem("Tom's (v2)!") == ""
    assert "33" in name_problem("x" * 33)
    for bad in ("Двойной", "A  B", " A", "A ", "😀", ""):
        assert name_problem(bad), bad


def test_inline_description_must_have_50_to_4000_characters(tmp_path: Path) -> None:
    """A short inline description is a `manifest_schema` finding with the real length."""
    from tools.check_manifest import validate

    folder = tmp_path / "app.busy.clock"
    folder.mkdir()

    def details(description: str) -> list[str]:
        (folder / "manifest.yml").write_text(
            yaml.safe_dump(
                {
                    "sourcecode": {
                        "type": "git",
                        "location": {
                            "origin": "https://github.com/o/r",
                            "commit_sha": "a" * 40,
                        },
                    },
                    "category": "productivity",
                    "short_description": "A small clock for the bar",
                    "description": description,
                    "changelog": "Initial release",
                    "screenshots": ["a.png"],
                }
            )
        )
        return [
            item["detail"]
            for item in validate(folder / "manifest.yml")
            if item["id"] == "manifest_schema"
        ]

    assert details("x" * 50) == [] and details("x" * 4000) == []
    assert details("@README.md") == []
    assert any("5 characters" in text for text in details("short"))
    assert any("4001" in text for text in details("x" * 4001))


def test_official_namespace_is_a_warning_for_foreign_repositories(
    tmp_path: Path,
) -> None:
    """`app.busy.*` from another owner gets the optional warning; from busy-app it does not."""
    from tools.check_manifest import validate

    def findings(owner: str, folder: str) -> list[dict]:
        directory = tmp_path / folder
        directory.mkdir(exist_ok=True)
        (directory / "manifest.yml").write_text(
            yaml.safe_dump(
                {
                    "sourcecode": {
                        "type": "git",
                        "location": {
                            "origin": f"https://github.com/{owner}/r",
                            "commit_sha": "a" * 40,
                        },
                    },
                    "category": "productivity",
                    "short_description": "Clock",
                    "description": "Clock",
                    "changelog": "Initial",
                    "screenshots": ["a.png"],
                }
            )
        )
        return [
            item
            for item in validate(directory / "manifest.yml")
            if item["id"] == "official_namespace"
        ]

    assert findings("busy-app", "app.busy.clock") == []
    assert findings("Busy-App", "app.busy.watch") == []
    warning = findings("someone", "app.busy.fake")
    assert warning and "someone" in warning[0]["detail"]
    assert not blocks("official_namespace", "fail")
    assert findings("someone", "io.github.someone.clock") == []
