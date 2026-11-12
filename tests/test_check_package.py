"""Checks for deterministic validation of app packages."""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

from tools.check_package import check_package


def package(path: Path, manifest: dict, extra: dict[str, bytes] | None = None) -> None:
    """
    Create a small USTAR package for a validation scenario.

    The generated files are local and contain no executable application code.
    """
    files = {
        "app.busy.clock/appmeta/manifest.json": json.dumps(manifest).encode(),
        "app.busy.clock/scripts/main.js": b"// test",
        **(extra or {}),
    }
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_package_rules(tmp_path: Path) -> None:
    """
    Accept a valid archive and report specific invalid manifest fields.

    The archive contains the minimum files accepted by the device.
    """
    path = tmp_path / "package.tgz"
    manifest = {
        "format_version": 1,
        "id": "app.busy.clock",
        "name": "Clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
    }
    package(path, manifest)
    assert check_package(path, "app.busy.clock") == []
    manifest.update(version="1.0", heap_size_kib=513, debug=True)
    package(
        path,
        manifest,
        {"outside/main.js": b"x", "app.busy.clock/scripts/large.js": b"x" * 256_001},
    )
    codes = {item["id"] for item in check_package(path, "app.busy.clock")}
    assert {
        "manifest_version",
        "manifest_heap",
        "manifest_debug",
        "package_path",
        "javascript_size",
    } <= codes


def test_reject_gnu_archive(tmp_path: Path) -> None:
    """
    Reject an archive using the GNU tar format.

    The device requires USTAR even when the files themselves are valid.
    """
    path = tmp_path / "gnu.tgz"
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo("app.busy.clock/scripts/main.js")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    assert "tar_format" in {
        item["id"] for item in check_package(path, "app.busy.clock")
    }


def test_require_supported_manifest_format_version(tmp_path: Path) -> None:
    """Reject unknown, string, and boolean manifest format versions."""
    path = tmp_path / "package.tgz"
    manifest = {
        "format_version": 1,
        "id": "app.busy.clock",
        "name": "Clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
    }
    for invalid in (2, "1", True):
        manifest["format_version"] = invalid
        package(path, manifest)
        assert "manifest_format_version" in {
            item["id"] for item in check_package(path, "app.busy.clock")
        }


def test_reject_excessive_unpacked_archive(tmp_path: Path) -> None:
    """
    Stop validation when gzip expands beyond the configured resource limit.

    A small fake limit avoids allocating a large test fixture.
    """
    path = tmp_path / "package.tgz"
    package(
        path,
        {
            "format_version": 1,
            "id": "app.busy.clock",
            "name": "Clock",
            "version": "1.0.0",
            "api_version": "27.14.0",
        },
    )
    with patch("tools.check_package.MAX_UNPACKED", 1024):
        assert "unpacked_size" in {
            item["id"] for item in check_package(path, "app.busy.clock")
        }


def test_manifest_id_detail_shows_both_values(tmp_path: Path) -> None:
    """The safety-net check names the folder and the id found in the package."""
    path = tmp_path / "package.tgz"
    package(
        path,
        {
            "format_version": 1,
            "id": "other",
            "name": "Clock",
            "version": "1.0.0",
            "api_version": "27.14.0",
        },
    )
    found = [
        i for i in check_package(path, "app.busy.clock") if i["id"] == "manifest_id"
    ]
    assert found and found[0]["detail"] == (
        "Folder `applications/app.busy.clock/`, but `appmeta/manifest.json` has `other`"
    )


def test_name_limit_is_32_characters(tmp_path: Path) -> None:
    """A 32-character name passes; 33 fails with the actual length."""
    path = tmp_path / "package.tgz"
    manifest = {
        "format_version": 1,
        "id": "app.busy.clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
    }
    package(path, {**manifest, "name": "N" * 32})
    assert "manifest_name" not in {
        item["id"] for item in check_package(path, "app.busy.clock")
    }
    package(path, {**manifest, "name": "N" * 33})
    found = [
        item
        for item in check_package(path, "app.busy.clock")
        if item["id"] == "manifest_name"
    ]
    assert found and "33 characters" in found[0]["detail"]
