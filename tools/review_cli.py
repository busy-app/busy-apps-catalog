"""Run an AI review: find the PR build, ask the backend, post one comment."""

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from tools.lib_github import GitHubGateway
from tools.lib_http import (
    ACCESS_REFUSED,
    AccessConfigError,
    AccessRefused,
    HttpError,
    request,
)
from tools.review_comment import clean, post_review, render_review, usage_line
from tools.review_request import ReviewInputError, build_request, discover

REVIEW_PATH = "/api/catalog/v1/ci/reviews"
BACKEND_TIMEOUT = 98
MAX_ANSWER = 4 * 1024 * 1024
FAILURES = {
    401: "the CI token was refused",
    403: "the CI token has no permission for reviews",
    404: "the backend has no review endpoint yet",
    413: "the request is too large",
    502: "the backend could not reach the model provider",
}


class ReviewError(Exception):
    """A review that could not be produced; the text goes to the workflow log."""


def ask_backend(api: str, token: str, body: Mapping[str, object]) -> tuple[int, dict]:
    """
    POST the request once and return the status with the parsed JSON answer.

    The request is not retried: a repeated call would cost model tokens twice.
    Errors name the status and the backend's own `detail`, never the token.
    """
    try:
        reply = request(
            "POST",
            api.rstrip("/") + REVIEW_PATH,
            headers={"Token": token},
            json=body,
            timeout=BACKEND_TIMEOUT,
            retry=False,
            max_bytes=MAX_ANSWER,
            error_body=MAX_ANSWER,
        )
        status, raw = reply.status, reply.content
    except AccessRefused:
        raise ReviewError(ACCESS_REFUSED) from None
    except AccessConfigError as error:
        raise ReviewError(str(error)) from None
    except HttpError as error:
        if error.status is None:
            raise ReviewError("the backend is unreachable") from None
        # An answer over the limit has no readable body; an error keeps its own.
        status, raw = error.status, error.body
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def answer_or_error(status: int, data: dict) -> tuple[dict, bool]:
    """
    The answer to render and whether it is partial.

    A 504 may carry what the steps managed to finish; any other failure is an error.
    """
    if status == 200:
        return data, False
    if status == 504 and (
        isinstance(data.get("steps"), dict) or isinstance(data.get("issues"), list)
    ):
        return data, True
    detail = data.get("detail")
    reason = FAILURES.get(status) or (
        clean(detail, 300) if isinstance(detail, str) else "the review failed"
    )
    raise ReviewError(f"HTTP {status}: {reason}")


def command_discover(args: argparse.Namespace) -> int:
    """Resolve the PR and export workflow outputs; no PR code is run."""
    gateway = GitHubGateway(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    )
    result = discover(gateway, args.pr, args.output)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
        file.writelines(f"{key}={value}\n" for key, value in result.items())
    print(json.dumps(result))
    return 0


def command_run(args: argparse.Namespace) -> int:
    """Build the request, ask the backend and update the PR's review comment."""
    gateway = GitHubGateway(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    )
    body = build_request(
        args.build_dir, args.results, args.manifest, args.pr, args.requested_by
    )
    if gateway.pull_head(args.pr) != args.head:
        raise ReviewError("the pull request head changed; run the review again")
    status, data = ask_backend(
        os.environ.get("CATALOG_API", ""), os.environ.get("CATALOG_CI_TOKEN", ""), body
    )
    answer, partial = answer_or_error(status, data)
    comment = render_review(answer, body["sourcecode"], args.requested_by)
    if partial and not isinstance(answer.get("issues"), list):
        comment += "\n_The review timed out; this is the partial result._\n"
    if isinstance(answer.get("models"), dict) or isinstance(answer.get("usage"), dict):
        print(
            f"models: {answer.get('models')} · tokens: {usage_line(answer.get('usage') or {})}"
        )
    post_review(gateway, args.pr, comment)
    print("AI review comment updated")
    return 0


def main() -> int:
    """Entry point of both workflow steps; a failure prints one clear line."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("discover")
    find.add_argument("--pr", type=int, required=True)
    find.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--pr", type=int, required=True)
    run.add_argument("--head", required=True)
    run.add_argument("--requested-by", required=True)
    run.add_argument("--build-dir", type=Path, required=True)
    run.add_argument("--results", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        return (
            command_discover(args) if args.command == "discover" else command_run(args)
        )
    except (ReviewError, ReviewInputError, ValueError, RuntimeError) as error:
        print(f"AI review failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
