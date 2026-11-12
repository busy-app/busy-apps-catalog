"""Discover trusted inputs for a merged catalog submission."""

import argparse
import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import yaml

from tools.check_catalog import github_repository
from tools.lib_github import GitHubGateway
from tools.lib_merge import (
    build_artifact_name,
    select_approver,
    select_artifact_run,
    select_merged_pr,
)
from tools.validate_scope import PATH_PATTERN, SHA_PATTERN, removed_folder


def paged(gateway: GitHubGateway, path: str, key: str | None = None) -> list[dict]:
    """
    Read all bounded pages of one fixed GitHub API collection.

    Malformed or excessively large collections fail closed.
    """
    items = []
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        response = gateway._request("GET", f"{path}{separator}per_page=100&page={page}")
        batch = response.get(key) if key and isinstance(response, dict) else response
        if not isinstance(batch, list) or any(
            not isinstance(item, dict) for item in batch
        ):
            raise ValueError("Invalid GitHub collection")
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise ValueError("GitHub collection exceeds page limit")


PAUSES = (2, 4, 8, 16)


def find_merged_pr(gateway: GitHubGateway, merge_sha: str) -> dict:
    """
    Find the one merged PR of a commit, waiting while GitHub has not linked them yet.

    Right after a squash merge the commit can briefly have no PR: that case is retried
    after 2, 4, 8 and 16 s. More than one candidate fails at once.
    """
    for pause in (*PAUSES, None):
        pulls = paged(gateway, f"commits/{merge_sha}/pulls")
        matches = [
            item
            for item in pulls
            if item.get("merge_commit_sha") == merge_sha
            and item.get("merged_at")
            and item.get("state") == "closed"
        ]
        if matches or pause is None:
            return dict(select_merged_pr(pulls, merge_sha))
        time.sleep(pause)
    raise AssertionError("unreachable")


def merged_pr_facts(pull: dict) -> tuple[int, str, str]:
    """Number, head commit and author login of the merged PR; anything odd is an error."""
    number = pull.get("number")
    head = pull.get("head")
    user = pull.get("user")
    sha = head.get("sha") if isinstance(head, dict) else None
    author = user.get("login") if isinstance(user, dict) else None
    if (
        type(number) is not int
        or number < 1
        or not isinstance(sha, str)
        or not SHA_PATTERN.fullmatch(sha)
        or not isinstance(author, str)
    ):
        raise ValueError("Invalid merged pull request")
    return number, sha, author


def folder_in_commit(gateway: GitHubGateway, folder: str, sha: str) -> bool:
    """True if `applications/<folder>` exists in the commit."""
    try:
        gateway._request(
            "GET", f"contents/applications/{quote(folder, safe='')}?ref={sha}"
        )
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return False
        raise
    return True


def fetch_manifest(gateway: GitHubGateway, path: str, sha: str) -> bytes:
    """The manifest as it was at the PR head, downloaded as data and size-checked."""
    content = gateway._request("GET", f"contents/{quote(path, safe='/')}?ref={sha}")
    if (
        not isinstance(content, dict)
        or content.get("encoding") != "base64"
        or not isinstance(content.get("content"), str)
        or len(content["content"]) > 131072
    ):
        raise ValueError("Invalid manifest response")
    manifest = base64.b64decode("".join(content["content"].split()), validate=True)
    if len(manifest) > 65536:
        raise ValueError("Manifest exceeds size limit")
    return manifest


def source_repository(manifest: bytes) -> str:
    """
    `owner/repo` of the application source named in the manifest, for the GitHub API.

    It is read the way the PR checks read it (`check_catalog.github_repository`):
    only `https://github.com/<owner>/<repo>`, with `.git` dropped. The template ends
    the origin in `.git`, and `repos/<owner>/<repo>.git` is 404 in the API.
    """
    origin = yaml.safe_load(manifest)["sourcecode"]["location"]["origin"]
    repository = github_repository(origin)
    if repository is None or repository.endswith("/"):
        raise ValueError(
            "Invalid source repository: the origin is not https://github.com/<owner>/<repo>"
        )
    return repository


def pipeline_artifacts(gateway: GitHubGateway, sha: str) -> tuple[int, str | None]:
    """The newest successful pipeline run with both artifacts of `sha`, and the build name."""
    runs = paged(
        gateway,
        "actions/workflows/pipeline.yml/runs?status=success",
        "workflow_runs",
    )
    candidates = []
    for run in runs:
        run_id = run.get("id")
        if type(run_id) is not int or run_id < 1:
            raise ValueError("Invalid workflow run ID")
        candidates.append(
            {
                **run,
                "artifacts": paged(
                    gateway, f"actions/runs/{run_id}/artifacts", "artifacts"
                ),
            }
        )
    run_id = select_artifact_run(candidates, sha)
    chosen = next(item for item in candidates if item["id"] == run_id)
    return run_id, build_artifact_name(
        {item.get("name") for item in chosen["artifacts"]}, sha
    )


def discover(gateway: GitHubGateway, merge_sha: str, output: Path) -> dict[str, object]:
    """
    Resolve the merged PR, current approval and exact pipeline artifact run.

    The original manifest is downloaded as data, never executed.
    """
    if not SHA_PATTERN.fullmatch(merge_sha):
        raise ValueError("Invalid merge SHA")
    number, sha, author = merged_pr_facts(find_merged_pr(gateway, merge_sha))
    files = paged(gateway, f"pulls/{number}/files")
    if not any(
        isinstance(item.get("filename"), str)
        and PATH_PATTERN.fullmatch(item["filename"])
        for item in files
    ):
        print(
            "Maintenance merge: no application manifests changed; publication skipped."
        )
        return {"skip": "true"}
    reviews = paged(gateway, f"pulls/{number}/reviews")
    approver = select_approver(reviews, sha, author)
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", approver) is None:
        raise ValueError("Invalid reviewer login")
    folder = removed_folder(
        [
            ("D" if item.get("status") == "removed" else "M", str(item.get("filename")))
            for item in files
        ],
        lambda name: folder_in_commit(gateway, name, merge_sha),
    )
    if folder is not None:
        return {
            "skip": "false",
            "kind": "remove",
            "pr_number": number,
            "head_sha": sha,
            "merge_sha": merge_sha,
            "app_id": folder,
            "approved_by": approver,
        }
    if len(files) != 1 or files[0].get("status") not in {"added", "modified"}:
        raise ValueError("Merged PR must contain one application manifest")
    path = files[0].get("filename")
    if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
        raise ValueError("Invalid application manifest path")
    manifest = fetch_manifest(gateway, path, sha)
    repository = source_repository(manifest)
    author = GitHubGateway(repository, gateway._token).owner_login()
    run_id, build_artifact = pipeline_artifacts(gateway, sha)
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.yml").write_bytes(manifest)
    return {
        "skip": "false",
        "kind": "publish",
        "pr_number": number,
        "head_sha": sha,
        "merge_sha": merge_sha,
        "app_id": path.split("/")[1],
        "approved_by": approver,
        "run_id": run_id,
        "author": author,
        "build_artifact": build_artifact,
    }


def main() -> int:
    """
    Discover publication inputs and export safe workflow outputs.

    No application code or build data is executed in this job.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = discover(
        GitHubGateway(
            os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
        ),
        args.merge,
        args.output,
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
        file.writelines(f"{key}={value}\n" for key, value in result.items())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
