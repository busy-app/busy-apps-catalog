"""Select the single submitted catalog manifest from a pull request."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.check_registry import CheckError, get
from tools.lib_app_id import MANIFEST_PATH, invalid_reason, is_valid
from tools.lib_comment import render_checks

PATH_PATTERN = MANIFEST_PATH
FOLDER_PATH = re.compile(r"applications/([^/]+)/manifest\.yml")
APP_FILE = re.compile(r"applications/([^/]+)/.+")
REMOVAL_PREFIX = "removal:"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
MAX_LISTED = 10


def select_manifest(repo: Path, base_sha: str, head_sha: str) -> str:
    """
    Identify exactly one added or modified manifest between the two commits.

    Other changes fail `pr_scope` and need the separate maintenance path;
    a manifest in a folder with an invalid application ID fails `app_id_format`.
    """
    if not SHA_PATTERN.fullmatch(base_sha) or not SHA_PATTERN.fullmatch(head_sha):
        raise ValueError("Invalid pull request commit SHA")
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            f"{base_sha}...{head_sha}",
            "--",
        ],
        cwd=repo,
        capture_output=True,
        check=True,
        timeout=30,
    )
    parts = result.stdout.decode("utf-8").split("\0")
    if parts[-1] == "":
        parts.pop()
    changes = list(zip(parts[0::2], parts[1::2], strict=False))
    folder = removed_folder(
        changes, lambda name: folder_in_commit(repo, head_sha, name)
    )
    if folder is not None:
        return REMOVAL_PREFIX + folder
    return select_from_changes(changes)


def folder_in_commit(repo: Path, sha: str, folder: str) -> bool:
    """True if `applications/<folder>` exists in the commit."""
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:applications/{folder}"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def removed_folder(changes: list[tuple[str, str]], exists) -> str | None:
    """
    The application id when the PR deletes one whole `applications/<id>/` folder.

    Every change must be a deletion inside the same folder and the folder must be
    gone in the head (`exists(folder)` is False); anything else is not a removal.
    """
    matches = [APP_FILE.fullmatch(path) for _, path in changes]
    if not changes or any(status != "D" for status, _ in changes) or None in matches:
        return None
    folders = {match.group(1) for match in matches}
    if len(folders) != 1:
        return None
    folder = folders.pop()
    return None if exists(folder) else folder


def select_from_changes(changes: list[tuple[str, str]]) -> str:
    """
    Pick the manifest path from `(status, path)` changes, `A`/`M`/`D`/`R`.

    Shared by the unprivileged validate job and the trusted pipeline.
    """
    folder = FOLDER_PATH.fullmatch(changes[0][1]) if len(changes) == 1 else None
    if folder is None or changes[0][0] not in {"A", "M"}:
        listed = [f"{status} {path}" for status, path in changes[:MAX_LISTED]]
        if len(changes) > MAX_LISTED:
            listed.append(f"and {len(changes) - MAX_LISTED} more")
        raise CheckError("pr_scope", ", ".join(listed))
    app_id = folder.group(1)
    if not is_valid(app_id) or not PATH_PATTERN.fullmatch(changes[0][1]):
        raise CheckError("app_id_format", f"`{app_id}`: {invalid_reason(app_id)}")
    return changes[0][1]


def report(error: CheckError) -> None:
    """Print the GitHub annotation and the step summary for a failed scope check."""
    check = get(error.id)
    message = f"{check.problem} {check.fix}"
    if error.detail:
        message = f"{check.problem} ({error.detail}) {check.fix}"
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={check.title}::{escaped}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        finding = [{"id": error.id, "result": "fail", "detail": error.detail}]
        lines = render_checks(finding, stages=("manifest",))
        with open(summary, "a", encoding="utf-8") as file:
            file.write("## Changed files\n\n" + "\n".join(lines) + "\n")


def main() -> int:
    """
    Print the safe relative path of the selected manifest for workflow use, or
    `removal:<id>` when the PR removes a whole application folder.

    Invalid pull request scope exits without producing a usable path.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        print(select_manifest(Path.cwd(), args.base, args.head))
    except CheckError as error:
        report(error)
        return 1
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
