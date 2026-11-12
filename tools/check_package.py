"""Deterministic checks for an application package produced by CI."""

import argparse
import gzip
import json
import re
import tarfile
from collections.abc import Callable
from pathlib import Path

from tools.check_registry import outcome
from tools.lib_app_id import APP_ID
from tools.lib_tar import (
    MAX_ENTRIES,
    MAX_UNPACKED,
    has_extended_header,
    is_plain_entry,
    path_is_safe,
)

MAX_PACKAGE = 100 * 1024 * 1024
MAX_JS = 256_000
MAX_MANIFEST = 512
MAX_NAME = 32
NAME_CHARACTERS = re.compile(r"[A-Za-z0-9 !?&'()+,\-./:#$%@_]+")


def name_problem(name: object) -> str:
    """What is wrong with the app name for the card design, or an empty string."""
    if not isinstance(name, str) or not name:
        return "`name` must be a non-empty string"
    if len(name) > MAX_NAME:
        return f"`name` is {len(name)} characters, the limit is {MAX_NAME}"
    if name != name.strip() or "  " in name:
        return "`name` has a leading, trailing or double space"
    if not NAME_CHARACTERS.fullmatch(name):
        return "`name` has characters that are not allowed (Latin only, no emoji)"
    return ""


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


Fail = Callable[[str, str], None]
MANIFEST_KEYS = ("format_version", "id", "name", "version", "api_version")


def unpacked_size_ok(path: Path) -> bool:
    """True if the gzip stream expands to no more than `MAX_UNPACKED` bytes."""
    with gzip.open(path, "rb") as raw:
        expanded = 0
        while chunk := raw.read(1024 * 1024):
            expanded += len(chunk)
            if expanded > MAX_UNPACKED:
                return False
    return True


def check_ustar(path: Path, members: list[tarfile.TarInfo], fail: Fail) -> None:
    """Every header must be plain ustar: the magic sits at offset 257 of each header."""
    with gzip.open(path, "rb") as raw:
        for member in members:
            raw.seek(member.offset + 257)
            if raw.read(8) != b"ustar\x0000":
                fail("tar_format", "")
                return


def check_members(
    members: list[tarfile.TarInfo], app_id: str, fail: Fail
) -> dict[str, tarfile.TarInfo]:
    """Check every entry's path, kind and size; return the entries by name."""
    names: dict[str, tarfile.TarInfo] = {}
    for member in members:
        if not path_is_safe(member.name, app_id):
            fail("package_path", "")
            continue
        if member.name in names:
            fail("duplicate_path", "")
        names[member.name] = member
        if has_extended_header(member):
            fail("tar_format", "")
        if not is_plain_entry(member):
            fail("archive_entry", "")
        if member.isfile() and member.name.endswith(".js") and member.size > MAX_JS:
            fail("javascript_size", "")
    return names


def check_manifest_fields(data: dict, app_id: str, fail: Fail) -> None:
    """Check the fields of `appmeta/manifest.json` that the firmware relies on."""
    for key in MANIFEST_KEYS:
        if key not in data:
            fail("manifest_field", key)
    if type(data.get("format_version")) is not int or data["format_version"] != 1:
        fail("manifest_format_version", "")
    if data.get("id") != app_id:
        fail(
            "manifest_id",
            f"Folder `applications/{app_id}/`, but "
            f"`appmeta/manifest.json` has `{data.get('id')}`",
        )
    if "name" in data and name_problem(data["name"]):
        fail("manifest_name", name_problem(data["name"]))
    for key in ("version", "api_version"):
        if key in data and (
            not isinstance(data[key], str) or not SEMVER.fullmatch(data[key])
        ):
            fail("manifest_version", key)
    heap = data.get("heap_size_kib")
    if heap is not None and (type(heap) is not int or not 1 <= heap <= 512):
        fail("manifest_heap", "")
    if data.get("debug") is True:
        fail("manifest_debug", "")


def check_app_manifest(
    archive: tarfile.TarFile,
    names: dict[str, tarfile.TarInfo],
    app_id: str,
    fail: Fail,
) -> None:
    """Find `main.js` and the manifest, then check the manifest content."""
    main = names.get(f"{app_id}/scripts/main.js")
    if main is None or not main.isfile():
        fail("main_missing", "")
    manifest = names.get(f"{app_id}/appmeta/manifest.json")
    if manifest is None or not manifest.isfile():
        fail("manifest_missing", "")
        return
    if manifest.size > MAX_MANIFEST:
        fail("manifest_size", "")
        return
    file = archive.extractfile(manifest)
    try:
        data = json.load(file) if file is not None else None
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        fail("manifest_json", "")
        return
    check_manifest_fields(data, app_id, fail)


def check_package(path: Path, app_id: str) -> list[dict[str, str]]:
    """
    Check the archive and app manifest without executing submitted code.

    Return actionable findings as structured data suitable for CI reporting.
    """
    findings = []

    def fail(code: str, detail: str = "") -> None:
        """
        Add a failed check to the structured result.

        The registry explains the problem and the fix; detail names the specifics.
        """
        findings.append(outcome(code, "fail", detail))

    if not APP_ID.fullmatch(app_id):
        fail("invalid_id")
        return findings
    if path.stat().st_size > MAX_PACKAGE:
        fail("package_size")
        return findings
    try:
        if not unpacked_size_ok(path):
            fail("unpacked_size")
            return findings
        with tarfile.open(path, mode="r:gz") as archive:
            members = []
            for member in archive:
                members.append(member)
                if len(members) > MAX_ENTRIES:
                    fail("archive_entries")
                    return findings
            check_ustar(path, members, fail)
            names = check_members(members, app_id, fail)
            check_app_manifest(archive, names, app_id, fail)
    except (tarfile.TarError, OSError, EOFError) as exc:
        fail("archive_invalid", exc.__class__.__name__)
    return findings


def main() -> int:
    """
    Run package checks and write a machine-readable result.

    A failed check returns a non-zero exit code without running app code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("app_id")
    parser.add_argument("--output", type=Path, default=Path("results.json"))
    args = parser.parse_args()
    try:
        findings = check_package(args.package, args.app_id)
    except OSError:
        findings = [outcome("package_missing")]
    result = {"status": "fail" if findings else "pass", "checks": findings}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
