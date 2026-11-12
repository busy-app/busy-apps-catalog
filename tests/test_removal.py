"""Removing an app through a PR: scope, rights, the PR comment and the pipeline outputs."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tools.lib_comment import render_comment
from tools.lib_http import HttpError
from tools.lib_removal import published_app, removal_findings
from tools.pipeline_prepare import prepare
from tools.pipeline_report import read_extra
from tools.validate_scope import REMOVAL_PREFIX, removed_folder, select_manifest

SHA = "a" * 40
ID = "app.busy.clock"
MANIFEST = f"applications/{ID}/manifest.yml"
CARD = {"author": "Author", "name": "Clock", "current_version": {"version": "1.2.0"}}
ATTESTED = "- [x] I'm the author of the app or have write access to its repository"


@pytest.mark.parametrize(
    ("changes", "exists", "expected"),
    [
        ([("D", MANIFEST)], False, ID),
        ([("D", MANIFEST), ("D", f"applications/{ID}/icon.png")], False, ID),
        ([("D", MANIFEST)], True, None),
        ([("D", MANIFEST), ("A", f"applications/{ID}/icon.png")], False, None),
        ([("D", MANIFEST), ("D", "tools/x.py")], False, None),
        (
            [("D", MANIFEST), ("D", "applications/app.busy.other/manifest.yml")],
            False,
            None,
        ),
        ([("M", MANIFEST)], False, None),
        ([], False, None),
    ],
)
def test_only_a_whole_folder_deletion_is_a_removal(changes, exists, expected) -> None:
    """All changes are deletions in one application folder and the folder is gone."""
    assert removed_folder(changes, lambda folder: exists) == expected


def git(repo: Path, *args: str) -> str:
    """Run git in the test repository."""
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_validate_scope_sees_a_removal_in_git(tmp_path: Path) -> None:
    """The unprivileged job gets `removal:<id>` for a deleted folder and pr_scope otherwise."""
    from tools.check_registry import CheckError

    git(tmp_path, "init", "-q", "-b", "main")
    folder = tmp_path / "applications" / ID
    folder.mkdir(parents=True)
    (folder / "manifest.yml").write_text("a: 1\n")
    (folder / "notes.txt").write_text("n\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "partial")
    (folder / "manifest.yml").unlink()
    git(tmp_path, "commit", "-qam", "partial")
    partial = git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(CheckError) as error:
        select_manifest(tmp_path, base, partial)
    assert error.value.id == "pr_scope"
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "checkout", "-qb", "whole")
    git(tmp_path, "rm", "-rq", f"applications/{ID}")
    git(tmp_path, "commit", "-qm", "whole")
    whole = git(tmp_path, "rev-parse", "HEAD")
    assert select_manifest(tmp_path, base, whole) == REMOVAL_PREFIX + ID


def gateway_for(files, permission="read", body=ATTESTED, login="author", gone=True):
    """A fake GitHub API for a removal PR."""
    gateway = Mock()
    answers = [
        [
            {
                "number": 7,
                "head": {"sha": SHA},
                "user": {"login": login, "type": "User"},
                "body": body,
            }
        ],
        {"labels": []},
        [{"filename": name, "status": "removed"} for name in files],
    ]
    if gone:
        answers.append(RuntimeError("GitHub API returned HTTP 404"))
    gateway._request.side_effect = answers
    gateway.collaborator_permission.return_value = permission
    return gateway


def run_prepare(tmp_path: Path, gateway, card=CARD, login_catalog="https://api.test"):
    """Prepare a removal PR with the catalog answering `card` (None = unknown app)."""
    results = tmp_path / "results.json"
    with patch("tools.lib_removal.published_app", return_value=card):
        out = prepare(
            gateway,
            SHA,
            tmp_path / "prepared",
            results=results,
            catalog_url=login_catalog,
        )
    return out, json.loads(results.read_text())


def ids(data) -> dict[str, str]:
    """Finding id → result."""
    return {item["id"]: item["result"] for item in data["checks"]}


def test_the_author_may_remove_in_any_case(tmp_path: Path) -> None:
    """`Author` (the catalog's spelling) equals the PR's `author`; nothing is red."""
    out, data = run_prepare(tmp_path, gateway_for([MANIFEST]))
    assert out["early"] == "true" and out["removal"] == ID
    assert ids(data) == {} and data["status"] == "pass"
    assert data["skip_reason"] == "removal" and data["manifest_path"] == ""
    assert data["removal"] == {
        "id": ID,
        "name": "Clock",
        "version": "1.2.0",
        "author": "Author",
        "published": True,
    }
    assert not (tmp_path / "prepared").exists()


def test_a_maintainer_may_remove_someone_elses_app(tmp_path: Path) -> None:
    """Write access to the catalog is enough."""
    _, data = run_prepare(tmp_path, gateway_for([MANIFEST], "write", login="someone"))
    assert ids(data) == {}


def test_a_stranger_is_refused(tmp_path: Path) -> None:
    """Neither the author nor a writer: `removal_not_owner`, and it blocks."""
    out, data = run_prepare(tmp_path, gateway_for([MANIFEST], "read", login="stranger"))
    assert ids(data) == {"removal_not_owner": "fail"} and data["status"] == "fail"
    assert out["early"] == "true"


def test_an_unpublished_app_is_only_a_warning(tmp_path: Path) -> None:
    """Merging just cleans the repository: the check is red-orange, not blocking."""
    _, data = run_prepare(tmp_path, gateway_for([MANIFEST]), card=None)
    assert ids(data) == {"removal_not_published": "fail"} and data["status"] == "pass"
    assert data["removal"]["published"] is False


def test_an_unreachable_catalog_asks_a_maintainer_nothing_but_blocks_others() -> None:
    """Rights cannot be checked without the app's author: a writer passes, others cannot run."""
    gateway = Mock()
    with patch("tools.lib_removal.published_app", side_effect=HttpError(503)):
        gateway.collaborator_permission.return_value = "write"
        assert removal_findings(gateway, "m", ID, "https://api.test")[0] == []
        gateway.collaborator_permission.return_value = "read"
        findings, _ = removal_findings(gateway, "x", ID, "https://api.test")
    assert (
        findings[0]["id"] == "removal_not_owner" and findings[0]["result"] == "not_run"
    )


def test_a_folder_that_remains_is_not_a_removal(tmp_path: Path) -> None:
    """Deleting only some files keeps the folder in the head: `pr_scope`."""
    gateway = gateway_for([MANIFEST], gone=False)
    gateway._request.side_effect = [*gateway._request.side_effect, {"type": "dir"}]
    results = tmp_path / "results.json"
    out = prepare(gateway, SHA, tmp_path / "prepared", results=results)
    data = json.loads(results.read_text())
    assert out["removal"] == "" and "pr_scope" in ids(data)


def test_a_removal_runs_no_code_and_downloads_nothing(tmp_path: Path) -> None:
    """The manifest is never fetched for a removal."""
    gateway = gateway_for([MANIFEST])
    run_prepare(tmp_path, gateway)
    paths = [call.args[1] for call in gateway._request.call_args_list]
    assert not any(
        path.startswith("contents/applications/") and "manifest.yml" in path
        for path in paths
    )


def test_published_app_asks_the_public_card(monkeypatch) -> None:
    """The card is read from the catalog's public API; 404 is None, other errors raise."""
    seen = []

    def fake(method, url, **kwargs):
        seen.append(url)
        return Mock(content=json.dumps(CARD).encode())

    monkeypatch.setattr("tools.lib_removal.request", fake)
    assert published_app("https://api.test/", ID) == CARD
    assert seen == [f"https://api.test/api/catalog/v1/applications/{ID}"]
    monkeypatch.setattr("tools.lib_removal.request", Mock(side_effect=HttpError(404)))
    assert published_app("https://api.test", ID) is None
    monkeypatch.setattr("tools.lib_removal.request", Mock(side_effect=HttpError(500)))
    with pytest.raises(HttpError):
        published_app("https://api.test", ID)
    assert (
        published_app("", ID) is None
        and published_app("https://api.test", "Bad Id") is None
    )


REMOVAL = {
    "id": ID,
    "name": "Clock",
    "version": "1.2.0",
    "author": "Author",
    "published": True,
}


def test_the_removal_comment_is_short_and_says_what_happens() -> None:
    """What goes, what it does, who may; no package, no build rows."""
    body = render_comment(
        [],
        skipped=["build", "package", "catalog"],
        skip_reason="removal",
        removal=REMOVAL,
    )
    assert (
        "### Removal" in body
        and "**Clock** (`app.busy.clock`), version 1.2.0, by Author" in body
    )
    assert "disappears from the catalog and from search" in body
    assert "versions can no longer be downloaded" in body
    assert (
        "installed on bars stay" in body and "author or the catalog maintainers" in body
    )
    assert "### Package" not in body and "passed" not in body.split("### Removal")[
        0
    ].replace("Passed", "")
    assert body.startswith("<!-- catalog-checks -->") and "✅ Passed" in body


def test_the_removal_comment_for_an_unpublished_app_and_with_a_problem() -> None:
    """Not in the catalog: only a clean-up note; a problem shows as a table row."""
    findings = [{"id": "removal_not_published", "result": "fail", "detail": ID}]
    body = render_comment(findings, removal=REMOVAL | {"published": False, "name": ""})
    assert "not in the catalog" in body and "⚠️" in body
    findings = [
        {"id": "removal_not_owner", "result": "fail", "detail": "x is not the author"}
    ]
    body = render_comment(findings, removal=REMOVAL)
    assert "❌ Action required" in body and "Who removes the app" in body


def test_the_removal_comment_neutralises_the_app_name() -> None:
    """The name comes from the catalog, not from the PR; it still cannot ping or inject."""
    body = render_comment([], removal=REMOVAL | {"name": "<b>@octocat`x`"})
    assert "<b>" not in body and "@octocat" not in body


def test_read_extra_carries_the_removal_and_rejects_nonsense(tmp_path: Path) -> None:
    """Trusted results keep only known string fields of the removal."""
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            {
                "skipped": ["build"],
                "skip_reason": "removal",
                "removal": REMOVAL | {"x": 1},
            }
        )
    )
    assert read_extra(path)["removal"] == REMOVAL
    path.write_text(json.dumps({"skipped": [], "removal": "oops"}))
    with pytest.raises(TypeError):
        read_extra(path)
    path.write_text(json.dumps({"skipped": []}))
    assert read_extra(path)["removal"] is None
