"""Tests for safe resolution of catalog manifest inputs."""

import struct
import subprocess
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.check_registry import CheckError, outcome
from tools.lib_comment import render_comment
from tools.lib_fetch import (
    fetch_checkout,
    resolve_manifest,
    source_directory,
    source_file,
)


def test_resolve_manifest_and_boundaries(tmp_path: Path) -> None:
    """
    Resolve files from a subdirectory and reject paths outside it.

    Symlinks are checked after resolving their real targets.
    """
    root = tmp_path / "repository"
    app = root / "app"
    app.mkdir(parents=True)
    (app / "README.md").write_text(
        "A small clock for the BUSY Bar that shows the time and the date."
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        """
        Make a PNG chunk with a valid checksum.

        The screenshot fixture is generated without external dependencies.
        """
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    header = struct.pack(">IIBBBBB", 72, 16, 8, 0, 0, 0, 0)
    rows = (b"\0" + b"\0" * 72) * 16
    (app / "screen.png").write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    manifest = {
        "category": "meetings",
        "sourcecode": {"location": {"subdir": "app"}},
        "description": "@README.md",
        "changelog": "Initial release",
        "screenshots": ["screen.png"],
    }
    assert resolve_manifest(manifest, root)["description"].startswith("A small clock")
    assert resolve_manifest(manifest, root)["category"] == "productivity"
    (app / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        source_file(app, "outside/secret")
    with pytest.raises(ValueError):
        source_directory(root, "../elsewhere")
    (app / "README.md").write_text("x" * 4001)
    with pytest.raises(ValueError):
        resolve_manifest(manifest, root)


def test_fetch_rejects_untrusted_origin(tmp_path: Path) -> None:
    """
    Reject local and non-GitHub sources before running Git.

    Only a full pinned SHA and a GitHub HTTPS origin are permitted.
    """
    with pytest.raises(ValueError):
        fetch_checkout("file:///tmp/source", "a" * 40, tmp_path / "checkout")
    with pytest.raises(ValueError):
        fetch_checkout(
            "https://github.com/example/project", "main", tmp_path / "checkout"
        )


def test_resolve_manifest_rejects_markdown_outside_allowlist(tmp_path: Path) -> None:
    """
    Reject unsafe markdown after resolving catalog text from its pinned source.

    The reported failure includes the affected field and line number.
    """
    root = tmp_path / "repository"
    root.mkdir()
    manifest = {
        "sourcecode": {"location": {"subdir": "."}},
        "description": "# Clock\n\n<script>alert(1)</script>\n\n" + "x" * 40,
        "changelog": "Initial release",
        "screenshots": [],
    }

    with pytest.raises(ValueError, match="description: line 3"):
        resolve_manifest(manifest, root)


def png(width: int, height: int) -> bytes:
    """Create a minimal valid grayscale PNG of the requested size."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    rows = (b"\0" + b"\0" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_missing_commit_is_a_source_commit_check(tmp_path: Path) -> None:
    """A commit that cannot be checked out is reported as the `source_commit` check."""
    ok = subprocess.CompletedProcess([], 0, stdout="")
    failure = subprocess.CalledProcessError(1, "git")
    with (
        patch("tools.lib_fetch.subprocess.run", side_effect=[ok, failure]),
        pytest.raises(CheckError) as error,
    ):
        fetch_checkout(
            "https://github.com/example/project", "a" * 40, tmp_path / "checkout"
        )
    assert error.value.id == "source_commit"
    comment = render_comment([outcome(error.value.id, "fail", error.value.detail)])
    assert "No commit found" in comment
    assert (
        "Check that the corresponding commit is available in your repository" in comment
    )


def test_oversized_screenshot_names_the_file_and_its_size(tmp_path: Path) -> None:
    """The author sees which file is wrong and its real size, without a traceback."""
    root = tmp_path / "repository"
    (root / "art").mkdir(parents=True)
    (root / "screenshots").mkdir()
    (root / "screenshots" / "ok.png").write_bytes(png(72, 16))
    (root / "art" / "chess-clock-icon-source.png").write_bytes(png(1254, 1254))
    manifest = {
        "sourcecode": {"location": {"subdir": "."}},
        "description": "A small clock for the BUSY Bar that shows the time and the date.",
        "changelog": "Initial release",
        "screenshots": ["screenshots/ok.png", "art/chess-clock-icon-source.png"],
    }
    with pytest.raises(CheckError) as error:
        resolve_manifest(manifest, root)
    assert error.value.id == "screenshot_png"
    assert error.value.detail == (
        "art/chess-clock-icon-source.png is 1254×1254, expected 72×16"
    )
    comment = render_comment([outcome(error.value.id, "fail", error.value.detail)])
    assert "art/chess-clock-icon-source.png is 1254×1254, expected 72×16" in comment
    assert "Traceback" not in comment and "ValueError" not in comment


def test_app_id_match(tmp_path: Path) -> None:
    """Accept a folder equal to the app id, reject a different or missing one."""
    from tools.lib_fetch import check_app_id

    (tmp_path / "src" / "appmeta").mkdir(parents=True)
    meta = tmp_path / "src" / "appmeta" / "manifest.json"
    meta.write_text('{"id": "app.busy.clock"}')
    check_app_id(tmp_path, "app.busy.clock")
    with pytest.raises(CheckError, match="app_id_match") as error:
        check_app_id(tmp_path, "imalkav.clock")
    assert "`applications/imalkav.clock/`" in error.value.detail
    assert "`app.busy.clock`" in error.value.detail
    meta.unlink()
    with pytest.raises(CheckError) as missing:
        check_app_id(tmp_path, "app.busy.clock")
    assert missing.value.id == "app_id_match"
    assert "src/appmeta/manifest.json" in missing.value.detail


def test_app_id_without_src(tmp_path: Path) -> None:
    """An app without `src/`, as busy-cli builds it, is read from `appmeta/` directly."""
    from tools.lib_fetch import check_app_id

    (tmp_path / "appmeta").mkdir()
    (tmp_path / "appmeta" / "manifest.json").write_text('{"id": "app.busy.clock"}')
    assert check_app_id(tmp_path, "app.busy.clock") == tmp_path
    with pytest.raises(CheckError, match="app_id_match") as error:
        check_app_id(tmp_path, "imalkav.clock")
    assert "but `appmeta/manifest.json` has `app.busy.clock`" in error.value.detail


def test_app_id_src_comes_first(tmp_path: Path) -> None:
    """With both layouts present, `src/` wins, as in busy-cli."""
    from tools.lib_fetch import check_app_id

    for folder, app_id in (("src/appmeta", "app.busy.clock"), ("appmeta", "other.id")):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "manifest.json").write_text(f'{{"id": "{app_id}"}}')
    assert check_app_id(tmp_path, "app.busy.clock") == tmp_path / "src"


@pytest.mark.parametrize("layout", ["src/appmeta", "appmeta"])
def test_app_id_manifest_link_is_rejected(tmp_path: Path, layout: str) -> None:
    """A linked manifest is refused in either layout; a valid root one is no fallback."""
    from tools.lib_fetch import check_app_id

    app = tmp_path / "app"
    (app / "appmeta").mkdir(parents=True)
    (app / "appmeta" / "manifest.json").write_text('{"id": "app.busy.clock"}')
    (tmp_path / "real.json").write_text('{"id": "app.busy.clock"}')
    (app / layout).mkdir(parents=True, exist_ok=True)
    link = app / layout / "manifest.json"
    link.unlink(missing_ok=True)
    link.symlink_to(tmp_path / "real.json")
    with pytest.raises(CheckError, match="app_id_match") as error:
        check_app_id(app, "app.busy.clock")
    assert f"`{layout}/manifest.json` is a link" in error.value.detail


def test_app_id_folder_link_outside_is_rejected(tmp_path: Path) -> None:
    """An `appmeta` folder that links outside the app folder is not read."""
    from tools.lib_fetch import check_app_id

    (tmp_path / "outside" / "appmeta").mkdir(parents=True)
    (tmp_path / "outside" / "appmeta" / "manifest.json").write_text(
        '{"id": "app.busy.clock"}'
    )
    app = tmp_path / "app"
    app.mkdir()
    (app / "appmeta").symlink_to(tmp_path / "outside" / "appmeta")
    with pytest.raises(CheckError, match="app_id_match") as error:
        check_app_id(app, "app.busy.clock")
    assert "leads outside the app folder" in error.value.detail


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        ("[" * 4096, "is not a readable JSON file"),
        ('{"id": "app.busy.clock"}' + " " * 4096, "is larger than 4 KiB"),
    ],
)
def test_app_id_unusable_manifest(tmp_path: Path, content: str, detail: str) -> None:
    """Deep nesting and an oversized file name their cause instead of a traceback."""
    from tools.lib_fetch import check_app_id

    (tmp_path / "appmeta").mkdir()
    (tmp_path / "appmeta" / "manifest.json").write_text(content)
    with pytest.raises(CheckError, match="app_id_match") as error:
        check_app_id(tmp_path, "app.busy.clock")
    assert detail in error.value.detail


def test_back_screenshots_are_optional_and_checked(tmp_path: Path, make_png) -> None:
    """Valid 160×80 files resolve; wrong size, non-PNG and more than 10 name their check."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "front.png").write_bytes(make_png(72, 16))
    (root / "back.png").write_bytes(make_png(160, 80))
    (root / "wrong.png").write_bytes(make_png(72, 16))
    (root / "text.png").write_bytes(b"not a png")
    base = {
        "sourcecode": {"location": {"subdir": "."}},
        "description": "A small clock for the BUSY Bar that shows the time and the date.",
        "changelog": "Initial release",
        "screenshots": ["front.png"],
    }
    assert "back_screenshots" not in resolve_manifest(base, root)
    resolved = resolve_manifest({**base, "back_screenshots": ["back.png"]}, root)
    assert resolved["back_screenshots"] == [str(root / "back.png")]
    for names, check in (
        (["wrong.png"], "back_screenshot_png"),
        (["text.png"], "back_screenshot_png"),
        (["back.png"] * 11, "back_screenshots_count"),
    ):
        with pytest.raises(CheckError) as error:
            resolve_manifest({**base, "back_screenshots": names}, root)
        assert error.value.id == check
    with pytest.raises(CheckError) as error:
        resolve_manifest({**base, "back_screenshots": ["wrong.png"]}, root)
    assert "wrong.png is 72×16, expected 160×80" in error.value.detail


def test_text_lengths_are_checked_after_the_file_is_read(tmp_path: Path) -> None:
    """A short `@README.md` fails with the real length; the limits are 50–4000 / 10–4000."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("too short")
    base = {
        "sourcecode": {"location": {"subdir": "."}},
        "description": "@README.md",
        "changelog": "Initial release",
        "screenshots": [],
    }
    with pytest.raises(CheckError) as error:
        resolve_manifest(base, root)
    assert error.value.id == "text_resolve"
    assert error.value.detail == "description is 9 characters, expected 50–4000"
    (root / "README.md").write_text("x" * 4001)
    with pytest.raises(CheckError, match="4001 characters"):
        resolve_manifest(base, root)
    (root / "README.md").write_text("x" * 50)
    with pytest.raises(CheckError, match="changelog is 9 characters"):
        resolve_manifest({**base, "changelog": "too short"}, root)
