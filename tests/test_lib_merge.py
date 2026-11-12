"""Tests for identifying eligible approved publication inputs."""

import pytest

from tools.lib_merge import select_approver, select_artifact_run, select_merged_pr


def test_merge_and_current_approval() -> None:
    """
    Require the exact merge and a current approval by another human.

    Later change requests invalidate an earlier approval from that reviewer.
    """
    sha, head = "a" * 40, "b" * 40
    assert (
        select_merged_pr(
            [{"merge_commit_sha": sha, "merged_at": "now", "state": "closed"}], sha
        )["state"]
        == "closed"
    )
    approve = {
        "user": {"login": "reviewer", "type": "User"},
        "state": "APPROVED",
        "commit_id": head,
        "submitted_at": "2026-01-01T00:00:00Z",
    }
    assert select_approver([approve], head, "author") == "reviewer"
    reject = {
        "user": {"login": "second-reviewer", "type": "User"},
        "state": "CHANGES_REQUESTED",
        "commit_id": head,
        "submitted_at": "2026-01-02T00:00:00Z",
    }
    with pytest.raises(ValueError, match="requested changes"):
        select_approver([approve, reject], head, "author")
    later = {
        **approve,
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-01-02T00:00:00Z",
    }
    with pytest.raises(ValueError):
        select_approver([approve, later], head, "author")
    with pytest.raises(ValueError):
        select_approver([approve], "c" * 40, "author")


def test_match_run_and_both_artifacts() -> None:
    """
    Select only a successful trusted workflow run with both live artifacts.

    Missing, expired or ambiguous runs must fail closed.
    """
    head = "b" * 40
    run = {
        "id": 42,
        "run_number": 5,
        "run_attempt": 1,
        "event": "workflow_run",
        "conclusion": "success",
        "artifacts": [
            {"name": f"build-{head}", "expired": False},
            {"name": f"results-{head}", "expired": False},
        ],
    }
    assert select_artifact_run([run], head) == 42
    newer = {**run, "id": 43, "run_number": 6}
    assert select_artifact_run([run, newer], head) == 43
    with pytest.raises(ValueError):
        select_artifact_run([run, run], head)


def test_dispatch_artifact_requires_default_branch():
    """
    Accept manual rebuild artifacts only from the trusted main workflow revision.

    A manually dispatched feature-branch workflow cannot supply publish artifacts.
    """
    head = "b" * 40
    run = {
        "id": 42,
        "run_number": 1,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "conclusion": "success",
        "artifacts": [
            {"name": f"build-{head}", "expired": False},
            {"name": f"results-{head}", "expired": False},
        ],
    }
    assert select_artifact_run([run], head) == 42
    with pytest.raises(ValueError):
        select_artifact_run([{**run, "head_branch": "untrusted"}], head)


def test_old_bundle_artifact_name_is_still_found() -> None:
    """Runs built before the rename (`bundle-<sha>`) can still be published."""
    from tools.lib_merge import build_artifact_name

    head = "a" * 40
    assert (
        build_artifact_name({f"build-{head}", f"bundle-{head}"}, head)
        == f"build-{head}"
    )
    assert build_artifact_name({f"bundle-{head}"}, head) == f"bundle-{head}"
    assert build_artifact_name({f"results-{head}"}, head) is None
    runs = [
        {
            "id": 5,
            "event": "workflow_run",
            "conclusion": "success",
            "run_number": 1,
            "run_attempt": 1,
            "artifacts": [
                {"name": f"bundle-{head}", "expired": False},
                {"name": f"results-{head}", "expired": False},
            ],
        }
    ]
    assert select_artifact_run(runs, head) == 5
