"""Tests for firmware-compatible output package generation."""

import json
import tarfile
from pathlib import Path

import pytest

from tools.check_package import check_package
from tools.check_registry import CheckError
from tools.lib_package import create_package


def test_package_build_output(tmp_path: Path) -> None:
    """
    Produce a valid reproducible USTAR archive from a built app.

    Verify that the package is accepted by the independent checker.
    """
    app = tmp_path / "dist" / "app.busy.clock"
    (app / "appmeta").mkdir(parents=True)
    (app / "scripts").mkdir()
    (app / "scripts" / "main.js").write_text("// test")
    (app / "appmeta" / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "app.busy.clock",
                "name": "Clock",
                "version": "1.0.0",
                "api_version": "27.14.0",
            }
        )
    )
    output = tmp_path / "package.tgz"
    create_package(app.parent, "app.busy.clock", output)
    first = output.read_bytes()
    assert not check_package(output, "app.busy.clock")
    with tarfile.open(output, "r:gz") as tar:
        assert "app.busy.clock/scripts/main.js" in tar.getnames()
        assert all(not member.pax_headers for member in tar)
    create_package(app.parent, "app.busy.clock", output)
    assert output.read_bytes() == first
    (app / "leak").symlink_to(tmp_path)
    with pytest.raises(CheckError, match="archive_entry"):
        create_package(app.parent, "app.busy.clock", output)


def make_archive(path: Path, members: list[tuple[str, bytes | str | None]]) -> None:
    """Write a gzip tar; bytes are files, None is a directory, a str is a symlink."""
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(content, str):
                info.type = tarfile.SYMTYPE
                info.linkname = content
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, __import__("io").BytesIO(content))


def test_resolve_accepts_directory_and_newer_cli_archive(tmp_path: Path) -> None:
    """`dist/<id>/` is used as is; `dist/<id>.tgz` is unpacked safely."""
    from tools.lib_package import resolve_built_root

    dist = tmp_path / "dist"
    (dist / "app.busy.x").mkdir(parents=True)
    assert resolve_built_root(dist, "app.busy.x", tmp_path / "s") == dist / "app.busy.x"
    other = tmp_path / "other"
    other.mkdir()
    make_archive(
        other / "app.busy.x.tgz",
        [("app.busy.x", None), ("app.busy.x/scripts/main.js", b"x")],
    )
    root = resolve_built_root(other, "app.busy.x", tmp_path / "scratch")
    assert (root / "scripts" / "main.js").read_bytes() == b"x"


@pytest.mark.parametrize(
    ("members", "check_id"),
    [
        ([("app.busy.x/link", "/etc/passwd")], "archive_entry"),
        ([("app.busy.x/../evil", b"x")], "package_path"),
        ([("/abs/file", b"x")], "package_path"),
        ([("other.id/file", b"x")], "package_path"),
    ],
)
def test_unsafe_archive_is_rejected(tmp_path: Path, members, check_id) -> None:
    """Links, traversal, absolute paths and a foreign prefix fail the build."""
    from tools.lib_package import resolve_built_root

    dist = tmp_path / "dist"
    dist.mkdir()
    make_archive(dist / "app.busy.x.tgz", members)
    with pytest.raises(CheckError) as error:
        resolve_built_root(dist, "app.busy.x", tmp_path / "scratch")
    assert error.value.id == check_id


def test_missing_output_lists_what_dist_contains(tmp_path: Path) -> None:
    """Neither form present: `build` names up to 10 files found."""
    from tools.lib_package import resolve_built_root

    dist = tmp_path / "dist"
    dist.mkdir()
    for index in range(12):
        (dist / f"f{index:02}").write_text("x")
    with pytest.raises(CheckError) as error:
        resolve_built_root(dist, "app.busy.x", tmp_path / "scratch")
    assert error.value.id == "build"
    assert "neither `dist/app.busy.x/` nor `dist/app.busy.x.tgz`" in error.value.detail
    assert "`f09`" in error.value.detail and "`f10`" not in error.value.detail
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CheckError) as error:
        resolve_built_root(empty, "app.busy.x", tmp_path / "scratch")
    assert "found: nothing" in error.value.detail
