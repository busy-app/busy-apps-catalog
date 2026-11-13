"""Tests for the deterministic PR report renderer."""

from tools.check_registry import outcome
from tools.lib_comment import (
    MARKER,
    find_comment,
    plural,
    render_checks,
    render_comment,
)


def test_headlines() -> None:
    """The first line is the verdict: required failure, warning only, or clean."""
    assert "❌ Action required" in render_comment([outcome("source_commit")])
    assert "⚠️ Passed with warnings" in render_comment([outcome("category_alias")])
    clean = render_comment([])
    assert "✅ Passed" in clean and "Action required" not in clean


def test_render_shows_every_stage_and_the_fix() -> None:
    """Passed stages are one line; problems carry title, problem, fix and detail."""
    result = render_comment([outcome("source_commit", "fail", "abc")])
    assert result.count(MARKER) == 1
    for title in ("Manifest", "Build", "Package", "Catalog", "Review"):
        assert title in result
    assert "✅ **Manifest** — 11 checks passed" in result
    assert "❌ | Source commit | No commit found. (abc)" in result
    assert (
        "Check that the corresponding commit is available in your repository" in result
    )


def test_failed_build_collapses_later_stages_into_one_row() -> None:
    """Stages that could not run are not listed as twenty red rows."""
    result = render_comment(
        [outcome("build", "fail", "boom")], skipped=["package", "catalog"]
    )
    assert "➖ **Package, Catalog** — Skipped: build failed" in result
    assert "Archive format" not in result


def test_optional_not_run_is_a_warning_required_is_could_not_run() -> None:
    """A check that could not run is ⚠️ if optional and ❌ 'Could not run' if required."""
    optional = render_comment([outcome("catalog_unavailable", "not_run")])
    assert "⚠️ | Catalog lookup" in optional
    required = render_comment([outcome("version_not_newer", "not_run")])
    assert "❌ | Version is new | Could not run:" in required


def test_detail_is_escaped() -> None:
    """Untrusted detail cannot inject a second marker, markup or table column."""
    result = render_comment(
        [outcome("markdown", "fail", "<b>x</b> | a\n<!-- catalog-checks --> `y`")]
    )
    assert result.count(MARKER) == 1
    assert "<b>" not in result and "&lt;b&gt;" in result
    assert "&#124;" in result


def test_find_only_bot_comment() -> None:
    """
    Select only the bot's marked comment for replacement.

    A marker authored by a different user is ignored.
    """
    comments = [
        {"id": 1, "body": MARKER, "user": {"login": "author"}},
        {"id": 2, "body": MARKER, "user": {"login": "github-actions[bot]"}},
    ]
    assert find_comment(comments, "github-actions[bot]") == 2
    assert find_comment(comments[:1], "github-actions[bot]") is None


def test_render_package_summary() -> None:
    """Show counts and sizes per directory, not file names."""
    result = render_comment(
        [],
        inventory=[
            {"path": "clock/scripts/main.js", "size": 2048},
            {"path": "clock/scripts/a.js", "size": 2048},
            {"path": "clock/sounds/t.snd", "size": 1024 * 1024},
            {"path": "clock/appmeta/manifest.json", "size": 243},
            {"path": "clock/readme", "size": 1},
        ],
    )
    assert "### Package" in result
    assert "**5 files, 1.0 MB unpacked**" in result
    assert "- Code: 2 files, 4.0 KB" in result
    assert "- Sounds: 1 files, 1.0 MB" in result
    assert "- App metadata: 1 files, 243 B" in result
    assert "- Other: 1 files, 1 B" in result
    assert "main.js" not in result


def test_passed_count_follows_the_run_order_of_the_build_stage() -> None:
    """A failure at any point of the build counts exactly the checks that ran before it."""
    from tools.check_registry import REGISTRY

    order = [check.id for check in REGISTRY.values() if check.stage == "build"]
    for check_id in (
        "screenshot_png",
        "back_screenshot_png",
        "build",
        "screenshot_final",
        "back_screenshot_final",
        "icon_png",
    ):
        text = "\n".join(render_checks([outcome(check_id, "fail", "x")]))
        expected = order.index(check_id)
        assert f"- ✅ {plural(expected)} passed" in text, check_id
    # the checks that run after the media step are counted as not run
    assert order.index("screenshot_final") > order.index("script_imports")
    assert order.index("back_screenshot_final") == order.index("screenshot_final") + 1
    assert order.index("icon_png") == order.index("back_screenshot_final") + 1
    assert order.index("screenshot_png") < order.index("back_screenshot_png")
    assert order.index("back_screenshot_png") < order.index("build")
    assert order.index("screenshot_png") < order.index("build")


def test_license_goes_to_the_reviewer_section() -> None:
    """The recognised license is named for the reviewer."""
    text = render_comment([], license_name="MIT")
    assert "### For the reviewer" in text and "- License: MIT" in text
    assert "License:" not in render_comment([])
