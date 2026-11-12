"""Tests for pull request scope selection."""

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from tools.check_registry import CheckError
from tools.validate_scope import select_manifest


def test_select_exactly_one_manifest(tmp_path: Path) -> None:
    """
    Select one safe manifest and reject an unrelated changed file.

    The test supplies fake Git output and never calls GitHub.
    """
    result = CompletedProcess(
        [], 0, stdout=b"M\0applications/app.busy.clock/manifest.yml\0"
    )
    with patch("tools.validate_scope.subprocess.run", return_value=result):
        assert (
            select_manifest(tmp_path, "a" * 40, "b" * 40)
            == "applications/app.busy.clock/manifest.yml"
        )
    result.stdout = b"M\0applications/app.busy.clock/manifest.yml\0M\0README.md\0"
    with (
        patch("tools.validate_scope.subprocess.run", return_value=result),
        pytest.raises(CheckError) as error,
    ):
        select_manifest(tmp_path, "a" * 40, "b" * 40)
    assert error.value.id == "pr_scope"


def scope_error(tmp_path: Path, stdout: bytes) -> CheckError:
    """Run selection over fake Git output and return the raised check error."""
    result = CompletedProcess([], 0, stdout=stdout)
    with (
        patch("tools.validate_scope.subprocess.run", return_value=result),
        pytest.raises(CheckError) as error,
    ):
        select_manifest(tmp_path, "a" * 40, "b" * 40)
    return error.value


def test_upper_case_folder_is_id_format_not_scope(tmp_path: Path) -> None:
    """A single manifest in an upper-case folder fails the ID rule and suggests lower case."""
    error = scope_error(tmp_path, b"A\0applications/App.Busy.X/manifest.yml\0")
    assert error.id == "app_id_format"
    assert error.detail == "`App.Busy.X`: use lower case — `app.busy.x`"


def test_extra_file_is_scope_with_path(tmp_path: Path) -> None:
    """Another changed file is listed with its status in the scope detail."""
    error = scope_error(
        tmp_path,
        b"M\0applications/app.busy.clock/manifest.yml\0A\0tools/x.py\0",
    )
    assert error.id == "pr_scope"
    assert "A tools/x.py" in error.detail


def test_two_manifests_and_deletion_are_scope(tmp_path: Path) -> None:
    """Two manifests or a deleted manifest fail the scope check."""
    two = (
        b"A\0applications/app.busy.a/manifest.yml\0"
        b"A\0applications/app.busy.b/manifest.yml\0"
    )
    assert scope_error(tmp_path, two).id == "pr_scope"
    deleted = b"D\0applications/app.busy.a/manifest.yml\0"
    assert scope_error(tmp_path, deleted).id == "pr_scope"


def test_scope_cli_emits_actionable_annotation(monkeypatch, capsys):
    """
    Send scope failures to stderr so command substitution cannot hide the annotation.

    No manifest path is emitted when a maintainer-only change is rejected.
    """
    from unittest.mock import patch

    from tools.validate_scope import main

    monkeypatch.setattr(
        "sys.argv", ["pr_scope", "--base", "a" * 40, "--head", "b" * 40]
    )
    with patch(
        "tools.validate_scope.select_manifest",
        side_effect=CheckError("pr_scope", "A tools/x.py"),
    ):
        assert main() == 1
    output = capsys.readouterr()
    assert "::error title=Changed files::" in output.err
    assert "tools/x.py" in output.err and "made by maintainers" in output.err
    assert output.out == ""
