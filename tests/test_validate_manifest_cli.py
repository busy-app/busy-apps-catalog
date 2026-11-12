"""CLI exit status, category fallback and source-line annotation regressions."""

import json
from pathlib import Path

import pytest
import yaml

from tools.validate_manifest import main


@pytest.mark.parametrize(
    ("category", "expected", "marker"),
    [
        ("clocks", 0, "Passed with warnings"),
        ("unknown", 1, "::error"),
        ("meetings", 0, "alias"),
    ],
)
def test_cli_categories_and_lines(
    tmp_path, monkeypatch, capsys, mock_http, category, expected, marker
):
    """
    Annotate the real category line and keep offline/category alias warnings nonfatal.

    External category requests use a fake transport with a deterministic outage.
    """
    data = json.loads(
        (
            Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
        ).read_text()
    )
    data["category"] = category
    path = tmp_path / "app.busy.clock/manifest.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    monkeypatch.setattr("sys.argv", ["validate", str(path)])
    import httpx2

    def offline(request):
        raise httpx2.ConnectError("offline", request=request)

    mock_http(offline)
    assert main() == expected
    output = capsys.readouterr().out
    assert marker in output and "::warning" in output
    if expected:
        line = next(
            index
            for index, text in enumerate(path.read_text().splitlines(), 1)
            if text.startswith("category:")
        )
        assert f",line={line}," in output


@pytest.mark.parametrize("name", ["a" * 33, ".clock", "clock app"])
def test_invalid_folder_name(tmp_path, monkeypatch, capsys, name):
    """
    Reject invalid folder IDs through the public CLI and return a nonzero status.

    The source manifest is otherwise the shipped valid example.
    """
    path = tmp_path / name / "manifest.yml"
    path.parent.mkdir()
    path.write_text(
        (
            Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
        ).read_text()
    )
    monkeypatch.setattr("sys.argv", ["validate", str(path), "--categories-url", ""])
    assert main() == 1
    assert "line=1" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["one", "two", "outside", "deleted"])
def test_cli_base_ref_scope(tmp_path, monkeypatch, capsys, change):
    """
    Exercise public CLI scope checks against real temporary Git history.

    Extra files and deletion require maintainers while one modified manifest passes.
    """
    import subprocess

    repository = tmp_path / "repo"
    repository.mkdir()
    source = Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
    path = repository / "applications/app.busy.clock/manifest.yml"
    path.parent.mkdir(parents=True)
    path.write_text(source.read_text())

    def git(*args):
        """
        Run a local fixture Git command without remote access or inherited hooks.

        The fixture supplies its own identity so user Git settings are unnecessary.
        """
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repository,
            check=True,
            capture_output=True,
        )

    git("init", "-b", "main")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "base",
    )
    git("checkout", "-b", "proposal")
    if change == "deleted":
        path.unlink()
    elif change == "outside":
        (repository / "other.txt").write_text("outside")
    else:
        path.write_text(path.read_text() + "\n# release update\n")
        if change == "two":
            extra = repository / "applications/other/manifest.yml"
            extra.parent.mkdir()
            extra.write_text(source.read_text())
    git("add", "-A")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "proposal",
    )
    monkeypatch.chdir(repository)
    monkeypatch.setattr(
        "sys.argv",
        ["validate", str(path), "--categories-url", "", "--base-ref", "main"],
    )
    assert main() == (0 if change == "one" else 1)
    if change != "one":
        assert "maintainers" in capsys.readouterr().out


def test_step_summary_uses_the_comment_format(tmp_path, monkeypatch, capsys):
    """Print and write the same per-stage table that the pull request comment shows."""
    path = tmp_path / "app.busy.clock/manifest.yml"
    path.parent.mkdir()
    path.write_text(
        (
            Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
        ).read_text()
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("sys.argv", ["validate", str(path), "--categories-url", ""])
    assert main() == 0
    assert "✅ Passed" in capsys.readouterr().out
    text = summary.read_text()
    assert "✅ **Manifest** — 12 checks passed" in text
