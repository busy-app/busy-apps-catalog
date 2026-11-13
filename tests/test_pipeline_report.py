"""Tests for pull-request report orchestration."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.lib_comment import MARKER
from tools.pipeline_report import publish_report, read_compare_url, read_results


def test_update_existing_bot_comment_and_check() -> None:
    """
    Update the marked bot comment and create a failed check on the same SHA.

    User-owned comments with the marker are never selected for editing.
    """
    gateway = Mock()
    gateway.pull_head.return_value = "a" * 40
    gateway.comments.return_value = [
        {"id": 1, "user": {"login": "author"}, "body": MARKER},
        {"id": 2, "user": {"login": "bot"}, "body": MARKER},
    ]
    publish_report(
        gateway, 3, "a" * 40, "bot", [{"id": "package_size", "result": "fail"}]
    )
    gateway.update_comment.assert_called_once()
    assert gateway.update_comment.call_args.args[0] == 2
    gateway.create_comment.assert_not_called()
    assert gateway.create_check.call_args.args[:2] == ("a" * 40, "failure")


def test_refuse_stale_report() -> None:
    """
    Avoid publishing results after a pull request receives another commit.

    No comment or check should be created for stale findings.
    """
    gateway = Mock()
    gateway.pull_head.return_value = "b" * 40
    with pytest.raises(ValueError, match="head changed"):
        publish_report(gateway, 3, "a" * 40, "bot", [])
    gateway.comments.assert_not_called()
    gateway.create_check.assert_not_called()


def test_reject_inconsistent_results(tmp_path: Path) -> None:
    """
    Reject invalid or contradictory artifacts before any GitHub request.

    A failed required check cannot be presented as a passing result.
    """
    path = tmp_path / "results.json"
    path.write_text('{"status":"pass","checks":[{"id":"build","result":"fail"}]}')
    with pytest.raises(ValueError, match="Inconsistent"):
        read_results(path)
    path.write_text('{"status":"pass","checks":[{"id":"unknown","result":"fail"}]}')
    with pytest.raises(ValueError, match="Invalid check"):
        read_results(path)
    path.write_text(
        '{"status":"pass","checks":[{"id":"category_alias","result":"fail"}]}'
    )
    assert read_results(path) == [
        {"id": "category_alias", "result": "fail", "detail": ""}
    ]


def test_results_from_before_a_check_was_retired_still_read(tmp_path: Path) -> None:
    """
    A retired check in results recorded before its removal is dropped, not rejected.

    PR #179 was checked with `id_style`, then the check was removed and its publication
    failed with "Invalid check finding".
    """
    path = tmp_path / "results.json"
    path.write_text(
        '{"status":"pass","checks":[{"id":"id_style","result":"fail",'
        '"detail":"info.truetug.knock"},{"id":"category_alias","result":"pass"}]}'
    )
    assert read_results(path) == [
        {"id": "category_alias", "result": "pass", "detail": ""}
    ]
    path.write_text('{"status":"pass","checks":[{"id":"id_styles","result":"fail"}]}')
    with pytest.raises(ValueError, match="Invalid check"):
        read_results(path)


def test_conclusion_and_annotations_follow_the_icons() -> None:
    """❌ fails the check run; ⚠️ stays green and becomes a warning annotation."""
    gateway = Mock()
    gateway.pull_head.return_value = "a" * 40
    gateway.comments.return_value = []
    path = "applications/app.busy.clock/manifest.yml"
    publish_report(
        gateway,
        3,
        "a" * 40,
        "bot",
        [{"id": "category_alias", "result": "fail"}],
        manifest_path=path,
    )
    args = gateway.create_check.call_args.args
    assert args[1] == "success"
    assert [item["annotation_level"] for item in args[3]] == ["warning"]
    assert args[3][0]["path"] == path
    assert "Passed with warnings" in gateway.create_comment.call_args.args[1]
    publish_report(
        gateway,
        3,
        "a" * 40,
        "bot",
        [
            {"id": "source_commit", "result": "fail", "detail": "abc"},
            {"id": "category_alias", "result": "fail"},
        ],
        manifest_path=path,
    )
    args = gateway.create_check.call_args.args
    assert args[1] == "failure"
    assert {item["annotation_level"] for item in args[3]} == {"failure", "warning"}
    publish_report(gateway, 3, "a" * 40, "bot", [], manifest_path=path)
    args = gateway.create_check.call_args.args
    assert args[1] == "success" and list(args[3]) == []
    assert "✅ Passed" in gateway.create_comment.call_args.args[1]


def test_reviewer_note_for_confirmed_authorship() -> None:
    """A confirmed attestation is a reviewer note, not a warning."""
    gateway = Mock()
    gateway.pull_head.return_value = "a" * 40
    gateway.comments.return_value = []
    publish_report(gateway, 3, "a" * 40, "bot", [], author_attested=True)
    body = gateway.create_comment.call_args.args[1]
    assert "### For the reviewer" in body
    assert "⚠️" not in body
    assert gateway.create_check.call_args.args[1] == "success"


def test_read_compare_url_accepts_only_full_github_compare_links(
    tmp_path: Path,
) -> None:
    """Reject external URLs and short refs before rendering report links."""
    path = tmp_path / "results.json"
    valid = f"https://github.com/acme/clock/compare/{'a' * 40}...{'b' * 40}"
    path.write_text(f'{{"compare_url":"{valid}"}}')
    assert read_compare_url(path) == valid
    path.write_text('{"compare_url":"https://evil.test/clock"}')
    with pytest.raises(ValueError, match="comparison URL"):
        read_compare_url(path)


def test_missing_results_publish_internal_error_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A crashed check job still ends in a comment with `internal_error` and the run link."""
    from tools import pipeline_report

    gateway = Mock()
    gateway.pull_head.return_value = "a" * 40
    gateway.comments.return_value = []
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(pipeline_report, "GitHubGateway", lambda *_: gateway)
    monkeypatch.setattr(
        "sys.argv",
        [
            "report",
            "--results",
            str(tmp_path / "absent.json"),
            "--pr",
            "3",
            "--sha",
            "a" * 40,
            "--repository",
            "o/r",
            "--run-url",
            "https://github.com/o/r/actions/runs/9",
        ],
    )
    assert pipeline_report.main() == 0
    body = gateway.create_comment.call_args.args[1]
    assert "Checks could not finish" in body
    assert "https://github.com/o/r/actions/runs/9" in body
    assert "Traceback" not in body and "absent.json" not in body
    assert "Skipped: the checks could not finish" in body
    assert "passed" not in body.split("Review")[0].replace("Skipped", "")
    assert gateway.create_check.call_args.args[1] == "failure"
    assert "Traceback" in capsys.readouterr().err
