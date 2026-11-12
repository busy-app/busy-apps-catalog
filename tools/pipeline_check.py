"""Produce a fail-closed report for the CI check job."""

import argparse
import json
import os
import tarfile
from pathlib import Path

import yaml

from tools.check_catalog import HttpCatalogSource, compare_catalog_with_diff
from tools.check_manifest import check_manifest
from tools.check_package import check_package
from tools.check_registry import REGISTRY, blocks, outcome
from tools.lib_license import ALLOWED
from tools.lib_media import build_view
from tools.lib_tar import load_json_member

PACKAGE_IDS = {check.id for check in REGISTRY.values() if check.stage == "package"}


DIAGNOSTIC_WORDS = ("error", "syntax", "api_version", "256000")


def build_detail(build_log: Path | None) -> str:
    """Pick the diagnostic lines from the tail of a failed build log."""
    if build_log is None or build_log.is_symlink() or not build_log.is_file():
        return ""
    with build_log.open("rb") as log:
        log.seek(max(0, build_log.stat().st_size - 8192))
        lines = log.read(8192).decode("utf-8", errors="replace").splitlines()
    diagnostics = [
        line for line in lines if any(word in line.lower() for word in DIAGNOSTIC_WORDS)
    ]
    return " ".join(diagnostics)[-1800:]


def read_build_error(path: Path | None) -> tuple[str, str] | None:
    """Read the check id and detail written by a failed build, if any."""
    if path is None or path.is_symlink() or not path.is_file():
        return None
    if path.stat().st_size > 16 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if (
        not isinstance(data, dict)
        or data.get("id") not in REGISTRY
        or not isinstance(data.get("detail", ""), str)
    ):
        return None
    return data["id"], data.get("detail", "")


def read_license(path: Path) -> str | None:
    """SPDX id the build recognised, or None (missing file, unknown text)."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("spdx")
    except (ValueError, AttributeError):
        return None
    return value if value in ALLOWED else None


def manifest_findings(manifest: Path, catalog_url: str, app_id: str) -> list[dict]:
    """Checks of the PR manifest, including the public category list when the API is known."""
    categories = (
        f"{catalog_url.rstrip('/')}/api/catalog/v1/categories" if catalog_url else ""
    )
    return check_manifest(manifest, categories, folder=app_id)


def source_location(build_dir: Path) -> tuple[str, str]:
    """Origin and commit of the resolved manifest; empty when it cannot be trusted."""
    path = build_dir / "manifest.resolved.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        return "", ""
    resolved = json.loads(path.read_text(encoding="utf-8"))
    location = (
        resolved.get("sourcecode", {}).get("location", {})
        if isinstance(resolved, dict)
        else {}
    )
    return location.get("origin", ""), location.get("commit_sha", "")


def package_inventory(package: Path, app_id: str) -> tuple[list[dict], dict]:
    """File list of the package and its app manifest, read without unpacking."""
    with tarfile.open(package, "r:gz") as archive:
        inventory = [
            {"path": member.name, "size": member.size}
            for member in archive
            if member.isfile()
        ]
        app_manifest = load_json_member(archive, f"{app_id}/appmeta/manifest.json")
    return inventory, app_manifest


def catalog_findings(
    catalog_url: str, app_id: str, app_manifest: dict, origin: str, commit: str
) -> tuple[list[dict], str | None]:
    """Compare with the published catalog; an unusable catalog is `not_run`, not a failure."""
    if not catalog_url:
        return [outcome("catalog_unavailable", "not_run")], None
    try:
        source = HttpCatalogSource(catalog_url)
    except ValueError:
        return [outcome("catalog_unavailable", "not_run")], None
    checks, compare_url = compare_catalog_with_diff(
        source,
        app_id,
        app_manifest["version"],
        app_manifest["api_version"],
        origin,
        commit,
    )
    return list(checks), compare_url


def package_stage(
    build_dir: Path, app_id: str, catalog_url: str
) -> tuple[list[dict], list[dict], str | None]:
    """
    Package checks, then (if the package is sound) its inventory and the catalog diff.

    Returns findings, inventory and the compare link. An unreadable package is
    `package_missing`.
    """
    findings: list[dict] = []
    inventory: list[dict] = []
    compare_url = None
    try:
        package = build_dir / "package.tgz"
        findings.extend(check_package(package, app_id))
        if not any(item["id"] in PACKAGE_IDS for item in findings):
            inventory, app_manifest = package_inventory(package, app_id)
            origin, commit = source_location(build_dir)
            checks, compare_url = catalog_findings(
                catalog_url, app_id, app_manifest, origin, commit
            )
            findings.extend(checks)
    except (OSError, ValueError, KeyError, tarfile.TarError):
        findings.append(outcome("package_missing"))
    return findings, inventory, compare_url


def artifact_findings(build_dir: Path, license_name: str | None) -> list[dict]:
    """Optional artifacts of the build: the front icon and a recognised license."""
    findings = []
    if not (build_dir / "media" / "icon.png").is_file():
        findings.append(outcome("icon_missing"))
    if license_name is None:
        findings.append(outcome("license_unrecognised"))
    return findings


def media_summary(manifest: Path, build_dir: Path) -> dict | None:
    """What the comment shows of screenshots and icons; None if the inputs are unusable."""
    try:
        icons = json.loads(
            (build_dir / "media-sources.json").read_text(encoding="utf-8")
        )
        return build_view(
            yaml.safe_load(manifest.read_text(encoding="utf-8")),
            icons if isinstance(icons, dict) else {},
        )
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return None


def stage_results(
    build_dir: Path | None,
    app_id: str,
    catalog_url: str = "",
    author_attested: bool = False,
    build_log: Path | None = None,
    build_error: Path | None = None,
    manifest: Path | None = None,
) -> dict:
    """
    Validate the manifest and the produced build output and report every problem.

    Missing artifacts or unavailable required rules prevent a passing status.
    """
    findings = []
    license_name = None
    inventory: list[dict] = []
    compare_url = None
    skipped: list[str] = []
    if manifest is not None:
        findings.extend(manifest_findings(manifest, catalog_url, app_id))
    if build_dir is None:
        error = read_build_error(build_error) or ("build", build_detail(build_log))
        findings.append(outcome(error[0], "fail", error[1]))
        skipped = ["package", "catalog"]
    else:
        license_name = read_license(build_dir / "license.json")
        findings.extend(artifact_findings(build_dir, license_name))
        package_findings, inventory, compare_url = package_stage(
            build_dir, app_id, catalog_url
        )
        findings.extend(package_findings)
    media = media_summary(manifest, build_dir) if manifest and build_dir else None
    if not author_attested:
        findings.append(outcome("author_attestation"))
    result = {
        "license": license_name,
        "status": "fail"
        if any(blocks(i["id"], i["result"]) for i in findings)
        else "pass",
        "checks": findings,
        "inventory": inventory,
        "media": media,
        "skipped": skipped,
        "author_attested": author_attested,
        "manifest_path": f"applications/{app_id}/manifest.yml",
    }
    if compare_url is not None:
        result["compare_url"] = compare_url
    return result


def main() -> int:
    """
    Write machine-readable findings for the trusted report job.

    A complete result is written even when the isolated build failed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--build-log", type=Path)
    parser.add_argument("--build-error", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(
            stage_results(
                args.build_dir,
                args.app_id,
                os.environ.get("CATALOG_API", ""),
                os.environ.get("AUTHOR_ATTESTED", "").lower() == "true",
                args.build_log,
                args.build_error,
                args.manifest,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
