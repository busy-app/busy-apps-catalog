"""Hiding a removed app after its PR is merged: the request, 404, retries and the comment."""

import json

import httpx2
import pytest

from tools import lib_http
from tools.publish_remove import main, removal_url, remove_application
from tools.publish_report import publish_confirmation, removal_body

ID = "app.busy.clock"


class Gateway:
    """The comments of a PR; records what was written."""

    def __init__(self, comments=()) -> None:
        self._comments = list(comments)
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []

    def comments(self, number):
        return self._comments

    def create_comment(self, number, body):
        self.created.append(body)

    def update_comment(self, comment_id, body):
        self.updated.append((comment_id, body))


def test_the_request_hides_and_carries_the_pr_and_the_approver(mock_http) -> None:
    """DELETE without `purge`, with `Token`, the PR number and the approver for the audit."""
    seen: list[httpx2.Request] = []

    def handler(req):
        seen.append(req)
        return httpx2.Response(204)

    mock_http(handler)
    result = remove_application("https://api.test/", "secret", ID, 9, "Re Viewer")
    assert result == {"removed": True, "app_id": ID, "already_gone": False}
    request = seen[0]
    assert request.method == "DELETE" and "purge" not in str(request.url)
    assert request.url.path == f"/api/catalog/v1/applications/{ID}"
    assert request.url.params["pr_number"] == "9"
    assert request.url.params["approved_by"] == "Re Viewer"
    assert (
        request.headers["token"] == "secret" and "authorization" not in request.headers
    )


def test_a_404_is_a_success_with_a_note(mock_http) -> None:
    """The app is already gone: nothing to do, not an error."""
    mock_http(lambda req: httpx2.Response(404, json={"detail": "not found"}))
    result = remove_application("https://api.test", "t", ID, 9, "r")
    assert result["already_gone"] is True


def test_an_error_names_the_status_and_the_backend_reason_but_not_the_token(
    mock_http,
) -> None:
    """A refusal is a clear failure of the job."""
    mock_http(
        lambda req: httpx2.Response(403, json={"detail": "Insufficient token role"})
    )
    with pytest.raises(RuntimeError) as error:
        remove_application("https://api.test", "secret-token", ID, 9, "r")
    assert "HTTP 403" in str(error.value) and "Insufficient" in str(error.value)
    assert "secret-token" not in str(error.value)


def test_5xx_is_retried_but_a_lost_connection_is_not(mock_http, monkeypatch) -> None:
    """Like notify: an answer may be repeated, a connection lost mid-request may not."""
    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    answers = [httpx2.Response(503), httpx2.Response(204)]
    mock_http(lambda req: answers.pop(0))
    assert remove_application("https://api.test", "t", ID, 9, "r")["removed"] is True
    calls = []

    def lost(req):
        calls.append(req)
        raise httpx2.ReadError("lost", request=req)

    mock_http(lost)
    with pytest.raises(RuntimeError):
        remove_application("https://api.test", "t", ID, 9, "r")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        ("http://example.test", ID, 9),
        ("https://api.test", "Bad Id", 9),
        ("https://api.test", ID, 0),
        ("https://api.test?x=1", ID, 9),
    ],
)
def test_bad_inputs_are_refused_before_a_request(bad) -> None:
    """A plain-HTTP host, an invalid id or PR, or a query in the address are errors."""
    with pytest.raises(ValueError):
        removal_url(*bad, "r")
    with pytest.raises(ValueError, match="token"):
        remove_application("https://api.test", "", ID, 9, "r")


def test_without_a_catalog_api_it_is_a_dry_run(monkeypatch, capsys) -> None:
    """No `CATALOG_API`: describe, change nothing."""
    monkeypatch.delenv("CATALOG_API", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["x", "--app-id", ID, "--pr", "9", "--approved-by", "r"]
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_the_confirmation_comment_for_a_removal() -> None:
    """One bot comment: removed, or removed and already gone; a dry run says nothing."""
    gateway = Gateway()
    publish_confirmation(
        gateway, 9, {"removed": True, "app_id": ID, "already_gone": False}
    )
    assert gateway.created == [
        f"<!-- catalog-published -->\nRemoved from the catalog: `{ID}`\n"
    ]
    assert "already gone" in removal_body(
        {"removed": True, "app_id": ID, "already_gone": True}
    )
    quiet = Gateway()
    publish_confirmation(quiet, 9, {"dry_run": True, "removed": False, "app_id": ID})
    assert quiet.created == []
    with pytest.raises(TypeError):
        removal_body({"removed": True, "app_id": "not an id"})
    existing = [
        {
            "id": 5,
            "user": {"login": "github-actions[bot]"},
            "body": "<!-- catalog-published -->\nold",
        }
    ]
    again = Gateway(existing)
    publish_confirmation(again, 9, {"removed": True, "app_id": ID})
    assert again.updated[0][0] == 5 and not again.created


def test_a_refusal_is_written_for_the_report(mock_http, monkeypatch, tmp_path) -> None:
    """With `--failure`, the backend's status and reason land in the file."""
    mock_http(
        lambda req: httpx2.Response(403, json={"detail": "Insufficient token role"})
    )
    monkeypatch.setenv("CATALOG_API", "https://api.test")
    monkeypatch.setenv("CATALOG_CI_TOKEN", "secret-token")
    failure = tmp_path / "failure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--app-id",
            ID,
            "--pr",
            "9",
            "--approved-by",
            "r",
            "--failure",
            str(failure),
        ],
    )
    with pytest.raises(RuntimeError):
        main()
    assert json.loads(failure.read_text()) == {
        "status": 403,
        "detail": "Insufficient token role",
    }
