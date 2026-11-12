"""Offline checks for the E2E harness."""

from unittest.mock import Mock

from e2e.application import Run
from e2e.domain import owned_ref


def test_cleanup_exact_namespace_and_keep():
    """
    Clean only this run's tracked PRs and branches, preserving foreign runs.

    Keep mode must perform no network mutations at all.
    """
    run = Run("dev")
    run.id = "run"
    run.author = Mock()
    run.author.request.return_value = {"state": "open"}
    run.author.refs_with_prefix.return_value = []
    run.refs = [
        (run.repo, "e2e/run/case"),
        (run.repo, "e2e/runner/case"),
        (run.repo, "main"),
    ]
    run.submissions = {
        "own": {"ref": "e2e/run/case", "number": 1},
        "foreign": {"ref": "e2e/other/case", "number": 2},
    }
    assert run.cleanup() == []
    # The own PR is read and closed; own branches go through delete_ref, foreign ones stay.
    assert run.author.request.call_count == 2
    deleted = [call.args[1] for call in run.author.delete_ref.call_args_list]
    assert deleted == ["e2e/run/case"]
    assert owned_ref("e2e/run", "run") and not owned_ref("e2e/runner", "run")
    run.keep = True
    run.author.reset_mock()
    assert run.cleanup() == []
    run.author.request.assert_not_called()
