"""Tests for publication confirmation without GitHub writes."""

from unittest.mock import Mock

import pytest

from tools.publish_report import (
    FAILED,
    MARKER,
    failure_body,
    main,
    post_failure,
    publish_confirmation,
)

RUN = "https://github.com/busy-app/busy-apps-catalog-dev/actions/runs/42"


def test_confirmation_is_idempotent_and_dry_run_is_quiet() -> None:
    """
    Update an existing bot confirmation and never post for a dry run.

    Matching markers from other users are ignored.
    """
    gateway = Mock()
    gateway.comments.return_value = [
        {"id": 1, "body": MARKER, "user": {"login": "author"}},
        {"id": 2, "body": MARKER, "user": {"login": "github-actions[bot]"}},
    ]
    publish_confirmation(
        gateway, 5, {"url": "https://cloud.busy.app/api/catalog/v1/applications/clock"}
    )
    assert gateway.update_comment.call_args.args[0] == 2
    gateway.reset_mock()
    publish_confirmation(gateway, 5, {"dry_run": True})
    gateway.comments.assert_not_called()


MERGE = "c" * 40


def test_failure_names_the_backend_reason_and_the_run() -> None:
    """A refused publication says so in the PR, with the reason, the run and the merge."""
    body = failure_body(
        "publish",
        {"status": 409, "detail": "Application belongs to another repository"},
        RUN,
        MERGE,
    )
    assert body.startswith(MARKER + "\n" + FAILED + "\n**Not published.**")
    assert (
        "refused this version (HTTP 409): Application belongs to another repository"
        in body
    )
    assert f"[publication run]({RUN})" in body and f"`{MERGE}`" in body


def test_failure_without_an_answer_claims_no_refusal() -> None:
    """Without a backend status the run failed somewhere: nothing is confirmed, nothing refused."""
    body = failure_body("publish", {"status": None, "detail": ""}, RUN, MERGE)
    assert "**Publication not confirmed.**" in body
    assert "refused" not in body and "HTTP" not in body
    removal = failure_body("remove", {"status": None, "detail": ""}, RUN, MERGE)
    assert "**Removal not confirmed.**" in removal


def test_failure_of_a_removal() -> None:
    """A refused removal names the status and the reason."""
    body = failure_body("remove", {"status": 403, "detail": "Insufficient"}, RUN, MERGE)
    assert "**Not removed.**" in body and "(HTTP 403): Insufficient" in body
    bare = failure_body("remove", {"status": 502, "detail": ""}, RUN, MERGE)
    assert "(HTTP 502)." in bare


def test_failure_detail_is_made_safe() -> None:
    """Mentions, markup and line breaks from the answer do not render."""
    body = failure_body(
        "publish", {"status": 409, "detail": "@team <b>x</b> `y`\nz"}, RUN, MERGE
    )
    assert "@\u200bteam" in body and "&lt;b>" in body and "`y`" not in body
    assert "\nz" not in body


@pytest.mark.parametrize(
    ("run_url", "merge"),
    [
        ("http://github.com/a/b/actions/runs/1", MERGE),
        ("https://example.com/a/b/actions/runs/1", MERGE),
        ("https://github.com/a/b/pull/1", MERGE),
        ("", MERGE),
        (RUN, "main"),
        (RUN, ""),
    ],
)
def test_failure_needs_a_run_url_and_a_merge_commit(run_url: str, merge: str) -> None:
    """The link can only point at a GitHub Actions run; the merge is a full SHA."""
    with pytest.raises(ValueError):
        failure_body("publish", {"status": 409, "detail": "x"}, run_url, merge)


def bot(comment_id: int, body: str) -> dict:
    """A comment of the workflow bot."""
    return {"id": comment_id, "body": body, "user": {"login": "github-actions[bot]"}}


def test_a_later_success_replaces_the_failure() -> None:
    """The failure and the confirmation share one bot comment."""
    gateway = Mock()
    failed = failure_body("publish", {"status": 409, "detail": "x"}, RUN, MERGE)
    gateway.comments.return_value = [bot(7, failed)]
    publish_confirmation(
        gateway, 5, {"url": "https://cloud.busy.app/api/catalog/v1/applications/clock"}
    )
    assert gateway.update_comment.call_args.args[0] == 7
    assert "Published:" in gateway.update_comment.call_args.args[1]


def test_a_failure_never_replaces_a_confirmation() -> None:
    """A failed later run leaves "Published" alone; an earlier failure is replaced."""
    failed = failure_body("publish", {"status": 409, "detail": "x"}, RUN, MERGE)
    gateway = Mock()
    gateway.comments.return_value = [bot(3, f"{MARKER}\nPublished: https://x/y\n")]
    assert post_failure(gateway, 5, failed) is False
    gateway.update_comment.assert_not_called()
    gateway.create_comment.assert_not_called()
    gateway.comments.return_value = [bot(4, failed)]
    assert post_failure(gateway, 5, failed) is True
    assert gateway.update_comment.call_args.args[0] == 4
    gateway.comments.return_value = []
    assert post_failure(gateway, 5, failed) is True
    gateway.create_comment.assert_called_once()


def test_main_reports_a_failure_without_a_failure_file(monkeypatch, tmp_path) -> None:
    """The report job posts even when the failing job left no file."""
    gateway = Mock()
    gateway.comments.return_value = []
    monkeypatch.setattr("tools.publish_report.GitHubGateway", lambda *args: gateway)
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--failed",
            "publish",
            "--failure",
            str(tmp_path / "missing.json"),
            "--run-url",
            RUN,
            "--merge-sha",
            MERGE,
            "--pr",
            "5",
        ],
    )
    assert main() == 0
    body = gateway.create_comment.call_args.args[1]
    assert "**Publication not confirmed.**" in body
