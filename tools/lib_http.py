"""One HTTP client for the catalog tools: bounded responses, no redirects, retries."""

import atexit
import random
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass

import httpx2

USER_AGENT = "busy-apps-catalog-ci"
MAX_RESPONSE = 1024 * 1024
RETRY_DELAYS = (1, 2, 4)
MAX_RETRY_AFTER = 30
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "PATCH", "DELETE"})

# Tests replace it with an httpx2.MockTransport; None means the real network.
TRANSPORT: httpx2.BaseTransport | None = None
_client: httpx2.Client | None = None


class HttpError(Exception):
    """A request failed: `status` is the final HTTP status, None if nothing answered."""

    def __init__(self, status: int | None, body: bytes = b"") -> None:
        """
        Keep the status, and the start of the body only if the caller asked for it.

        The message never holds the URL, headers or body (they may hold secrets).
        """
        super().__init__(f"HTTP {status}" if status is not None else "unreachable")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Reply:
    """A successful (2xx) answer read up to the size limit."""

    status: int
    headers: Mapping[str, str]
    content: bytes


def client() -> httpx2.Client:
    """The process-wide client, created on first use and closed at exit."""
    global _client
    if _client is None:
        _client = httpx2.Client(
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            transport=TRANSPORT,
        )
        atexit.register(_client.close)
    return _client


def reset() -> None:
    """Drop the shared client, so the next request picks up a new TRANSPORT."""
    global _client
    if _client is not None:
        _client.close()
    _client = None


def retry_delay(attempt_delay: float, response: httpx2.Response | None) -> float:
    """Pause before the next attempt: `Retry-After` of a 429, otherwise backoff with jitter."""
    if response is not None and response.status_code == 429:
        value = response.headers.get("retry-after", "")
        if value.isdigit():
            return min(int(value), MAX_RETRY_AFTER)
    return attempt_delay + random.uniform(0, attempt_delay / 4)


def retryable(status: int | None) -> bool:
    """Network errors, server errors and rate limits may pass; 501 and 4xx are final."""
    return status is None or (status >= 500 and status != 501) or status == 429


def read_limited(
    response: httpx2.Response, max_bytes: int, truncate: bool = False
) -> bytes:
    """Read the body; more than `max_bytes` is an error, or just cut off with `truncate`."""
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            if truncate:
                chunks.append(chunk[: max_bytes - (size - len(chunk))])
                break
            raise HttpError(response.status_code)
        chunks.append(chunk)
    return b"".join(chunks)


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json: object = None,
    content: bytes | Callable[[], Iterable[bytes]] | None = None,
    timeout: float = 30,
    max_bytes: int = MAX_RESPONSE,
    retry: bool | None = None,
    accept: Collection[int] = (),
    error_body: int = 0,
    retry_network: bool = True,
) -> Reply:
    """
    Send one request and return a 2xx answer; anything else raises `HttpError`.

    `content` is bytes or a function giving the chunks again for every attempt (set
    `Content-Length` to avoid chunked encoding). `accept` adds success statuses (412 on
    a PUT); `error_body` keeps that many bytes of an error answer in `HttpError.body`.
    Redirects are not followed. Only idempotent methods are retried by default; with
    `retry_network=False` a lost connection is final (the request may have been applied).
    """
    allowed = method in IDEMPOTENT if retry is None else retry
    delays = iter(RETRY_DELAYS if allowed else ())
    while True:
        response = None
        body = b""
        try:
            with client().stream(
                method,
                url,
                headers=headers,
                json=json,
                content=content() if callable(content) else content,
                timeout=timeout,
            ) as response:
                status = response.status_code
                if 200 <= status < 300 or status in accept:
                    body = read_limited(response, max_bytes)
                    return Reply(status, dict(response.headers), body)
                if error_body:
                    body = read_limited(response, error_body, truncate=True)
        except httpx2.TransportError:
            status = None
        may_retry = retryable(status) and (status is not None or retry_network)
        delay = next(delays, None) if may_retry else None
        if delay is None:
            raise HttpError(status, body)
        time.sleep(retry_delay(delay, response))
