"""Derive backend publication metadata from an approved CI build output."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from tools.bar import digest, read_json, validate_result

from tools.check_manifest import category_aliases
from tools.check_package import check_package
from tools.check_png import BACK_SCREENSHOT as BACK_SCREENSHOT_RULES
from tools.check_png import ICON_BACK, ICON_FRONT, validate_png
from tools.check_png import SCREENSHOT as SCREENSHOT_RULES
from tools.check_registry import blocks
from tools.lib_tar import load_json_member
from tools.pipeline_report import read_results
from tools.publish_upload import validate_publication

SCREENSHOT = re.compile(r"screenshot-[1-9][0-9]*\.png")
BACK_SCREENSHOT = re.compile(r"back-screenshot-[1-9][0-9]*\.png")


def artifact(path: Path, prefix: str, suffix: str, destination: Path) -> dict:
    """
    Verify and copy an artifact using its computed content hash.

    Only regular files are accepted and matching objects are safely deduplicated.
    """
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 100 * 1024 * 1024
    ):
        raise ValueError("Missing or oversized build artifact")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    target = destination / (sha + suffix)
    if not target.exists():
        shutil.copyfile(path, target)
    return {
        "key": f"{prefix}/{target.name}",
        "sha256": sha,
        "size": path.stat().st_size,
    }


def read_approved_inputs(
    build_dir: Path, results: Path, original: Path
) -> tuple[dict, dict]:
    """
    The approved PR manifest and the resolved manifest of the build, checked to match.

    Failed checks, symlinks, oversized files or a resolved manifest that differs from
    what was approved all refuse the publication.
    """
    if any(blocks(item["id"], item["result"]) for item in read_results(results)):
        raise ValueError("Checks did not pass")
    if original.is_symlink() or original.stat().st_size > 65536:
        raise ValueError("Invalid original manifest")
    original_data = yaml.safe_load(original.read_text(encoding="utf-8"))
    original_data["category"] = category_aliases().get(
        original_data["category"], original_data["category"]
    )
    resolved_path = build_dir / "manifest.resolved.json"
    if resolved_path.is_symlink() or resolved_path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid resolved manifest")
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    if (build_dir / "media").is_symlink() or not (build_dir / "media").is_dir():
        raise ValueError("Invalid build media directory")
    for key in ("sourcecode", "category", "short_description"):
        if resolved.get(key) != original_data.get(key):
            raise ValueError(f"Resolved manifest differs from the approved {key}")
    for key in ("description", "changelog"):
        if (
            not original_data[key].startswith("@")
            and resolved.get(key) != original_data[key]
        ):
            raise ValueError(f"Resolved manifest differs from the approved {key}")
    return original_data, resolved


def verify_package(build_dir: Path, resolved: dict, expected_app_id: str) -> Path:
    """Check the package against the resolved app metadata; return its path."""
    app_id = resolved["appmeta"]["id"]
    if app_id != expected_app_id:
        raise ValueError("Application ID does not match the approved PR path")
    package_path = build_dir / "package.tgz"
    if package_path.is_symlink() or check_package(package_path, app_id):
        raise ValueError("Invalid application package")
    with tarfile.open(package_path, "r:gz") as archive:
        manifest = load_json_member(archive, f"{app_id}/appmeta/manifest.json")
    if manifest != resolved["appmeta"]:
        raise ValueError("App metadata differs from the approved package")
    return package_path


def github_owner(source: dict) -> str:
    """Owner part of the source repository; only https://github.com/<owner>/<repo>."""
    parsed = urlsplit(source["origin"])
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.netloc != "github.com" or len(parts) != 2:
        raise ValueError("Invalid source repository")
    return parts[0]


def checked_images(
    build_dir: Path,
    names: object,
    approved: list,
    pattern: re.Pattern,
    rules: tuple,
    label: str,
) -> list[str]:
    """Screenshot file names of one kind: right count and shape, regular PNGs of the right size."""
    if (
        not isinstance(names, list)
        or len(names) != len(approved)
        or any(
            not isinstance(name, str) or not pattern.fullmatch(name) for name in names
        )
    ):
        raise ValueError(f"Invalid {label} list")
    for name in names:
        image = build_dir / "media" / name
        if image.is_symlink():
            raise ValueError(f"Invalid {label} path")
        validate_png(image, *rules)
    return names


