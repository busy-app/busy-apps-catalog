"""Inputs of an AI review: which PR build to use and the request body for the backend."""

import base64
import json
from pathlib import Path
from urllib.parse import quote

import yaml

from tools.lib_github import GitHubGateway
from tools.lib_merge import build_artifact_name, select_artifact_run
from tools.pipeline_report import read_results
from tools.publish_discover import paged
from tools.validate_scope import PATH_PATTERN, SHA_PATTERN

MAX_MANIFEST = 65536
MAX_RESOLVED = 1024 * 1024


class ReviewInputError(ValueError):
    """Why a review cannot start; the text is shown to the person who ran it."""


def find_pipeline_run(gateway: GitHubGateway, sha: str) -> tuple[int, str]:
    """Return the run id and build artifact name of the checks of exactly this commit."""
    runs = paged(
        gateway, "actions/workflows/pipeline.yml/runs?status=success", "workflow_runs"
    )
    candidates = [
        {
            **run,
            "artifacts": paged(
                gateway, f"actions/runs/{run['id']}/artifacts", "artifacts"
            ),
        }
        for run in runs
        if type(run.get("id")) is int
    ]
    try:
        run_id = select_artifact_run(candidates, sha)
    except ValueError:
        raise ReviewInputError(
            "Run the checks first: there is no finished pipeline run for this commit"
        ) from None
    chosen = next(item for item in candidates if item["id"] == run_id)
    name = build_artifact_name({item.get("name") for item in chosen["artifacts"]}, sha)
    if name is None:
        raise ReviewInputError("The pipeline run has no build artifact")
    return run_id, name


def discover(gateway: GitHubGateway, number: int, output: Path) -> dict[str, object]:
    """
    Resolve an open PR to its head commit, manifest and pipeline artifacts.

    The manifest is downloaded as data into `output/manifest.yml`; nothing is executed.
    """
    pull = gateway._request("GET", f"pulls/{number}")
    head = pull.get("head") if isinstance(pull, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(pull, dict) or pull.get("state") != "open":
        raise ReviewInputError("The pull request is not open")
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise ReviewInputError("Invalid pull request head")
    files = paged(gateway, f"pulls/{number}/files")
    manifests = [
        item["filename"]
        for item in files
        if isinstance(item.get("filename"), str)
        and PATH_PATTERN.fullmatch(item["filename"])
    ]
    if len(files) != 1 or len(manifests) != 1:
        raise ReviewInputError("The PR must change exactly one application manifest")
    content = gateway._request(
        "GET", f"contents/{quote(manifests[0], safe='/')}?ref={sha}"
    )
    encoded = content.get("content") if isinstance(content, dict) else None
    if not isinstance(encoded, str) or len(encoded) > 131072:
        raise ReviewInputError("Invalid manifest response")
    manifest = base64.b64decode("".join(encoded.split()), validate=True)
    if len(manifest) > MAX_MANIFEST:
        raise ReviewInputError("Manifest exceeds size limit")
    run_id, build_artifact = find_pipeline_run(gateway, sha)
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.yml").write_bytes(manifest)
    return {
        "pr_number": number,
        "head_sha": sha,
        "run_id": run_id,
        "build_artifact": build_artifact,
        "app_id": manifests[0].split("/")[1],
    }


def build_request(
    build_dir: Path, results: Path, original: Path, number: int, requested_by: str
) -> dict[str, object]:
    """
    The body of `POST /ci/reviews`: source location, manifest texts and check facts.

    Texts come from the resolved manifest (after `@file` substitution); screenshot
    paths come from the PR manifest, because the backend reads them at `commit_sha`.
    """
    resolved_path = build_dir / "manifest.resolved.json"
    if resolved_path.is_symlink() or resolved_path.stat().st_size > MAX_RESOLVED:
        raise ReviewInputError("Invalid resolved manifest")
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    if original.is_symlink() or original.stat().st_size > MAX_MANIFEST:
        raise ReviewInputError("Invalid original manifest")
    manifest = yaml.safe_load(original.read_text(encoding="utf-8"))
    location = manifest["sourcecode"]["location"]
    appmeta = resolved["appmeta"]
    return {
        "pr_number": number,
        "requested_by": requested_by,
        "sourcecode": {
            "origin": location["origin"],
            "commit_sha": location["commit_sha"],
            "subdir": location.get("subdir", "."),
        },
        "manifest": {
            "id": appmeta["id"],
            "name": appmeta["name"],
            "version": appmeta["version"],
            "api_version": appmeta["api_version"],
            "category": resolved["category"],
            "short_description": resolved["short_description"],
            "description": resolved["description"],
            "changelog": resolved["changelog"],
            "screenshots": manifest["screenshots"],
            "back_screenshots": manifest.get("back_screenshots", []),
        },
        "facts": read_results(results),
    }
