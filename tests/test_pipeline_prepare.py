"""Tests for preparing pull request data in a trusted job."""

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.pipeline_prepare import PullSkip, prepare

VALID = (
    Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
).read_bytes()
SHA = "a" * 40
STATUS = {"A": "added", "M": "modified", "D": "removed"}
ATTESTED = "- [x] I'm the author of the app or have write access to its repository"


def api(files, content=VALID, body=ATTESTED, closed=False):
    """Fake GitHub API answering the pull list, the PR, its files and the manifest."""
    gateway = Mock()
    pulls = (
        []
        if closed
        else [
            {
                "number": 7,
                "head": {"sha": SHA},
                "user": {"login": "author", "type": "User"},
                "body": body,
            }
        ]
    )
    answers = [pulls]
    if not closed:
        answers += [
            {"labels": []},
            [{"filename": name, "status": STATUS[status]} for status, name in files],
        ]
        if content is not None:
            answers.append(
                {"encoding": "base64", "content": base64.b64encode(content).decode()}
            )
    gateway._request.side_effect = answers
    return gateway


def early(tmp_path, files, **kwargs):
    """Run prepare and return its outputs and the findings written for the report."""
    import json

    results = tmp_path / "results.json"
    result = prepare(api(files, **kwargs), SHA, tmp_path / "prepared", results=results)
    return result, json.loads(results.read_text()) if results.exists() else None


@pytest.mark.parametrize(
    ("files", "check_id", "detail"),
    [
        (
            [("A", "applications/app.busy.clock/manifest.yml"), ("A", "tools/x.py")],
            "pr_scope",
            "A tools/x.py",
        ),
        (
            [
                ("A", "applications/app.busy.a/manifest.yml"),
                ("A", "applications/app.busy.b/manifest.yml"),
            ],
            "pr_scope",
            "applications/app.busy.b/manifest.yml",
        ),
        (
            [
                ("D", "applications/app.busy.clock/manifest.yml"),
                ("D", "tools/x.py"),
            ],
            "pr_scope",
            "D applications/app.busy.clock/manifest.yml",
        ),
        (
            [
                ("D", "applications/app.busy.clock/manifest.yml"),
                ("A", "applications/app.busy.clock/extra.txt"),
            ],
            "pr_scope",
            "A applications/app.busy.clock/extra.txt",
        ),
        (
            [("A", "applications/App.Busy.X/manifest.yml")],
            "app_id_format",
            "use lower case",
        ),
    ],
)
def test_scope_problems_end_in_results_not_exceptions(
    tmp_path, files, check_id, detail
):
    """Author scope mistakes become one required finding and skip the build."""
    result, data = early(tmp_path, files, content=None)
    assert result["early"] == "true"
    blocking = [item for item in data["checks"] if item["id"] == check_id]
    assert len(blocking) == 1 and detail in blocking[0]["detail"]
    assert data["skip_reason"] == "manifest"
    assert not (tmp_path / "prepared").exists()


def test_bad_manifest_skips_build_with_manifest_finding(tmp_path):
    """An unreadable manifest is reported as the manifest check, not as an exception."""
    result, data = early(
        tmp_path,
        [("A", "applications/app.busy.clock/manifest.yml")],
        content=b"a: [unclosed",
    )
    assert result["early"] == "true"
    assert [item["id"] for item in data["checks"]] == ["manifest_readable"]


def test_oversized_manifest_is_a_finding(tmp_path):
    """A manifest over the limit is the author's error."""
    _result, data = early(
        tmp_path,
        [("A", "applications/app.busy.clock/manifest.yml")],
        content=b"x" * (64 * 1024 + 1),
    )
    assert data["checks"][0]["id"] == "manifest_schema"
    assert (
        "larger" in data["checks"][0]["detail"]
        or "limit" in data["checks"][0]["detail"]
    )


def test_missing_attestation_does_not_skip_build(tmp_path):
    """An unchecked confirmation is reported at the end and does not stop the build."""
    result, data = early(
        tmp_path, [("A", "applications/app.busy.clock/manifest.yml")], body=""
    )
    assert result["early"] == "false" and data is None


