"""Update the published confirmation comment after backend synchronization."""

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from tools.lib_app_id import is_valid
from tools.lib_comment import clean_field
from tools.lib_github import GitHubGateway
from tools.lib_publication_failure import MAX_DETAIL, read_failure

MARKER = "<!-- catalog-published -->"


def publish_confirmation(
    gateway: GitHubGateway,
    pr_number: int,
    result: dict,
    bot_login: str = "github-actions[bot]",
) -> None:
    """
    Post or update one bot-owned publication confirmation on the merged PR.

    Dry-run results never create a comment or claim a publication exists.
    """
    if result.get("dry_run") is True:
        return
    if result.get("removed") is True:
        post_confirmation(gateway, pr_number, removal_body(result), bot_login)
        return
    url = result.get("url")
    if not isinstance(url, str):
        raise TypeError("Published URL missing")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid published URL")
    body = f"{MARKER}\nPublished: {parsed.scheme}://{parsed.netloc}{quote(parsed.path, safe='/%-._~')}\n"
    post_confirmation(gateway, pr_number, body, bot_login)


def removal_body(result: dict) -> str:
    """The comment of a removal: the app id (checked) and whether it was still published."""
    app_id = result.get("app_id")
    if not isinstance(app_id, str) or not is_valid(app_id):
        raise TypeError("Removed application missing")
    suffix = " (it was already gone)" if result.get("already_gone") is True else ""
    return f"{MARKER}\nRemoved from the catalog: `{app_id}`{suffix}\n"


FAILED = "<!-- catalog-publication-failed -->"
SHA = re.compile(r"^[0-9a-f]{40}$")


def failure_body(
    kind: str, failure: dict[str, object], run_url: str, merge_sha: str
) -> str:
    """
    The comment when the publication or removal failed after the merge.

    A refusal is claimed only when the backend answered with a status; otherwise the
    run failed somewhere (before the catalog, a lost connection, after it) and the
    comment says only that nothing was confirmed. The reason is the backend's
    client-visible `detail`, made safe for a comment.
    """
    if kind not in {"publish", "remove"}:
        raise ValueError("Invalid publication kind")
    if not SHA.fullmatch(merge_sha):
        raise ValueError("Invalid merge commit")
    parsed = urlsplit(run_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or "/actions/runs/" not in parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid run URL")
    status, detail = failure.get("status"), failure.get("detail")
    reason = clean_field(detail, MAX_DETAIL) if isinstance(detail, str) else ""
    if type(status) is int:
        what = (
            "**Not published.** The pull request is merged, but the catalog refused this version"
            if kind == "publish"
            else "**Not removed.** The pull request is merged, but the catalog refused to remove the app"
        )
        what += f" (HTTP {status})" + (f": {reason}" if reason else ".")
    elif kind == "publish":
        what = (
            "**Publication not confirmed.** The publication run after the merge failed, "
            "so there is no confirmation that this version is in the catalog."
        )
    else:
        what = (
            "**Removal not confirmed.** The run after the merge failed, "
            "so there is no confirmation that the app was removed."
        )
    return (
        f"{MARKER}\n{FAILED}\n{what}\n\n"
        f"See the [publication run]({run_url}). The maintainers can run *Publish catalog "
        f"application* again for merge commit `{merge_sha}` once the cause is fixed on "
        "their side. If the app itself has to change, open a new pull request with a "
        "newer version.\n"
    )


def post_failure(
    gateway: GitHubGateway,
    pr_number: int,
    body: str,
    bot_login: str = "github-actions[bot]",
) -> bool:
    """
    Post the failure unless the PR already shows a successful publication or removal.

    A failed later run (a dispatch for the same merge, a retry) must not replace a
    correct confirmation; an earlier failure is replaced. Returns whether it posted.
    """
    for item in gateway.comments(pr_number):
        user = item.get("user")
        body_text = item.get("body")
        if (
            isinstance(user, dict)
            and user.get("login") == bot_login
            and isinstance(body_text, str)
            and MARKER in body_text
            and FAILED not in body_text
        ):
            return False
    post_confirmation(gateway, pr_number, body, bot_login)
    return True


def post_confirmation(
    gateway: GitHubGateway, pr_number: int, body: str, bot_login: str
) -> None:
    """Create the bot's confirmation comment, or update its one earlier comment."""
    previous = []
    for item in gateway.comments(pr_number):
        user = item.get("user")
        if (
            isinstance(user, dict)
            and user.get("login") == bot_login
            and isinstance(item.get("body"), str)
            and MARKER in item["body"]
            and type(item.get("id")) is int
        ):
            previous.append(item["id"])
    if len(previous) > 1:
        raise ValueError("Multiple publication comments found")
    if previous:
        gateway.update_comment(previous[0], body)
    else:
        gateway.create_comment(pr_number, body)


def main() -> int:
    """
    Publish a confirmation from a bounded backend response file.

    The GitHub token stays in the environment and is never printed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--result", type=Path, help="backend answer of a success")
    outcome.add_argument("--failed", choices=("publish", "remove"))
    parser.add_argument("--failure", type=Path, help="why it failed, if known")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--merge-sha", default="")
    args = parser.parse_args()
    if args.pr < 1:
        raise ValueError("Invalid pull request")
    gateway = GitHubGateway(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    )
    if args.failed:
        failure = read_failure(args.failure) if args.failure else {}
        body = failure_body(args.failed, failure, args.run_url, args.merge_sha)
        if not post_failure(gateway, args.pr, body):
            print("The PR already shows a confirmation; it is left as it is")
        return 0
    if args.result.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid publication result")
    result = json.loads(args.result.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise TypeError("Invalid publication result")
    publish_confirmation(gateway, args.pr, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
