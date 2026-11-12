"""Select the approved pull request and trusted artifacts for a merge."""

from collections.abc import Collection, Mapping, Sequence


def select_merged_pr(
    pulls: Sequence[Mapping[str, object]], merge_sha: str
) -> Mapping[str, object]:
    """
    Identify exactly one merged PR whose merge commit matches the push.

    Direct pushes or ambiguous results are never eligible for publication.
    """
    matches = [
        item
        for item in pulls
        if item.get("merge_commit_sha") == merge_sha
        and item.get("merged_at")
        and item.get("state") == "closed"
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one merged pull request")
    return matches[0]


def select_approver(
    reviews: Sequence[Mapping[str, object]], head_sha: str, author: str
) -> str:
    """
    Select a human reviewer whose latest review approves the exact head commit.

    A reviewer's later dismissal or change request supersedes their earlier approval.
    """
    latest = {}
    for review in reviews:
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        kind = user.get("type") if isinstance(user, dict) else None
        timestamp = review.get("submitted_at")
        state = review.get("state")
        if (
            not isinstance(login, str)
            or kind != "User"
            or login.lower() == author.lower()
            or not isinstance(timestamp, str)
            or not isinstance(state, str)
        ):
            continue
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        previous = latest.get(login.lower())
        if previous is None or timestamp > previous["submitted_at"]:
            latest[login.lower()] = review
    if any(review.get("state") == "CHANGES_REQUESTED" for review in latest.values()):
        raise ValueError(
            "A reviewer requested changes; resolve and obtain a new approval"
        )
    for review in latest.values():
        if review.get("state") == "APPROVED" and review.get("commit_id") == head_sha:
            return review["user"]["login"]
    raise ValueError("No current approval from another human reviewer")


def build_artifact_name(names: Collection[object], head_sha: str) -> str | None:
    """
    Name of the build artifact of a run: `build-<sha>`, or the old `bundle-<sha>`.

    TODO(2026-11-20): runs built before the rename are gone by then; drop the old name.
    """
    for name in (f"build-{head_sha}", f"bundle-{head_sha}"):
        if name in names:
            return name
    return None


def select_artifact_run(runs: Sequence[Mapping[str, object]], head_sha: str) -> int:
    """
    Select the newest successful pipeline run containing both expected artifacts.

    The caller must supply artifact lists read from the matching trusted workflow runs.
    """
    candidates = []
    for run in runs:
        artifacts = run.get("artifacts")
        if (
            run.get("event") not in {"workflow_run", "workflow_dispatch"}
            or (
                run.get("event") == "workflow_dispatch"
                and run.get("head_branch") != "main"
            )
            or run.get("conclusion") != "success"
            or type(run.get("id")) is not int
            or not isinstance(artifacts, list)
        ):
            continue
        names = {
            item.get("name")
            for item in artifacts
            if isinstance(item, dict) and item.get("expired") is False
        }
        if build_artifact_name(names, head_sha) is not None and (
            f"results-{head_sha}" in names
        ):
            number = run.get("run_number")
            attempt = run.get("run_attempt")
            if type(number) is not int or type(attempt) is not int:
                raise ValueError("Missing pipeline run ordering")
            candidates.append((number, attempt, run["id"]))
    if not candidates:
        raise ValueError("No successful run with approved artifacts")
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][:2] == candidates[1][:2]:
        raise ValueError("Ambiguous pipeline run ordering")
    return candidates[0][2]
