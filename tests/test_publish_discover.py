"""Tests for selecting merged PR publication inputs."""

import base64
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tools.lib_github import GitHubGateway
from tools.publish_discover import discover, find_merged_pr

MANIFEST = (
    b"sourcecode:\n  location:\n    origin: https://github.com/imalkav/clock.git\n"
    b"    commit_sha: " + b"a" * 40 + b"\n"
)


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    """Record the retry pauses instead of sleeping."""
    pauses = []
    monkeypatch.setattr("tools.publish_discover.time.sleep", pauses.append)
    return pauses


def test_discover_approved_run(tmp_path: Path) -> None:
    """
    Resolve an approved merge and matching trusted run using fake API responses.

    The source manifest is saved as data without executing PR code.
    """
    merge, head = "a" * 40, "b" * 40
    gateway = Mock()
    gateway._request.side_effect = [
        [
            {
                "number": 7,
                "merge_commit_sha": merge,
                "merged_at": "now",
                "state": "closed",
                "head": {"sha": head},
                "user": {"login": "author"},
            }
        ],
        [{"filename": "applications/app.busy.clock/manifest.yml", "status": "added"}],
        [
            {
                "user": {"login": "reviewer", "type": "User"},
                "state": "APPROVED",
                "commit_id": head,
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ],
        {
            "encoding": "base64",
            "content": base64.b64encode(MANIFEST).decode(),
        },
        {
            "workflow_runs": [
                {
                    "id": 42,
                    "run_number": 3,
                    "run_attempt": 1,
                    "event": "workflow_run",
                    "conclusion": "success",
                }
            ]
        },
        {
            "artifacts": [
                {"name": f"build-{head}", "expired": False},
                {"name": f"results-{head}", "expired": False},
            ]
        },
    ]
    with patch.object(
        GitHubGateway, "owner_login", autospec=True, return_value="iMalkav"
    ) as owner:
        result = discover(gateway, merge, tmp_path / "approved")
    assert owner.call_args.args[0]._repository == "imalkav/clock"
    assert result["author"] == "iMalkav"
    assert result["run_id"] == 42 and result["approved_by"] == "reviewer"
    assert result["build_artifact"] == f"build-{head}"
    assert result["kind"] == "publish"
    assert (tmp_path / "approved" / "manifest.yml").read_bytes() == MANIFEST


def test_discover_rejects_direct_push(tmp_path: Path) -> None:
    """
    Reject pushes with no associated merged pull request.

    No manifest is written for an unapproved direct push.
    """
    gateway = Mock()
    gateway._request.return_value = []
    with pytest.raises(ValueError, match="merged pull"):
        discover(gateway, "a" * 40, tmp_path / "approved")
    assert not (tmp_path / "approved").exists()


def test_maintenance_merge_skips_publication(tmp_path: Path, capsys) -> None:
    """
    Skip non-application merges before looking up approvals or artifacts.

    No manifest directory or publication inputs are produced for maintenance changes.
    """
    gateway = Mock()
    sha = "a" * 40
    gateway._request.side_effect = [
        [
            {
                "number": 1,
                "merge_commit_sha": sha,
                "merged_at": "now",
                "state": "closed",
                "head": {"sha": "b" * 40},
                "user": {"login": "author"},
            }
        ],
        [{"filename": "tools/lib_build.py", "status": "modified"}],
    ]
    assert discover(gateway, sha, tmp_path / "approved") == {"skip": "true"}
    assert gateway._request.call_count == 2
    assert not (tmp_path / "approved").exists()
    assert "publication skipped" in capsys.readouterr().out


def merged(merge: str) -> dict:
    """A merged pull request as GitHub returns it."""
    return {
        "number": 7,
        "merge_commit_sha": merge,
        "merged_at": "now",
        "state": "closed",
    }


def test_pr_not_linked_yet_is_retried(no_pauses) -> None:
    """Right after the merge the commit has no PR: 0 results, then 1 gives the PR."""
    merge = "a" * 40
    gateway = Mock()
    gateway._request.side_effect = [[], [], [merged(merge)]]
    assert find_merged_pr(gateway, merge)["number"] == 7
    assert no_pauses == [2, 4]


def test_missing_pr_fails_after_all_pauses(no_pauses) -> None:
    """A commit that never gets a PR (direct push) fails after 2, 4, 8 and 16 s."""
    gateway = Mock()
    gateway._request.return_value = []
    with pytest.raises(ValueError, match="exactly one"):
        find_merged_pr(gateway, "a" * 40)
    assert no_pauses == [2, 4, 8, 16]


def test_two_prs_fail_at_once(no_pauses) -> None:
    """More than one candidate is not retried."""
    merge = "a" * 40
    gateway = Mock()
    gateway._request.return_value = [merged(merge), merged(merge)]
    with pytest.raises(ValueError, match="exactly one"):
        find_merged_pr(gateway, merge)
    assert no_pauses == []


def removal_gateway(statuses: list[tuple[str, str]], folder_gone: bool = True) -> Mock:
    """A merged removal PR: files, an approval and the folder check at the merge commit."""
    merge, head = "a" * 40, "b" * 40
    gateway = Mock()
    answers = [
        [
            {
                "number": 9,
                "merge_commit_sha": merge,
                "merged_at": "now",
                "state": "closed",
                "head": {"sha": head},
                "user": {"login": "author"},
            }
        ],
        [{"filename": name, "status": status} for status, name in statuses],
        [
            {
                "user": {"login": "reviewer", "type": "User"},
                "state": "APPROVED",
                "commit_id": head,
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ],
    ]
    if folder_gone:
        answers.append(RuntimeError("GitHub API returned HTTP 404"))
    gateway._request.side_effect = answers
    return gateway


def test_discover_recognises_a_removal(tmp_path: Path) -> None:
    """A deleted application folder is a `remove`: no manifest, no artifacts, no run lookup."""
    gateway = removal_gateway([("removed", "applications/app.busy.clock/manifest.yml")])
    result = discover(gateway, "a" * 40, tmp_path / "approved")
    assert result == {
        "skip": "false",
        "kind": "remove",
        "pr_number": 9,
        "head_sha": "b" * 40,
        "merge_sha": "a" * 40,
        "app_id": "app.busy.clock",
        "approved_by": "reviewer",
    }
    assert not (tmp_path / "approved").exists()
    paths = [call.args[1] for call in gateway._request.call_args_list]
    assert not any("actions/" in path for path in paths)


def test_a_removal_without_an_approval_is_refused(tmp_path: Path) -> None:
    """Approval is as required for removing as for publishing."""
    gateway = removal_gateway([("removed", "applications/app.busy.clock/manifest.yml")])
    answers = list(gateway._request.side_effect)
    answers[2] = []
    gateway._request.side_effect = answers
    with pytest.raises(ValueError, match="approval"):
        discover(gateway, "a" * 40, tmp_path / "approved")


def test_a_folder_that_remains_is_not_published_or_removed(tmp_path: Path) -> None:
    """Deleting a manifest but keeping the folder is not a removal and not a publication."""
    gateway = removal_gateway(
        [("removed", "applications/app.busy.clock/manifest.yml")], folder_gone=False
    )
    gateway._request.side_effect = [*gateway._request.side_effect, {"type": "dir"}]
    with pytest.raises(ValueError, match="one application manifest"):
        discover(gateway, "a" * 40, tmp_path / "approved")


@pytest.mark.parametrize(
    ("origin", "repository"),
    [
        (
            "https://github.com/iMalkav/busybar-world-clock.git",
            "iMalkav/busybar-world-clock",
        ),
        (
            "https://github.com/iMalkav/busybar-world-clock",
            "iMalkav/busybar-world-clock",
        ),
        ("https://github.com/a/b.github.io.git", "a/b.github.io"),
    ],
)
def test_source_repository_is_the_api_name(origin: str, repository: str) -> None:
    """`.git` from the template is dropped: `repos/<owner>/<repo>.git` is 404 in the API."""
    from tools.publish_discover import source_repository

    manifest = f"sourcecode:\n  location:\n    origin: {origin}\n".encode()
    assert source_repository(manifest) == repository


@pytest.mark.parametrize(
    "origin",
    [
        "https://gitlab.com/iMalkav/busybar-world-clock.git",
        "http://github.com/iMalkav/busybar-world-clock",
        "https://github.com/iMalkav/.git",
        "https://github.com/iMalkav/busybar-world-clock/tree/main",
    ],
)
def test_source_repository_refuses_other_origins(origin: str) -> None:
    """Only a GitHub repository address leads to an owner lookup, with a clear error."""
    from tools.publish_discover import source_repository

    manifest = f"sourcecode:\n  location:\n    origin: {origin}\n".encode()
    with pytest.raises(ValueError, match="Invalid source repository"):
        source_repository(manifest)
