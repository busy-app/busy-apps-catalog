"""GitHub calls repeat on transient failures only; the pauses are not slept for real."""

import subprocess
from unittest.mock import Mock

import pytest

from e2e.infrastructure import GitHub, HarnessNetworkError, is_network, transient


def done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    """What `gh` returned."""
    return subprocess.CompletedProcess([], code, out, err)


def github(*answers) -> tuple[GitHub, Mock, Mock]:
    """A client whose `gh` gives the answers in turn."""
    runner, sleep = Mock(side_effect=list(answers)), Mock()
    return GitHub(runner=runner, sleep=sleep), runner, sleep


def test_a_network_error_is_repeated_until_it_works() -> None:
    """Two lost connections, then the answer."""
    client, runner, sleep = github(
        done(1, err='Get "https://api.github.com/x": TLS handshake timeout'),
        done(1, err="error connecting to api.github.com"),
        done(0, '{"ok": true}'),
    )
    assert client.request("GET", "repos/x") == {"ok": True}
    assert runner.call_count == 3 and sleep.call_count == 2


def test_a_client_error_is_final() -> None:
    """404 and 422 are answers, not accidents."""
    client, runner, sleep = github(done(1, err="gh: Not Found (HTTP 404)"))
    with pytest.raises(RuntimeError, match="HTTP 404"):
        client.request("GET", "repos/x")
    assert runner.call_count == 1 and sleep.call_count == 0


def test_get_5xx_is_repeated_then_given_up_as_a_network_error() -> None:
    """The fourth failure is final and counts as the harness, not a feature."""
    client, runner, _ = github(*[done(1, err="gh: Bad Gateway (HTTP 502)")] * 4)
    with pytest.raises(HarnessNetworkError, match="HTTP 502"):
        client.request("GET", "repos/x")
    assert runner.call_count == 4


def test_a_post_with_a_status_is_never_repeated() -> None:
    """A 502 may follow an applied request; the repeat would fail as a duplicate."""
    client, runner, _ = github(done(1, err="gh: Bad Gateway (HTTP 502)"), done(0, "{}"))
    with pytest.raises(RuntimeError):
        client.request("POST", "repos/x/git/refs", {"ref": "r"})
    assert runner.call_count == 1


def test_a_post_that_never_connected_is_repeated() -> None:
    """Nothing was sent, so nothing can be duplicated."""
    client, runner, _ = github(
        done(1, err="error connecting to api.github.com"), done(0, '{"a": 1}')
    )
    assert client.request("POST", "repos/x", {"a": 1}) == {"a": 1}
    assert runner.call_count == 2


def test_rate_limits_are_transient_even_as_403() -> None:
    """Primary and secondary limits come as 403 or 429, for any method."""
    assert transient("gh: API rate limit exceeded (HTTP 403)", "GET")
    assert transient("gh: secondary rate limit (HTTP 403)", "POST")
    assert transient("gh: Too Many Requests (HTTP 429)", "GET")
    assert not transient("gh: Forbidden (HTTP 403)", "GET")


def test_deterministic_failures_are_final_at_once() -> None:
    """No auth and empty output are not network trouble: one run, no pauses."""
    for failure in ("gh: To get started with GitHub CLI, run: gh auth login", ""):
        client, runner, sleep = github(done(1, err=failure))
        with pytest.raises(RuntimeError) as caught:
            client.request("GET", "repos/x")
        assert not isinstance(caught.value, HarnessNetworkError)
        assert runner.call_count == 1 and sleep.call_count == 0


def test_501_is_final_and_a_lost_get_answer_is_repeated() -> None:
    """The retry policy is that of `lib_http`."""
    assert not transient("gh: Not Implemented (HTTP 501)", "GET")
    assert transient("EOF", "GET")
    assert not transient("EOF", "POST")
    assert is_network("EOF") and is_network("HTTP 504") and not is_network("HTTP 404")


def test_retries_stop_at_the_wait_deadline(monkeypatch) -> None:
    """A poll with a short deadline does not sleep past it."""
    from e2e import infrastructure

    monkeypatch.setattr(infrastructure.time, "monotonic", lambda: 100.0)
    token = infrastructure.DEADLINE.set(100.5)
    try:
        client, runner, sleep = github(*[done(1, err="error connecting")] * 4)
        with pytest.raises(HarnessNetworkError):
            client.request("GET", "repos/x")
    finally:
        infrastructure.DEADLINE.reset(token)
    assert runner.call_count == 1 and sleep.call_count == 0
