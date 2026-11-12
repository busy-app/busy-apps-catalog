"""Tests for the GitHub report adapter without network access."""

from unittest.mock import patch

import pytest

from tools.lib_github import GitHubGateway


def test_comment_pagination_and_check() -> None:
    """
    Read all comment pages and construct a check run for the reviewed SHA.

    A fake request transport prevents real GitHub writes.
    """
    gateway = GitHubGateway("owner/repository", "fake")
    with patch.object(
        gateway,
        "_request",
        side_effect=[[{"id": i} for i in range(100)], [{"id": 101}], {}],
    ) as request:
        assert len(gateway.comments(5)) == 101
        gateway.create_check("a" * 40, "success", "Done")
    assert "page=2" in request.call_args_list[1].args[1]
    assert request.call_args_list[2].args[2]["name"] == "catalog-checks"


def test_reject_repo_url() -> None:
    """
    Reject repository strings that could change the GitHub API destination.

    Tokens must never be sent to an arbitrary endpoint.
    """
    with pytest.raises(ValueError):
        GitHubGateway("example.com/owner/repo", "fake")


def request_with(mock_http, statuses: list[int], method: str = "GET"):
    """Run one request against scripted statuses; return result, calls, sleeps."""
    import httpx2

    answers = list(statuses)
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.headers["authorization"])
        status = answers.pop(0)
        return httpx2.Response(status, json={"ok": True} if status == 200 else {})

    sleeps = mock_http(handler)
    try:
        result = GitHubGateway("owner/repository", "fake")._request(method, "pulls/1")
    except RuntimeError as error:
        result = error
    return result, calls, sleeps


def test_server_errors_are_retried_with_backoff(mock_http) -> None:
    """500, 500, 200 succeeds after 1 s and 2 s pauses, with the token on every attempt."""
    result, calls, sleeps = request_with(mock_http, [500, 500, 200])
    assert result == {"ok": True} and calls == ["Bearer fake"] * 3 and sleeps == [1, 2]


def test_persistent_server_error_raises_after_four_attempts(mock_http) -> None:
    """Four 500 answers raise and wait 1, 2 and 4 seconds."""
    result, calls, sleeps = request_with(mock_http, [500, 500, 500, 500])
    assert isinstance(result, RuntimeError) and "HTTP 500" in str(result)
    assert len(calls) == 4 and sleeps == [1, 2, 4]


def test_client_errors_and_posts_are_not_retried(mock_http) -> None:
    """A 404 is final; a POST is not repeated, so no comment is duplicated."""
    result, calls, _ = request_with(mock_http, [404, 200])
    assert isinstance(result, RuntimeError) and len(calls) == 1
    result, calls, _ = request_with(mock_http, [500, 200], method="POST")
    assert isinstance(result, RuntimeError) and len(calls) == 1


def test_redirect_is_an_error_and_not_followed(mock_http) -> None:
    """A 302 to another host is reported, the token never leaves api.github.com."""
    import httpx2

    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        return httpx2.Response(302, headers={"Location": "https://evil.example/"})

    mock_http(handler)
    with pytest.raises(RuntimeError, match="HTTP 302"):
        GitHubGateway("owner/repository", "fake")._request("GET", "pulls/1")
    assert hosts == ["api.github.com"]


def test_owner_login_keeps_github_capitalization() -> None:
    """`owner.login` is returned as GitHub spells it."""
    from unittest.mock import patch

    from tools.lib_github import GitHubGateway

    gateway = GitHubGateway("iMalkav/chess", "token")
    with patch.object(
        GitHubGateway, "_request", return_value={"owner": {"login": "iMalkav"}}
    ) as request:
        assert gateway.owner_login() == "iMalkav"
    request.assert_called_once_with("GET", "")
    with (
        patch.object(GitHubGateway, "_request", return_value={"owner": {}}),
        pytest.raises(ValueError),
    ):
        gateway.owner_login()
