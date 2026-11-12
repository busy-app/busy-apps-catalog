"""Bounded GitHub REST and polling adapters for the E2E use cases."""

import json
import os
import random
import re
import subprocess
import time
from collections.abc import Callable
from contextvars import ContextVar
from urllib.parse import quote

from e2e.domain import ref_gone
from tools import lib_http

# gh messages that prove the request never left this machine: safe to repeat a POST.
NOT_SENT = ("error connecting", "tls handshake timeout", "no such host", "dial tcp")
LOST = ("timeout", "timed out", "eof", "connection reset", "connection refused")
STATUS = re.compile(r"HTTP (\d{3})")
# The deadline of the wait in progress: retries never run past it.
DEADLINE: ContextVar[float | None] = ContextVar("deadline", default=None)


class HarnessNetworkError(RuntimeError):
    """A network or GitHub outage persisted after the retries: not a feature failure."""


def rate_limited(status: int | None, text: str) -> bool:
    """GitHub answers primary and secondary rate limits with 429 or 403."""
    return status in {403, 429} and "rate limit" in text.lower()


def is_network(stderr: str) -> bool:
    """Whether the failure is the network or GitHub being down, whatever the method."""
    match = STATUS.search(stderr)
    status = int(match.group(1)) if match else None
    if status is None:
        return any(item in stderr.lower() for item in (*NOT_SENT, *LOST))
    return rate_limited(status, stderr) or lib_http.retryable(status)


def transient(stderr: str, method: str) -> bool:
    """
    Whether a failed `gh api` call is worth repeating.

    A POST that got any HTTP status is never repeated (5xx can follow an applied request,
    and the repeat would fail as a duplicate); it is repeated only when it certainly never
    left this machine, or when the answer was a rate limit. Without a status only network
    markers count: missing auth or empty output is final at once.
    """
    match = STATUS.search(stderr)
    status = int(match.group(1)) if match else None
    if rate_limited(status, stderr):
        return True
    if status is not None:
        return method != "POST" and lib_http.retryable(status)
    lowered = stderr.lower()
    if method == "POST":
        return any(item in lowered for item in NOT_SENT)
    return any(item in lowered for item in (*NOT_SENT, *LOST))


def fetch(method: str, url: str, **kwargs) -> lib_http.Reply:
    """`lib_http.request` for the scenarios: a lost network is a harness error, other statuses assertions."""
    try:
        return lib_http.request(method, url, **kwargs)
    except lib_http.HttpError as error:
        if error.status is None:
            raise HarnessNetworkError(f"{method} {url}: unreachable") from None
        raise AssertionError(f"{method} {url}: {error}") from None


class GitHub:
    """Use gh authentication or one explicit role token without printing secrets."""

    def __init__(
        self,
        token: str = "",
        runner: Callable = subprocess.run,
        sleep: Callable = time.sleep,
    ) -> None:
        """Keep role credentials in the subprocess environment only; the rest is for tests."""
        self.runner = runner
        self.sleep = sleep
        self.environment = dict(os.environ)
        if token:
            self.environment["GH_TOKEN"] = token

    def request(self, method: str, path: str, data: dict | None = None):
        """
        Execute a bounded REST request and decode its JSON response.

        Errors expose endpoint and status text, never credential values.
        """
        command = ["gh", "api", "--method", method, path]
        if data is not None:
            command += ["--input", "-"]
        for delay in (*lib_http.RETRY_DELAYS, None):
            limit = DEADLINE.get()
            budget = 60 if limit is None else max(1, min(60, limit - time.monotonic()))
            try:
                result = self.runner(
                    command,
                    input=json.dumps(data) if data is not None else None,
                    check=False,
                    text=True,
                    capture_output=True,
                    env=self.environment,
                    timeout=budget,
                )
                failure = result.stderr.strip()
            except subprocess.TimeoutExpired:
                result, failure = None, "timeout"
            if result is not None and not result.returncode:
                break
            pause = (delay or 0) * random.uniform(0.8, 1.2)
            out_of_time = limit is not None and time.monotonic() + pause >= limit
            if delay is None or out_of_time or not transient(failure, method):
                error = HarnessNetworkError if is_network(failure) else RuntimeError
                raise error(f"GitHub {method} {path}: {failure}")
            self.sleep(pause)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def delete_ref(self, repo: str, ref: str) -> None:
        """
        Delete a branch; one that is already gone counts as deleted.

        Any other failure (403, 5xx after the retries) is raised.
        """
        try:
            self.request(
                "DELETE", f"repos/{repo}/git/refs/heads/{quote(ref, safe='/')}"
            )
        except RuntimeError as error:
            if not ref_gone(str(error)):
                raise

    def refs_with_prefix(self, repo: str, prefix: str) -> list[str]:
        """Branch names (without `refs/heads/`) that start with `prefix`; every page is read."""
        found = self.collection(
            f"repos/{repo}/git/matching-refs/heads/{quote(prefix, safe='/')}"
        )
        return [item["ref"].removeprefix("refs/heads/") for item in found]

    def collection(self, path: str, key: str | None = None) -> list[dict]:
        """
        Read every bounded page of an API collection instead of silently truncating it.

        A malformed page fails the scenario with the endpoint involved.
        """
        items = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            value = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            batch = value[key] if key else value
            if not isinstance(batch, list):
                raise TypeError(f"Invalid GitHub collection: {path}")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise RuntimeError(f"Too many pages: {path}")


def wait_for(
    read: Callable,
    accept: Callable,
    description: str,
    timeout: float,
    interval: float = 5,
    clock: Callable = time.monotonic,
    sleep: Callable = time.sleep,
):
    """
    Wait for a specific externally observable state until the bounded deadline.

    The failure names the awaited evidence and configured duration.
    """
    deadline = clock() + timeout
    token = DEADLINE.set(deadline if clock is time.monotonic else None)
    try:
        while True:
            value = read()
            if accept(value):
                return value
            if clock() >= deadline:
                raise AssertionError(
                    f"Not found: {description} within {timeout:g} seconds"
                )
            sleep(min(interval, max(0, deadline - clock())))
    finally:
        DEADLINE.reset(token)
