"""The queue watchdog: logic over a fake API, and its calls over a mock transport."""

import json

import httpx2
import pytest

from tools import bar_watchdog
from tools.bar_watchdog import BAR_QUEUE_TIMEOUT, bar_waiting, watch
from tools.lib_http import HttpError


class Clock:
    """A clock that only moves when the watchdog sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def jobs(status: str | None) -> list[dict]:
    """The job list of a run; the bar has `status` (None: not created yet)."""
    rows = [
        {"name": "prepare", "status": "completed"},
        {"name": "check", "status": "completed"},
    ]
    if status is not None:
        rows.append({"name": "bar", "status": status})
    return rows


def run_watch(reads, **kw):
    """Run `watch` with scripted reads; returns (outcome, cancels, reads made)."""
    clock, cancels, made = Clock(), [], []
    script = iter(reads)

    def read():
        made.append(clock.now)
        item = next(script, reads[-1])
        if isinstance(item, Exception):
            raise item
        return item

    outcome = watch(
        read, lambda: cancels.append(clock.now), clock=clock, sleep=clock.sleep, **kw
    )
    return outcome, cancels, made


def test_the_queue_limit_is_thirty_minutes() -> None:
    assert BAR_QUEUE_TIMEOUT == 30 * 60


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_waiting_states(status) -> None:
    assert bar_waiting(jobs(status))


@pytest.mark.parametrize("status", ["in_progress", "completed"])
def test_started_states(status) -> None:
    assert not bar_waiting(jobs(status))


def test_a_job_that_does_not_exist_yet_counts_as_waiting() -> None:
    assert bar_waiting(jobs(None)) and bar_waiting([])


def test_the_bar_leaving_the_queue_ends_the_watch_without_cancel() -> None:
    outcome, cancels, made = run_watch(
        [jobs("queued"), jobs("queued"), jobs("in_progress")]
    )
    assert (outcome, cancels) == ("started", [])
    assert made == [0, 30, 60]  # polls every 30 s, stops at once


def test_a_bar_that_stays_queued_is_cancelled_after_the_limit() -> None:
    outcome, cancels, made = run_watch([jobs("queued")])
    assert outcome == "cancelled" and len(cancels) == 1
    assert cancels[0] >= BAR_QUEUE_TIMEOUT and made[0] == 0
    assert max(b - a for a, b in zip(made, made[1:])) <= 30


def test_a_bar_that_started_at_the_last_moment_is_not_cancelled() -> None:
    reads = [jobs("queued")] * 61 + [
        jobs("in_progress")
    ]  # 60 polls to the deadline, then the final read
    outcome, cancels, _ = run_watch(reads)
    assert (outcome, cancels) == ("started", [])


def test_a_read_error_keeps_the_last_state() -> None:
    error = HttpError(502)
    outcome, cancels, _ = run_watch([jobs("queued"), error])
    assert outcome == "cancelled" and len(cancels) == 1  # still queued as far as known
    outcome, cancels, _ = run_watch([jobs("in_progress"), error])
    assert (outcome, cancels) == ("started", [])


def test_a_custom_limit() -> None:
    outcome, cancels, _ = run_watch([jobs("queued")], timeout=90, poll=30)
    assert outcome == "cancelled" and cancels == [90]


def test_main_calls_the_api_with_the_job_token_and_cancels(
    monkeypatch, mock_http
) -> None:
    from tools import lib_http

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_token_zq")
    monkeypatch.setenv("GITHUB_REPOSITORY", "busy-app/busy-apps-catalog-dev")
    monkeypatch.setenv("RUN_ID", "555")
    monkeypatch.setenv("RUN_ATTEMPT", "2")
    monkeypatch.setattr(bar_watchdog, "BAR_QUEUE_TIMEOUT", 0)
    monkeypatch.setattr(bar_watchdog.time, "sleep", lambda seconds: None)
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        if request.method == "GET":
            return httpx2.Response(200, json={"jobs": jobs("queued")})
        return httpx2.Response(409)  # the run already ended: not an error

    mock_http(handler)
    lib_http.reset()
    original = bar_watchdog.watch
    monkeypatch.setattr(bar_watchdog, "watch", lambda r, c: original(r, c, timeout=0))
    assert bar_watchdog.main() == 0
    paths = [(method, path) for method, path, _ in seen]
    assert (
        "GET",
        "/repos/busy-app/busy-apps-catalog-dev/actions/runs/555/attempts/2/jobs",
    ) in paths
    assert (
        "POST",
        "/repos/busy-app/busy-apps-catalog-dev/actions/runs/555/cancel",
    ) in paths
    assert all(auth == "Bearer ghs_secret_token_zq" for _, _, auth in seen)


def test_a_failed_cancel_does_not_leak_the_token(monkeypatch, mock_http) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_token_zq")
    mock_http(lambda request: httpx2.Response(403, json={"message": "no"}))
    with pytest.raises(HttpError) as caught:
        bar_watchdog.github(
            "POST",
            "/repos/o/r/actions/runs/1/cancel",
            "ghs_secret_token_zq",
            accept={202, 409},
        )
    assert "ghs_secret_token_zq" not in str(
        caught.value
    ) and "ghs_secret_token_zq" not in repr(caught.value)
    assert json is not None
