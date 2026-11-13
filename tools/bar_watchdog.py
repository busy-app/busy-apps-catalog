"""Cancel the pipeline run if the real-bar job waits in the queue for too long."""

import os
import sys
import time
from collections.abc import Callable, Mapping

from tools.lib_http import HttpError, request

# The one place for the queue limit. The job's own run limit is `timeout-minutes` in pipeline.yml.
BAR_QUEUE_TIMEOUT = 30 * 60
POLL_SECONDS = 30
BAR_JOB = "bar"
WAITING = {"queued", "waiting", "pending", "requested"}
API = "https://api.github.com"


def bar_waiting(jobs: list[Mapping[str, object]]) -> bool:
    """Whether the `bar` job has not started: still queued, or not created yet."""
    for job in jobs:
        if job.get("name") == BAR_JOB:
            return job.get("status") in WAITING
    return True


def watch(
    read_jobs: Callable[[], list[Mapping[str, object]]],
    cancel: Callable[[], None],
    *,
    timeout: float = BAR_QUEUE_TIMEOUT,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """
    Poll the jobs of this run; return "started" once `bar` left the queue, else cancel the run.

    A failed read keeps the last known state, so an API hiccup neither cancels a bar that is
    running nor lets a stuck queue go on for a day. Before cancelling, the state is read once
    more: a bar that has just started is left alone.
    """
    deadline = clock() + timeout
    waiting = True
    while True:
        try:
            waiting = bar_waiting(read_jobs())
        except HttpError:
            pass
        if not waiting:
            return "started"
        if clock() >= deadline:
            break
        sleep(min(poll, max(0.0, deadline - clock())))
    try:
        if not bar_waiting(read_jobs()):
            return "started"
    except HttpError:
        pass
    cancel()
    return "cancelled"


def github(method: str, path: str, token: str, **options):
    """One GitHub API call with the job token; the token never reaches a message."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return request(method, f"{API}{path}", headers=headers, timeout=20, **options)


def main() -> int:
    """Watch the run named by the Actions environment; exit 0 whether it started or was cancelled."""
    token = os.environ["GITHUB_TOKEN"]
    repo, run, attempt = (
        os.environ[key] for key in ("GITHUB_REPOSITORY", "RUN_ID", "RUN_ATTEMPT")
    )
    base = f"/repos/{repo}/actions/runs/{run}"

    def read_jobs() -> list[Mapping[str, object]]:
        import json

        reply = github("GET", f"{base}/attempts/{attempt}/jobs?per_page=100", token)
        return json.loads(reply.content).get("jobs", [])

    def cancel() -> None:
        # 409: the run already ended. Cancelling is the same either way, so no repeat is needed.
        github("POST", f"{base}/cancel", token, accept={202, 409})

    outcome = watch(read_jobs, cancel)
    print(f"BAR job: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
