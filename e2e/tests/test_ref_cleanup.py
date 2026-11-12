"""Branch cleanup: already-gone branches are fine, the sweep touches only this run."""

import json
import subprocess
from unittest.mock import Mock

import pytest

RUN = "20261111174253198166"
GONE_BODY = (
    '{"message":"Reference does not exist","documentation_url":'
    '"https://docs.github.com/rest/git/refs#delete-a-reference","status":"422"}'
)


def done(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def client(*answers):
    from e2e.infrastructure import GitHub

    runner = Mock(side_effect=list(answers))
    return GitHub(runner=runner, sleep=Mock()), runner


def test_ref_gone_classification() -> None:
    from e2e.domain import ref_gone

    base = "GitHub DELETE repos/o/r/git/refs/heads/e2e/x: gh: "
    assert ref_gone(base + "Reference does not exist (HTTP 422)")
    assert ref_gone(base + "Not Found (HTTP 404)")
    assert not ref_gone(base + "Forbidden (HTTP 403)")
    assert not ref_gone(base + "Bad Gateway (HTTP 502)")
    assert not ref_gone(base + "Reference already exists (HTTP 422)")
    assert not ref_gone("")


def test_delete_ref_422_does_not_exist_is_success() -> None:
    gh, runner = client(done(1, GONE_BODY, "gh: Reference does not exist (HTTP 422)"))
    gh.delete_ref("o/r", f"e2e/{RUN}/valid")
    args = runner.call_args.args[0]
    assert "DELETE" in args
    assert f"repos/o/r/git/refs/heads/e2e/{RUN}/valid" in args


def test_delete_ref_404_is_success() -> None:
    gh, _ = client(done(1, err="gh: Not Found (HTTP 404)"))
    gh.delete_ref("o/r", "e2e/x/valid")


def test_delete_ref_403_is_error() -> None:
    gh, _ = client(done(1, err="gh: Forbidden (HTTP 403)"))
    with pytest.raises(RuntimeError):
        gh.delete_ref("o/r", "e2e/x/valid")


def test_delete_ref_5xx_is_error() -> None:
    gh, _ = client(*[done(1, err="gh: Bad Gateway (HTTP 502)")] * 4)
    with pytest.raises(RuntimeError):
        gh.delete_ref("o/r", "e2e/x/valid")


def test_refs_with_prefix_strips_refs_heads() -> None:
    hit = [
        {"ref": f"refs/heads/e2e/{RUN}/valid", "object": {"sha": "a", "type": "commit"}}
    ]
    gh, runner = client(done(0, json.dumps(hit)), done(0, "[]"))
    assert gh.refs_with_prefix("o/r", f"e2e/{RUN}/") == [f"e2e/{RUN}/valid"]
    assert any(
        item.startswith(f"repos/o/r/git/matching-refs/heads/e2e/{RUN}/?")
        for item in runner.call_args.args[0]
    )
    assert gh.refs_with_prefix("o/r", f"e2e/{RUN}/") == []


def make_run(listed, tracked=(), prs=None):
    from e2e.application import Run

    run = Run("dev")
    run.id = RUN
    run.author = Mock()
    run.author.refs_with_prefix.return_value = list(listed)
    run.author.collection.side_effect = lambda path, key=None: (prs or {}).get(path, [])
    run.refs = [(run.repo, r) for r in tracked]
    run.submissions = {}
    return run


def deleted(run):
    return [c.args[1] for c in run.author.delete_ref.call_args_list]


def test_sweep_deletes_only_own_untracked_branches() -> None:
    own = f"e2e/{RUN}/valid"
    listed = [
        own,
        f"e2e/{RUN}/remove-own",
        f"e2e/{RUN}",
        f"e2e/{RUN}9/x",
        "e2e/20990101000000000000/valid",
        "e2e/",
        "main",
    ]
    run = make_run(listed, tracked=[own])
    assert run.sweep_refs() == []
    assert deleted(run) == [f"e2e/{RUN}/remove-own"]
    prefix = run.author.refs_with_prefix.call_args.args[1]
    assert prefix == f"e2e/{RUN}/"


def test_sweep_closes_open_pr_of_swept_branch() -> None:
    ref = f"e2e/{RUN}/valid"
    run = make_run([ref])
    run.author.collection.side_effect = lambda path, key=None: (
        [{"number": 7}] if "pulls" in path and ref in path.replace("%2F", "/") else []
    )
    assert run.sweep_refs() == []
    patches = [c for c in run.author.request.call_args_list if c.args[0] == "PATCH"]
    assert any(
        c.args[1].endswith("pulls/7") and c.args[2] == {"state": "closed"}
        for c in patches
    )
    assert deleted(run) == [ref]


def test_sweep_continues_after_failure() -> None:
    a, b = f"e2e/{RUN}/a", f"e2e/{RUN}/b"
    run = make_run([a, b])
    run.author.delete_ref.side_effect = [RuntimeError("boom"), None]
    errors = run.sweep_refs()
    assert len(errors) == 1 and deleted(run) == [a, b]


def test_cleanup_survives_already_deleted_branches() -> None:
    run = make_run([], tracked=[f"e2e/{RUN}/valid"])
    run.keep = False
    assert run.cleanup() == []
    assert deleted(run) == [f"e2e/{RUN}/valid"]


def test_cleanup_keep_does_not_sweep() -> None:
    run = make_run([f"e2e/{RUN}/x"])
    run.keep = True
    assert run.cleanup() == []
    run.author.delete_ref.assert_not_called()
    run.author.refs_with_prefix.assert_not_called()


def test_refs_with_prefix_reads_every_page() -> None:
    """A run opens dozens of branches; GitHub pages the answer at 100."""
    page = [{"ref": f"refs/heads/e2e/{RUN}/b{n}"} for n in range(100)]
    rest = [{"ref": f"refs/heads/e2e/{RUN}/last"}]
    gh, _ = client(done(0, json.dumps(page)), done(0, json.dumps(rest)))
    names = gh.refs_with_prefix("o/r", f"e2e/{RUN}/")
    assert len(names) == 101 and names[-1] == f"e2e/{RUN}/last"


def test_a_failed_pr_close_does_not_keep_the_branch() -> None:
    """Deleting the branch is still tried; both errors are not hidden."""
    ref = f"e2e/{RUN}/valid"
    run = make_run([ref])
    run.author.collection.side_effect = RuntimeError("GitHub GET pulls: boom")
    errors = run.sweep_refs()
    assert len(errors) == 1 and deleted(run) == [ref]