def checked_icons(build_dir: Path) -> tuple[Path, Path]:
    """Paths of the front and back icons; each, if present, must be a clean PNG."""
    icon = build_dir / "media" / "icon.png"
    back = build_dir / "media" / "icon-back.png"
    for image, rules in ((icon, ICON_FRONT), (back, ICON_BACK)):
        if image.is_symlink():
            raise ValueError("Invalid icon path")
        if image.exists():
            validate_png(image, *rules)
    return icon, back


def notice_data(
    build_dir: Path,
    output: Path,
    resolved: dict,
    source: dict,
    who: tuple,
    files: tuple,
) -> dict:
    """The notice for the backend: texts, content-addressed artifacts and who approved."""
    author, pr_number, head_sha, merge_sha, approved_by, ci_run_url = who
    package_path, screenshots, back_screenshots, icon, back = files
    appmeta = resolved["appmeta"]
    media = build_dir / "media"
    data = {
        "alias": appmeta["id"],
        "name": appmeta["name"],
        "version": appmeta["version"],
        "api_version": appmeta["api_version"],
        "author": author,
        "origin": source["origin"],
        "commit_sha": source["commit_sha"],
        "subdir": source.get("subdir", "."),
        "category": resolved["category"],
        "short_description": resolved["short_description"],
        "description": resolved["description"],
        "changelog": resolved["changelog"],
        "package": artifact(package_path, "packages", ".tgz", output),
        "sources": artifact(build_dir / "sources.tar.gz", "sources", ".tar.gz", output),
        "screenshots": [
            artifact(media / name, "assets", ".png", output) for name in screenshots
        ],
        "toolchain": resolved["toolchain"],
        "pr_number": pr_number,
        "head_sha": head_sha,
        "merge_sha": merge_sha,
        "approved_by": approved_by,
    }
    if back_screenshots:
        data["back_screenshots"] = [
            artifact(media / name, "assets", ".png", output)
            for name in back_screenshots
        ]
    if ci_run_url:
        data["ci_run_url"] = ci_run_url
    if icon.exists():
        data["icon"] = artifact(icon, "assets", ".png", output)
    if back.exists():
        data["icon_back"] = artifact(back, "assets", ".png", output)
    return data


def prepare_publication(
    build_dir: Path,
    results: Path,
    original: Path,
    output: Path,
    pr_number: int,
    head_sha: str,
    merge_sha: str,
    approved_by: str,
    expected_app_id: str,
    ci_run_url: str | None = None,
    author: str | None = None,
) -> dict:
    """
    Verify the approved build output against the original PR manifest and prepare notice data.

    Only a passing trusted check result allows content-addressed objects to be prepared.
    """
    original_data, resolved = read_approved_inputs(build_dir, results, original)
    package_path = verify_package(build_dir, resolved, expected_app_id)
    # The real-bar check is required in a PR but not for publication: absent is fine,
    # a present result must still belong to this package.
    evidence = read_json(results).get("bar")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValueError("Invalid hardware smoke result")
        validate_result(evidence, {**evidence, "app_id": expected_app_id, "head_sha": head_sha,
                                  "version": resolved["appmeta"]["version"], "package_sha256": digest(package_path)})
    source = original_data["sourcecode"]["location"]
    owner = github_owner(source)
    screenshots = checked_images(
        build_dir,
        resolved["screenshots"],
        original_data["screenshots"],
        SCREENSHOT,
        SCREENSHOT_RULES,
        "screenshot",
    )
    back_screenshots = checked_images(
        build_dir,
        resolved.get("back_screenshots", []),
        original_data.get("back_screenshots", []),
        BACK_SCREENSHOT,
        BACK_SCREENSHOT_RULES,
        "back screenshot",
    )
    icon, back = checked_icons(build_dir)
    output.mkdir(parents=True, exist_ok=False)
    data = notice_data(
        build_dir,
        output,
        resolved,
        source,
        (author or owner, pr_number, head_sha, merge_sha, approved_by, ci_run_url),
        (package_path, screenshots, back_screenshots, icon, back),
    )
    validate_publication(data, results, output)
    return data


def main() -> int:
    """
    Prepare content-addressed artifacts and metadata for a publication job.

    Inputs come from the approved PR and its selected workflow run.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notice", type=Path, required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--ci-run-url")
    parser.add_argument("--author")
    args = parser.parse_args()
    result = prepare_publication(
        args.build_dir,
        args.results,
        args.original,
        args.output,
        args.pr,
        args.head,
        args.merge,
        args.approver,
        args.app_id,
        args.ci_run_url,
        args.author,
    )
    args.notice.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
