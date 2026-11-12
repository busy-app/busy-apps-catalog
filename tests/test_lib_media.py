"""Tests for the Screenshots section of the Checks comment."""

from tools.lib_comment import render_comment
from tools.lib_media import build_view, parse_view, render_media

COMMIT = "a" * 40


def manifest(**extra) -> dict:
    """A manifest pointing at a subdirectory of an author's repository."""
    return {
        "sourcecode": {
            "type": "git",
            "location": {
                "origin": "https://github.com/owner/repo",
                "commit_sha": COMMIT,
                "subdir": "apps/clock",
            },
        },
        "screenshots": ["shots/one.png", "shots/two words.png"],
        **extra,
    }


def test_front_back_and_icons_at_their_real_size() -> None:
    """Every image keeps its size and links to the file pinned to the full commit."""
    view = build_view(
        manifest(back_screenshots=["back/1.png"]),
        {"icon_front": "apps/clock/src/appmeta/icon_front_8x8.png"},
    )
    text = "\n".join(render_media(view))
    assert "### Screenshots" in text
    assert (
        f"https://raw.githubusercontent.com/owner/repo/{COMMIT}/apps/clock/shots/one.png"
        in text
    )
    assert 'width="72" height="16"' in text and 'width="160" height="80"' in text
    assert 'width="8" height="8"' in text and 'width="11"' not in text
    assert f"https://github.com/owner/repo/blob/{COMMIT}/apps/clock/back/1.png" in text


def test_spaces_are_encoded_and_alt_is_escaped() -> None:
    """Author paths are untrusted: encoded in URLs, escaped in alt text."""
    view = build_view(manifest(screenshots=['a b/"><script>.png']))
    text = "\n".join(render_media(view))
    assert "a%20b/%22%3E%3Cscript%3E.png" in text
    assert "<script>" not in text and "&quot;&gt;&lt;script&gt;" in text


def test_no_icon_found_and_bad_input_show_nothing_extra() -> None:
    """Without icons only the screenshots show; broken or foreign data shows nothing."""
    text = "\n".join(render_media(build_view(manifest())))
    assert 'height="8"' not in text
    assert (
        build_view(
            {
                "sourcecode": {
                    "location": {
                        "origin": "https://evil.example/o/r",
                        "commit_sha": COMMIT,
                    }
                }
            }
        )
        is None
    )
    assert (
        build_view(
            {
                "sourcecode": {
                    "location": {
                        "origin": "https://github.com/o/r",
                        "commit_sha": "main",
                    }
                }
            }
        )
        is None
    )
    assert render_media(None) == []
    view = build_view(manifest(screenshots=["../escape.png", "/abs.png", "ok.png"]))
    assert [item["path"] for item in view["items"]] == ["apps/clock/ok.png"]


def test_report_side_validation_rejects_forged_results() -> None:
    """The trusted report re-validates what the check job wrote."""
    good = build_view(manifest())
    assert parse_view(good) == good
    for bad in (
        {**good, "commit_sha": "main"},
        {**good, "repo": "o/r/../x"},
        {**good, "items": [{"kind": "screenshot", "path": "../x.png"}]},
        {**good, "items": [{"kind": "script", "path": "x.png"}]},
        {**good, "items": [{"kind": "screenshot", "path": "x" * 400}]},
        "text",
    ):
        assert parse_view(bad) is None


def test_comment_has_the_section() -> None:
    """The section is part of the rendered comment."""
    assert "### Screenshots" in render_comment([], media=build_view(manifest()))
    assert "### Screenshots" not in render_comment([])