def test_api_failure_becomes_internal_error(tmp_path):
    """A GitHub failure after the PR is known ends in `internal_error` with the run link."""
    import json

    gateway = api([("A", "applications/app.busy.clock/manifest.yml")])
    gateway._request.side_effect = [
        gateway._request.side_effect.__next__(),
        {"labels": []},
        RuntimeError("GitHub API returned HTTP 502"),
    ]
    results = tmp_path / "results.json"
    result = prepare(
        gateway, SHA, tmp_path / "prepared", run_url="https://x/run/1", results=results
    )
    data = json.loads(results.read_text())
    assert result["early"] == "true"
    assert data["checks"][0] == {
        "id": "internal_error",
        "result": "fail",
        "detail": "https://x/run/1",
    }
    assert data["skip_reason"] == "internal"
    assert "Traceback" not in results.read_text()


def test_closed_pull_request_is_skipped_quietly(tmp_path):
    """No open PR for the commit: nothing to comment on."""
    with pytest.raises(PullSkip):
        prepare(api([], closed=True), SHA, tmp_path / "prepared")


def test_prepare_only_manifest_data(tmp_path: Path) -> None:
    """
    Copy one PR manifest as data after matching its exact head SHA.

    Fake API responses prevent GitHub access or author code execution.
    """
    sha = "a" * 40
    gateway = Mock()
    gateway._request.side_effect = [
        [
            {
                "number": 7,
                "head": {"sha": sha},
                "user": {"login": "author", "type": "User"},
                "body": "- [x] I'm the author of the app or have write access to its repository",
            }
        ],
        {"labels": []},
        [{"filename": "applications/app.busy.clock/manifest.yml", "status": "added"}],
        {
            "encoding": "base64",
            "content": base64.b64encode(VALID).decode(),
        },
    ]
    result = prepare(gateway, sha, tmp_path / "prepared")
    assert result == {
        "skip": "false",
        "early": "false",
        "pr_number": 7,
        "head_sha": sha,
        "app_id": "app.busy.clock",
        "removal": "",
        "author": "author",
        "author_attested": "true",
    }
    assert (tmp_path / "prepared" / "manifest.yml").read_bytes() == VALID


def test_prepare_rejects_more_than_one_pr(tmp_path: Path) -> None:
    """
    Reject ambiguous pull requests targeting the same head commit.

    No manifest is written for an ambiguous result.
    """
    sha = "a" * 40
    gateway = Mock()
    gateway._request.return_value = [
        {"number": 1, "head": {"sha": sha}},
        {"number": 2, "head": {"sha": sha}},
    ]
    with pytest.raises(PullSkip, match="one open"):
        prepare(gateway, sha, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_prepare_reports_unchecked_author_attestation(tmp_path: Path) -> None:
    """Keep an unchecked ownership declaration visible to the trusted check job."""
    sha = "a" * 40
    gateway = Mock()
    gateway._request.side_effect = [
        [
            {
                "number": 7,
                "head": {"sha": sha},
                "user": {"login": "author", "type": "User"},
                "body": "- [ ] I'm the author of the app or have write access to its repository",
            }
        ],
        {"labels": []},
        [{"filename": "applications/app.busy.clock/manifest.yml", "status": "added"}],
        {
            "encoding": "base64",
            "content": base64.b64encode(b"category: clocks").decode(),
        },
    ]

    result = prepare(gateway, sha, tmp_path / "prepared")

    assert result["author_attested"] == "false"


def test_maintenance_prepare_skips_build(tmp_path: Path) -> None:
    """
    Read current labels from the API and bypass application preparation for maintenance.

    The workflow receives a skip flag without creating any build inputs.
    """
    gateway = Mock()
    sha = "a" * 40
    gateway._request.side_effect = [
        [{"number": 7, "head": {"sha": sha}}],
        {"labels": [{"name": "maintenance"}]},
    ]
    assert prepare(gateway, sha, tmp_path / "prepared")["skip"] == "true"
    assert not (tmp_path / "prepared").exists()
