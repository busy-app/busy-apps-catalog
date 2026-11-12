"""Tests for the shared HTTP client: limits, redirects and retries, without network."""

import httpx2
import pytest

from tools import lib_http
from tools.lib_http import HttpError, request


def scripted(answers: list):
    """A handler returning the scripted answers in order; an exception is raised."""
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler, seen


def test_success_returns_body_and_sends_user_agent(mock_http) -> None:
    """A 200 is returned with its body; every request carries the CI User-Agent."""
    handler, seen = scripted([httpx2.Response(200, content=b"[]")])
    mock_http(handler)
    reply = request("GET", "https://example.test/x")
    assert reply.status == 200 and reply.content == b"[]"
    assert seen[0].headers["user-agent"] == lib_http.USER_AGENT


def test_response_over_the_limit_is_refused(mock_http) -> None:
    """A body larger than `max_bytes` is an error, not a truncated answer."""
    handler, _ = scripted([httpx2.Response(200, content=b"x" * 11)])
    mock_http(handler)
    with pytest.raises(HttpError):
        request("GET", "https://example.test/x", max_bytes=10)


def test_redirect_is_not_followed(mock_http) -> None:
    """A 3xx is final: one request, error with the status."""
    handler, seen = scripted([httpx2.Response(301, headers={"Location": "/y"})])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", "https://example.test/x")
    assert error.value.status == 301 and len(seen) == 1


def test_server_errors_and_network_errors_are_retried(mock_http) -> None:
    """5xx and a dropped connection are retried with 1 and 2 s pauses."""
    handler, seen = scripted(
        [
            httpx2.Response(503),
            httpx2.ConnectError("reset"),
            httpx2.Response(200, content=b"ok"),
        ]
    )
    sleeps = mock_http(handler)
    assert request("GET", "https://example.test/x").content == b"ok"
    assert len(seen) == 3 and sleeps == [1, 2]


def test_unreachable_after_all_attempts(mock_http) -> None:
    """Four network failures end with `status=None` after 1, 2, 4 s."""
    handler, seen = scripted([httpx2.ConnectError("down")] * 4)
    sleeps = mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", "https://example.test/x")
    assert error.value.status is None and len(seen) == 4 and sleeps == [1, 2, 4]


def test_rate_limit_waits_for_retry_after_capped(mock_http) -> None:
    """A 429 waits `Retry-After` seconds, never more than the cap."""
    handler, _ = scripted(
        [
            httpx2.Response(429, headers={"Retry-After": "3"}),
            httpx2.Response(429, headers={"Retry-After": "3600"}),
            httpx2.Response(200),
        ]
    )
    sleeps = mock_http(handler)
    request("GET", "https://example.test/x")
    assert sleeps == [3, lib_http.MAX_RETRY_AFTER]


def test_client_errors_are_final(mock_http) -> None:
    """A 404 is not retried."""
    handler, seen = scripted([httpx2.Response(404), httpx2.Response(200)])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", "https://example.test/x")
    assert error.value.status == 404 and len(seen) == 1


def test_post_is_retried_only_when_asked(mock_http) -> None:
    """POST is not idempotent: no retry by default, retry with `retry=True`."""
    handler, seen = scripted([httpx2.Response(500), httpx2.Response(200)])
    mock_http(handler)
    with pytest.raises(HttpError):
        request("POST", "https://example.test/x", json={})
    assert len(seen) == 1
    handler, seen = scripted([httpx2.Response(500), httpx2.Response(200)])
    mock_http(handler)
    assert request("POST", "https://example.test/x", json={}, retry=True).status == 200
    assert len(seen) == 2


def test_jitter_adds_at_most_a_quarter() -> None:
    """Backoff jitter stays within delay..1.25 × delay."""
    for delay in (1, 2, 4):
        assert delay <= lib_http.retry_delay(delay, None) <= delay * 1.25


def test_error_text_has_no_url_or_body(mock_http) -> None:
    """The error message carries the status only, so tokens in URLs never leak."""
    handler, _ = scripted([httpx2.Response(403, content=b"secret body")])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", "https://example.test/x?token=secret")
    assert str(error.value) == "HTTP 403"


def test_accepted_status_counts_as_success(mock_http) -> None:
    """A status named in `accept` (412 on a PUT) is returned, not raised."""
    handler, _ = scripted([httpx2.Response(412, content=b"exists")])
    mock_http(handler)
    reply = request("PUT", "https://example.test/x", content=b"a", accept={412})
    assert reply.status == 412 and reply.content == b"exists"


def test_error_body_is_kept_only_when_asked_and_cut(mock_http) -> None:
    """The start of an error answer is available for messages, never more than asked."""
    handler, _ = scripted([httpx2.Response(403, content=b"x" * 50)])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", "https://example.test/x", error_body=10)
    assert error.value.body == b"x" * 10 and "x" not in str(error.value)
    handler, _ = scripted([httpx2.Response(403, content=b"secret")])
    mock_http(handler)
    with pytest.raises(HttpError) as plain:
        request("GET", "https://example.test/x")
    assert plain.value.body == b""


def test_501_is_final_but_503_is_retried(mock_http, monkeypatch) -> None:
    """Not implemented cannot pass; an outage can."""
    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    handler, seen = scripted([httpx2.Response(501)])
    mock_http(handler)
    with pytest.raises(HttpError):
        request("POST", "https://example.test/x", retry=True)
    assert len(seen) == 1
    handler, seen = scripted(
        [httpx2.Response(503), httpx2.Response(200, content=b"{}")]
    )
    mock_http(handler)
    assert request("POST", "https://example.test/x", retry=True).status == 200
    assert len(seen) == 2


def test_streamed_content_is_sent_again_on_retry_without_chunking(
    mock_http, monkeypatch
) -> None:
    """A function body is called per attempt; with Content-Length the body is not chunked."""
    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    calls = []

    def chunks():
        calls.append(1)
        yield b"ab"
        yield b"cd"

    handler, seen = scripted([httpx2.Response(503), httpx2.Response(200)])
    mock_http(handler)
    request(
        "PUT",
        "https://example.test/x",
        headers={"Content-Length": "4"},
        content=chunks,
    )
    assert len(calls) == 2
    assert seen[1].content == b"abcd"
    assert "transfer-encoding" not in seen[1].headers


def test_lost_connection_is_final_when_network_retry_is_off(
    mock_http, monkeypatch
) -> None:
    """For a request that may already have been applied, only an answer is retried."""
    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    handler, seen = scripted(
        [httpx2.ConnectError("x", request=httpx2.Request("POST", "https://e.test"))]
    )
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("POST", "https://example.test/x", retry=True, retry_network=False)
    assert error.value.status is None and len(seen) == 1
