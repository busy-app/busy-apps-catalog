"""Rules for a catalog manifest, checked without fetching or running app code."""

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator

from tools.check_registry import outcome
from tools.lib_app_id import MANIFEST_PATH, is_valid
from tools.lib_http import HttpError, request

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "catalog-manifest.schema.json"
CATEGORIES = SCHEMA.with_name("categories.json")


def category_aliases() -> dict[str, str]:
    """
    Read the agreed local category IDs and compatibility aliases.

    This copy remains usable when the public categories API is unavailable.
    """
    rows = json.loads(CATEGORIES.read_text())["categories"]
    return {alias: row["id"] for row in rows for alias in row["aliases"]}


def annotation_line(path: Path, message: str) -> int:
    """
    Locate an error's dotted field path in the YAML syntax tree.

    File-level and missing-field errors point to the document start.
    """
    try:
        node = yaml.compose(path.read_text(encoding="utf-8"))
        for part in message.split(":", 1)[0].split("."):
            if isinstance(node, yaml.MappingNode):
                node = next(value for key, value in node.value if key.value == part)
            elif isinstance(node, yaml.SequenceNode) and part.isdigit():
                node = node.value[int(part)]
            else:
                return 1
        return node.start_mark.line + 1 if node else 1
    except (OSError, UnicodeError, yaml.YAMLError, StopIteration, IndexError):
        return 1


def public_categories(url: str) -> list[str]:
    """Sorted ids of the public category list; any bad answer is a `ValueError`."""
    rows = json.loads(request("GET", url, timeout=5).content)
    if (
        not isinstance(rows, list)
        or not rows
        or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in rows
        )
    ):
        raise ValueError("Invalid category response")
    return sorted(item["id"] for item in rows)


def public_category_findings(path: Path, url: str) -> list[dict[str, str]]:
    """
    Compare a parsed category with the public list without blocking offline use.

    Aliases produce a warning; network failures fall back to the local copy.
    """
    findings = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return findings
    if not isinstance(data, dict) or not isinstance(data.get("category"), str):
        return findings
    category = data["category"]
    aliases = category_aliases()
    if category in aliases:
        detail = f"category: {category} → {aliases[category]}"
        findings.append(outcome("category_alias", "fail", detail))
        category = aliases[category]
    if url:
        try:
            allowed = public_categories(url)
        except (HttpError, ValueError):
            findings.append(outcome("category_offline", "not_run", "category"))
        else:
            if category not in allowed:
                detail = f"category: {category}; allowed: {', '.join(allowed)}"
                findings.append(outcome("category_public", "fail", detail))
    return findings


def safe_relative(value: str) -> bool:
    """
    Check that a manifest path stays inside the application directory.

    Absolute paths, parent traversal and backslashes are forbidden.
    """
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
    )


Findings = list[dict[str, str]]


def read_manifest(path: Path) -> tuple[object, Findings]:
    """Parse the YAML file; an unreadable file yields no manifest and one finding."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), []
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark else ""
        what = getattr(error, "problem", None) or "invalid YAML"
        return None, [outcome("manifest_readable", "fail", f"{where}{what}")]
    except (OSError, UnicodeError):
        return None, [outcome("manifest_readable", "fail", "not a UTF-8 text file")]


def schema_findings(manifest: object) -> Findings:
    """Errors of the JSON schema; a category alias is resolved first."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    if isinstance(manifest, dict) and isinstance(manifest.get("category"), str):
        manifest = {
            **manifest,
            "category": category_aliases().get(
                manifest["category"], manifest["category"]
            ),
        }
    return [
        outcome(
            "manifest_schema",
            "fail",
            f"{'.'.join(str(part) for part in error.absolute_path) or 'manifest'}: "
            f"{error.message}",
        )
        for error in Draft202012Validator(schema).iter_errors(manifest)
    ]


def reference_findings(manifest: dict) -> Findings:
    """File references are allowed only in `description`, and only as safe paths."""
    findings = []
    for key in ("short_description", "category"):
        value = manifest.get(key)
        if isinstance(value, str) and value.startswith("@"):
            findings.append(
                outcome("manifest_references", "fail", f"{key}: file reference")
            )
    value = manifest.get("description")
    if (
        isinstance(value, str)
        and value.startswith("@")
        and not safe_relative(value[1:])
    ):
        findings.append(outcome("manifest_references", "fail", f"description: {value}"))
    return findings


