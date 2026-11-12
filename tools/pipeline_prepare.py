"""Find an open PR and check its scope and manifest as untrusted data."""

import base64
import binascii
import json
import os
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import quote

from tools.check_manifest import check_manifest
from tools.check_registry import CheckError, blocks, outcome
from tools.lib_github import GitHubGateway
from tools.lib_removal import removal_findings
from tools.validate_scope import SHA_PATTERN, removed_folder, select_from_changes

MAX_MANIFEST = 64 * 1024
STATUSES = {"added": "A", "modified": "M", "removed": "D", "renamed": "R"}
AUTHOR_ATTESTATION = re.compile(
    r"^\s*-\s*\[x\]\s*I['’]m the author of the app or have write access to its repository\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class PullSkip(ValueError):
    """The run has no open application PR to report on, so nothing is commented."""


def find_pull(gateway: GitHubGateway, head_sha: str) -> dict:
    """Find exactly one open PR whose head is the given commit."""
    if not SHA_PATTERN.fullmatch(head_sha):
        raise PullSkip("Invalid head SHA")
    matches = []
    for page in range(1, 101):
        pulls = gateway._request("GET", f"pulls?state=open&per_page=100&page={page}")
        if not isinstance(pulls, list):
            raise TypeError("Invalid pull request list")
        for pull in pulls:
            if (
                isinstance(pull, dict)
                and isinstance(pull.get("head"), dict)
                and pull["head"].get("sha") == head_sha
            ):
                matches.append(pull)
        if len(pulls) < 100:
            break
    else:
        raise PullSkip("Too many pull request pages")
    if len(matches) != 1 or not isinstance(matches[0].get("number"), int):
        raise PullSkip("Expected one open pull request for the head SHA")
    return matches[0]


def changed_files(gateway: GitHubGateway, number: int) -> list[tuple[str, str]]:
    """List `(status, path)` of the PR files, `A`/`M`/`D`/`R`, from the GitHub API."""
    changes = []
    for page in range(1, 31):
        files = gateway._request(
            "GET", f"pulls/{number}/files?per_page=100&page={page}"
        )
        if not isinstance(files, list):
            raise TypeError("Invalid pull request files")
        for item in files:
            changes.append(
                (STATUSES.get(item.get("status"), "M"), str(item.get("filename")))
            )
        if len(files) < 100:
            return changes
    return changes


def read_manifest(gateway: GitHubGateway, path: str, head_sha: str) -> bytes:
    """Read the manifest bytes at the PR head; unreadable content is the author's error."""
    content = gateway._request(
        "GET", f"contents/{quote(path, safe='/')}?ref={head_sha}"
    )
    if (
        not isinstance(content, dict)
        or content.get("encoding") != "base64"
        or not isinstance(content.get("content"), str)
    ):
        raise CheckError(
            "manifest_readable", "`manifest.yml` is not a regular file (symlink?)"
        )
    if len(content["content"]) > 2 * MAX_MANIFEST:
        raise CheckError(
            "manifest_schema",
            f"`manifest.yml` is larger than {MAX_MANIFEST // 1024} KiB",
        )
    try:
        data = base64.b64decode("".join(content["content"].split()), validate=True)
    except binascii.Error:
        raise CheckError(
            "manifest_readable", "`manifest.yml` is not readable"
        ) from None
    if len(data) > MAX_MANIFEST:
        raise CheckError(
            "manifest_schema",
            f"`manifest.yml` is {len(data)} bytes, the limit is {MAX_MANIFEST}",
        )
    return data


def check_pull(
    gateway: GitHubGateway,
    number: int,
    head_sha: str,
    output: Path,
    categories_url: str,
    changes: list[tuple[str, str]] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """
    Run the scope and manifest checks and copy the manifest for later jobs.

    Returns the application folder (empty when scope failed) and the findings.
    """
    try:
        path = select_from_changes(changes or changed_files(gateway, number))
    except CheckError as error:
        return "", [outcome(error.id, "fail", error.detail)]
    folder = path.split("/")[1]
    try:
        data = read_manifest(gateway, path, head_sha)
    except CheckError as error:
        return folder, [outcome(error.id, "fail", error.detail)]
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.yml").write_bytes(data)
    return folder, check_manifest(
        output / "manifest.yml", categories_url, folder=folder
    )


def human_author(pull: dict) -> str:
    """Login of the PR author; a bot or an odd login is not reviewed."""
    user = pull.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if (
        not isinstance(author, str)
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", author) is None
        or user.get("type") != "User"
    ):
        raise PullSkip("Expected a human pull request author")
    return author


def folder_in_head(gateway: GitHubGateway, folder: str, head_sha: str) -> bool:
    """True if `applications/<folder>` still exists in the PR head."""
    try:
        gateway._request(
            "GET", f"contents/applications/{quote(folder, safe='')}?ref={head_sha}"
        )
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return False
        raise
    return True


def run_early_checks(
    gateway: GitHubGateway,
    number: int,
    head_sha: str,
    output: Path,
    categories_url: str,
    run_url: str,
    login: str = "",
    catalog_url: str = "",
) -> tuple[str, list[dict[str, str]], bool, dict[str, object] | None]:
    """
    Scope and manifest checks, or the removal checks; a crash is `internal_error`.

    Returns the folder, the findings, whether the checks crashed and, for a PR that
    removes a whole application folder, the facts of the removal.
    """
    try:
        changes = changed_files(gateway, number)
        folder = removed_folder(
            changes, lambda name: folder_in_head(gateway, name, head_sha)
        )
        if folder is not None:
            findings, removal = removal_findings(gateway, login, folder, catalog_url)
            return folder, findings, False, removal
        folder, findings = check_pull(
            gateway, number, head_sha, output, categories_url, changes
        )
        return folder, findings, False, None
    except Exception:  # noqa: BLE001
        # The full trace stays in the job log only.
        traceback.print_exc(file=sys.stderr)
        return "", [outcome("internal_error", "fail", run_url)], True, None


def early_result(
    findings: list[dict[str, str]],
    folder: str,
    attested: bool,
    internal: bool,
    removal: dict[str, object] | None = None,
) -> dict[str, object]:
    """The results.json of a PR that stops before the build."""
    result: dict[str, object] = {
        "status": "fail"
        if internal or any(blocks(item["id"], item["result"]) for item in findings)
        else "pass",
        "checks": findings,
        "inventory": [],
        "skipped": ["build", "package", "catalog"],
        "skip_reason": "internal" if internal else "removal" if removal else "manifest",
        "author_attested": attested,
        "manifest_path": f"applications/{folder}/manifest.yml"
        if folder and not internal and not removal
        else "",
    }
    if removal is not None:
        result["removal"] = removal
    return result


def prepare(
    gateway: GitHubGateway,
    head_sha: str,
    output: Path,
    categories_url: str = "",
    run_url: str = "",
    results: Path | None = None,
    catalog_url: str = "",
) -> dict[str, object]:
    """
    Find the open PR and decide whether the build can start.

    The pull-request checkout is never executed in this privileged job. A blocking
    scope or manifest problem is written to `results` and the build is skipped.
    """
    pull = find_pull(gateway, head_sha)
    number = pull["number"]
    full = gateway._request("GET", f"pulls/{number}")
    if any(item.get("name") == "maintenance" for item in full.get("labels", [])):
        return {"skip": "true", "pr_number": number, "head_sha": head_sha}
    author = human_author(pull)
    body = pull.get("body")
    attested = isinstance(body, str) and bool(AUTHOR_ATTESTATION.search(body))
    folder, findings, internal, removal = run_early_checks(
        gateway, number, head_sha, output, categories_url, run_url, author, catalog_url
    )
    if not attested:
        findings.append(outcome("author_attestation"))
    early = (
        internal
        or removal is not None
        or any(
            blocks(item["id"], item["result"])
            for item in findings
            if item["id"] != "author_attestation"
        )
    )
    if early and results is not None:
        document = early_result(findings, folder, attested, internal, removal)
        results.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return {
        "skip": "false",
        "early": str(early).lower(),
        "pr_number": number,
        "head_sha": head_sha,
        "app_id": folder,
        "removal": folder if removal is not None else "",
        "author": author,
        "author_attested": str(attested).lower(),
    }


def main() -> int:
    """
    Prepare a manifest and write validated outputs for GitHub Actions.

    The GitHub token is read from the job environment and never printed.
    """
    head = os.environ.get("HEAD_SHA", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    catalog = os.environ.get("CATALOG_API", "").rstrip("/")
    result = prepare(
        GitHubGateway(repository, token),
        head,
        Path("prepared"),
        f"{catalog}/api/catalog/v1/categories" if catalog else "",
        os.environ.get("RUN_URL", ""),
        Path("results.json"),
        catalog,
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in result.items())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
