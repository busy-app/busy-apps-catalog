"""Tests for lock-file driven build execution."""

import io
import json
import tarfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from tools.lib_build import (
    locked_cli_version,
    run_build,
    run_npm_build,
    validate_main_script,
)


def test_npm_build_uses_lock_and_disables_scripts(tmp_path: Path) -> None:
    """
    Use the pinned CLI and suppress npm lifecycle scripts.

    Fake subprocesses ensure no third-party code executes during the test.
    """
    app = tmp_path / "app"
    app.mkdir()
    (app / "package-lock.json").write_text(
        json.dumps({"packages": {"node_modules/@busy-app/cli": {"version": "0.2.3"}}})
    )
    executable = app / "node_modules" / ".bin" / "busy-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    with patch(
        "tools.lib_build.subprocess.run", return_value=CompletedProcess([], 0)
    ) as run:
        assert run_npm_build(app, tmp_path) == "0.2.3"
    assert run.call_args_list[0].args[0] == ["npm", "ci", "--ignore-scripts"]
    command = run.call_args_list[1].args[0]
    assert command[-2:] == ["/work/node_modules/.bin/busy-cli", "build"]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--cap-drop" in command
    assert "GITHUB_TOKEN" not in run.call_args_list[0].kwargs["env"]


def test_missing_cli_version_fails(tmp_path: Path) -> None:
    """
    Reject a lock file that does not pin the build tool.

    This fails before running npm or app-provided code.
    """
    (tmp_path / "package-lock.json").write_text('{"packages": {}}')
    with pytest.raises(ValueError):
        locked_cli_version(tmp_path)


def test_pnpm_uses_frozen_lock_without_hooks(tmp_path: Path) -> None:
    """
    Resolve pinned pnpm and CLI versions and disable all install hooks.

    The fake executor keeps the test independent of network and npm packages.
    """
    (tmp_path / "package.json").write_text('{"packageManager":"pnpm@10.33.0"}')
    (tmp_path / "pnpm-lock.yaml").write_text(
        "importers:\n  .:\n    devDependencies:\n      '@busy-app/cli':\n        version: 0.2.1(peer@1.0.0)\n"
    )
    executable = tmp_path / "node_modules" / ".bin" / "busy-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    results = [
        CompletedProcess([], 0, stdout="10.33.0\n"),
        CompletedProcess([], 0),
        CompletedProcess([], 0),
    ]
    with patch("tools.lib_build.subprocess.run", side_effect=results) as run:
        assert run_build(tmp_path, tmp_path) == "0.2.1"
    assert run.call_args_list[1].args[0] == [
        "npx",
        "--yes",
        "--package=pnpm@10.33.0",
        "pnpm",
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
        "--ignore-pnpmfile",
    ]
    assert "GITHUB_TOKEN" not in run.call_args_list[1].kwargs["env"]
    (tmp_path / "package-lock.json").write_text("{}")
    with pytest.raises(ValueError, match="exactly one"):
        run_build(tmp_path, tmp_path)


@pytest.mark.parametrize(
    ("source", "chunk", "error"),
    [
        (
            b"import {x} from './chunk.js'; export default x;",
            b"export const x = 1;",
            None,
        ),
        (b"export const = ;", None, "invalid module syntax"),
        (b"import '../../outside.js';", None, "invalid or missing import"),
        (b"import './missing.js';", None, "invalid or missing import"),
        (b"import '/absolute.js';", None, "invalid or missing import"),
    ],
)
def test_module_package_syntax_and_imports(
    tmp_path: Path, source: bytes, chunk: bytes | None, error: str | None
) -> None:
    """
    Parse real ESM modules without evaluation and reject broken import paths.

    Multiple files are supported while scripts outside the package cannot be loaded.
    """
    package = tmp_path / "package.tgz"
    files = {"app.busy.clock/scripts/main.js": source}
    if chunk is not None:
        files["app.busy.clock/scripts/chunk.js"] = chunk
    with tarfile.open(package, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    if error:
        with pytest.raises(ValueError, match=error):
            validate_main_script(package, "app.busy.clock")
    else:
        validate_main_script(package, "app.busy.clock")