def text_findings(manifest: dict) -> Findings:
    """Length of an inline description and the inline-only changelog."""
    findings = []
    value = manifest.get("description")
    if (
        isinstance(value, str)
        and not value.startswith("@")
        and not 50 <= len(value) <= 4000
    ):
        detail = f"description: {len(value)} characters, expected 50–4000"
        findings.append(outcome("manifest_schema", "fail", detail))
    changelog = manifest.get("changelog")
    if isinstance(changelog, str):
        if changelog.startswith("@"):
            detail = "changelog must be inline text, not a file reference"
            findings.append(outcome("changelog_text", "fail", detail))
        elif not 10 <= len(changelog) <= 4000:
            detail = f"changelog is {len(changelog)} characters, expected 10–4000"
            findings.append(outcome("changelog_text", "fail", detail))
    return findings


def source_findings(manifest: dict, folder: str) -> Findings:
    """Official namespace and a safe `subdir` of the source location."""
    findings = []
    location = manifest.get("sourcecode")
    location = location.get("location") if isinstance(location, dict) else None
    origin = location.get("origin") if isinstance(location, dict) else None
    if isinstance(origin, str) and folder.startswith("app.busy."):
        owner = [part for part in urlsplit(origin).path.split("/") if part][:1]
        if owner and owner[0].lower() != "busy-app":
            detail = f"repository owner: {owner[0]}"
            findings.append(outcome("official_namespace", "fail", detail))
    if (
        isinstance(location, dict)
        and isinstance(location.get("subdir"), str)
        and not safe_relative(location["subdir"])
    ):
        detail = f"sourcecode.location.subdir: {location['subdir']}"
        findings.append(outcome("manifest_references", "fail", detail))
    return findings


def media_path_findings(manifest: dict) -> Findings:
    """Screenshot paths must stay inside the application directory."""
    findings = []
    for field in ("screenshots", "back_screenshots"):
        paths = manifest.get(field)
        if isinstance(paths, list) and any(
            not isinstance(item, str) or not safe_relative(item) for item in paths
        ):
            findings.append(outcome("manifest_references", "fail", f"{field}: paths"))
    return findings


def validate(path: Path, folder: str | None = None) -> Findings:
    """
    Parse YAML and validate the catalog manifest and local path syntax.

    The validator does not access the application repository or run its code.
    `folder` names the application directory when the file was copied elsewhere.
    """
    findings = []
    folder = folder or path.parent.name
    if path.name != "manifest.yml" or not is_valid(folder):
        findings.append(outcome("manifest_path", "fail", str(path)))
    manifest, unreadable = read_manifest(path)
    if unreadable:
        return [*findings, *unreadable]
    findings += schema_findings(manifest)
    if not isinstance(manifest, dict):
        return findings
    return [
        *findings,
        *reference_findings(manifest),
        *text_findings(manifest),
        *source_findings(manifest, folder),
        *media_path_findings(manifest),
    ]


def validate_scope(repo: Path, base_ref: str, manifest: Path) -> list[dict[str, str]]:
    """
    Require a pull request to change exactly the selected application manifest.

    Renames and deletions require the separate maintainer workflow.
    """
    if (
        base_ref.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", base_ref)
        or ".." in base_ref
    ):
        raise ValueError("Invalid base reference")
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            f"{base_ref}...HEAD",
            "--",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=30,
    )
    values = result.stdout.decode("utf-8").split("\0")
    if values[-1] == "":
        values.pop()
    expected = manifest.resolve().relative_to(repo.resolve()).as_posix()
    if (
        len(values) != 2
        or values[0] not in {"A", "M"}
        or values[1] != expected
        or not MANIFEST_PATH.fullmatch(expected)
    ):
        return [outcome("pr_scope")]
    return []


def check_manifest(
    path: Path,
    categories_url: str,
    base_ref: str | None = None,
    folder: str | None = None,
) -> list[dict[str, str]]:
    """Run every manifest check and return only the findings."""
    findings = validate(path, folder)
    findings.extend(public_category_findings(path, categories_url))
    if base_ref:
        try:
            findings.extend(validate_scope(Path.cwd(), base_ref, path))
        except (OSError, ValueError, subprocess.SubprocessError):
            findings.append(
                outcome("pr_scope", "not_run", "could not compare with the base branch")
            )
    return findings
