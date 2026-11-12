"""Tests for composing a CI build output from isolated build output."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tools.check_registry import CheckError

MIT_TEXT = (Path(__file__).parent / "licenses" / "MIT.txt").read_text()
from tools.pipeline_build import assemble, author_icon_paths


def run_assemble(
    tmp_path: Path,
    png,
    front: bytes | None = None,
    back: bytes | None = None,
    screenshot: bytes | None = None,
    back_shot: bytes | None = None,
    swap_back: bytes | None = None,
    layout: str = "src",
) -> Path:
    """
    Combine prepared build output, source archive and resolved metadata.

    External Git, package installation and app execution are replaced by fakes;
    the built app gets the given icons (a missing one is simply absent).
    """
    front = png(8, 8) if front is None else front
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "sourcecode": {
                    "location": {
                        "origin": "https://github.com/example/app",
                        "commit_sha": "a" * 40,
                    }
                },
                "description": "A small clock for the BUSY Bar that shows the time and the date.",
                "changelog": "Initial release",
                "screenshots": ["screen.png"],
            }
        )
    )
    work = tmp_path / "work"
    work.mkdir()

    def fake_fetch(origin: str, sha: str, target: Path) -> None:
        """
        Create a safe checked-out application fixture.

        No network access or code execution occurs.
        """
        built = target / "dist" / "app.busy.clock"
        (built / "appmeta").mkdir(parents=True)
        (built / "scripts").mkdir()
        (built / "appmeta" / "manifest.json").write_text(
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
        (built / "scripts" / "main.js").write_text("// test")
        (target / "LICENSE").write_text(MIT_TEXT)
        if front != b"":
            (built / "appmeta" / "icon_front_8x8.png").write_bytes(front)
        if back is not None:
            (built / "appmeta" / "icon_back_11x11.png").write_bytes(back)
        (target / "screen.png").write_bytes(png(72, 16))
        if back_shot is not None:
            (target / "back.png").write_bytes(back_shot)
        appmeta = target / layout / "appmeta"
        appmeta.mkdir(parents=True)
        if front != b"":
            (appmeta / "icon_front_8x8.png").write_bytes(front)
        (appmeta / "manifest.json").write_text('{"id": "app.busy.clock"}')

    def fake_archive(checkout: Path, subdir: str, destination: Path) -> None:
        """
        Write a placeholder archive for the orchestration check.

        Actual Git archive behavior is tested separately.
        """
        destination.write_bytes(b"archive")

    def fake_resolve(data: dict, checkout: Path) -> dict:
        """
        Return a resolved screenshot in the temporary source directory.

        Individual path and PNG validation are tested by the fetch tests.
        """
        extra = (
            {"back_screenshots": [str(checkout / "back.png")]}
            if back_shot is not None
            else {}
        )
        return {**data, "screenshots": [str(checkout / "screen.png")], **extra}

    def fake_build(source: Path, home: Path) -> str:
        """The build may rewrite files in the mounted source tree, screenshots included."""
        if screenshot is not None:
            (source / "screen.png").write_bytes(screenshot)
        if swap_back is not None:
            (source / "back.png").write_bytes(swap_back)
        return "0.2.1"

    with (
        patch("tools.pipeline_build.fetch_checkout", side_effect=fake_fetch),
        patch("tools.pipeline_build.source_archive", side_effect=fake_archive),
        patch("tools.pipeline_build.resolve_manifest", side_effect=fake_resolve),
        patch("tools.pipeline_build.run_build", side_effect=fake_build),
        patch("tools.pipeline_build.validate_main_script") as validate_main_script,
    ):
        assemble(manifest, "app.busy.clock", tmp_path / "build", work)
    validate_main_script.assert_called_once_with(
        tmp_path / "build" / "package.tgz", "app.busy.clock"
    )
    return tmp_path / "build"


def test_assemble_build(tmp_path: Path, make_png) -> None:
    """The build output has the package, sources, resolved manifest and normalized media."""
    run_assemble(tmp_path, make_png)
    result = json.loads((tmp_path / "build" / "manifest.resolved.json").read_text())
    assert result["toolchain"] == "0.2.1"
    assert result["screenshots"] == ["screenshot-1.png"]
    assert (tmp_path / "build" / "package.tgz").is_file()
    assert (tmp_path / "build" / "sources.tar.gz").is_file()
    media = sorted(path.name for path in (tmp_path / "build" / "media").iterdir())
    assert media == ["icon.png", "screenshot-1.png"]


def test_front_icon_is_optional(tmp_path: Path, make_png) -> None:
    """No front icon is not a build error: the build output simply has no `media/icon.png`."""
    run_assemble(tmp_path, make_png, front=b"")
    media = sorted(path.name for path in (tmp_path / "build" / "media").iterdir())
    assert media == ["screenshot-1.png"]


@pytest.mark.parametrize("kind", ["not a png", "wrong size", "no palette", "16 bit"])
def test_bad_front_icon_is_icon_png(tmp_path: Path, make_png, kind: str) -> None:
    """A present but invalid front icon fails with `icon_png`."""
    front = {
        "not a png": b"not a png",
        "wrong size": make_png(9, 9),
        "no palette": make_png(8, 8, color=3),
        "16 bit": make_png(8, 8, depth=16),
    }[kind]
    with pytest.raises(CheckError) as error:
        run_assemble(tmp_path, make_png, front=front)
    assert error.value.id == "icon_png"


def test_pillow_failures_are_icon_png(tmp_path: Path, make_png) -> None:
    """Malformed ancillary chunks (valid CRC) that make Pillow raise still give `icon_png`."""
    for late in ((b"gAMA", b""), (b"iCCP", b""), (b"zTXt", b"k\x00\x01x")):
        front = make_png(8, 8, late=(late,))
        directory = tmp_path / late[0].decode()
        directory.mkdir()
        with pytest.raises(CheckError) as error:
            run_assemble(directory, make_png, front=front)
        assert error.value.id == "icon_png", late
        assert "normalized" in error.value.detail


def test_palette_index_outside_palette_is_rejected(tmp_path: Path, make_png) -> None:
    """A palette image using a color it does not define is refused, not rendered black."""
    import struct
    import zlib

    header = struct.pack(">IIBBBBB", 8, 8, 8, 3, 0, 0, 0)
    rows = (b"\0" + b"\x05" * 8) * 8
    front = (
        b"\x89PNG\r\n\x1a\n"
        + make_png.chunk(b"IHDR", header)
        + make_png.chunk(b"PLTE", b"\0\0\0\xff\xff\xff")
        + make_png.chunk(b"IDAT", zlib.compress(rows))
        + make_png.chunk(b"IEND", b"")
    )
    with pytest.raises(CheckError) as error:
        run_assemble(tmp_path, make_png, front=front)
    assert error.value.id == "icon_png" and "palette" in error.value.detail


def test_screenshot_swapped_by_the_build_is_rejected(tmp_path: Path, make_png) -> None:
    """The check runs after the build: a 72×16 non-PNG put in place of a screenshot fails."""
    with pytest.raises(CheckError) as error:
        run_assemble(tmp_path, make_png, screenshot=b"GIF89a" + b"\0" * 20)
    assert error.value.id == "screenshot_final"


def test_back_icon_is_optional_but_checked(tmp_path: Path, make_png) -> None:
    """No back icon is fine; a broken one fails; a good one is published."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    run_assemble(tmp_path / "a", make_png, back=make_png(11, 11))
    assert (tmp_path / "a" / "build" / "media" / "icon-back.png").is_file()
    with pytest.raises(CheckError) as error:
        run_assemble(tmp_path / "b", make_png, back=make_png(8, 8))
    assert error.value.id == "icon_png"


def test_package_keeps_the_authors_icons(tmp_path: Path, make_png) -> None:
    """Normalization touches only media copies: icons inside `package.tgz` are as built."""
    import tarfile

    front = make_png(8, 8)
    build_dir = run_assemble(tmp_path, make_png, front=front)
    with tarfile.open(build_dir / "package.tgz") as archive:
        member = archive.extractfile("app.busy.clock/appmeta/icon_front_8x8.png")
        assert member is not None and member.read() == front


def test_id_mismatch_stops_before_the_build(tmp_path: Path) -> None:
    """A folder that differs from the app's own id fails without running the build."""
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(
        yaml.safe_dump(
            {"sourcecode": {"location": {"origin": "o", "commit_sha": "a" * 40}}}
        )
    )
    work = tmp_path / "work"
    work.mkdir()

    def fake_fetch(origin: str, sha: str, target: Path) -> None:
        (target / "src" / "appmeta").mkdir(parents=True)
        (target / "src" / "appmeta" / "manifest.json").write_text(
            '{"id": "app.busy.chess_clock"}'
        )

    with (
        patch("tools.pipeline_build.fetch_checkout", side_effect=fake_fetch),
        patch("tools.pipeline_build.run_build") as run_build,
        pytest.raises(CheckError) as error,
    ):
        assemble(manifest, "imalkav.chess_clock", tmp_path / "build", work)
    run_build.assert_not_called()
    assert error.value.id == "app_id_match"
    assert error.value.detail == (
        "Folder `applications/imalkav.chess_clock/`, but "
        "`src/appmeta/manifest.json` has `app.busy.chess_clock`"
    )


def test_back_screenshots_are_normalized_into_media(tmp_path: Path, make_png) -> None:
    """A back screenshot becomes `media/back-screenshot-1.png` and is listed in the manifest."""
    bundle = run_assemble(tmp_path, make_png, back_shot=make_png(160, 80))
    assert (bundle / "media" / "back-screenshot-1.png").is_file()
    resolved = json.loads((bundle / "manifest.resolved.json").read_text())
    assert resolved["back_screenshots"] == ["back-screenshot-1.png"]


def test_no_back_screenshots_changes_nothing(tmp_path: Path, make_png) -> None:
    """Without the field there is no key and no extra media."""
    bundle = run_assemble(tmp_path, make_png)
    assert "back_screenshots" not in json.loads(
        (bundle / "manifest.resolved.json").read_text()
    )
    assert not list((bundle / "media").glob("back-*"))


def test_back_screenshot_swapped_by_the_build_is_rejected(
    tmp_path: Path, make_png
) -> None:
    """The post-build check holds back screenshots to the PNG rules too."""
    with pytest.raises(CheckError) as error:
        run_assemble(
            tmp_path,
            make_png,
            back_shot=make_png(160, 80),
            swap_back=b"GIF89a" + b"\0" * 20,
        )
    assert error.value.id == "back_screenshot_final"


def test_license_missing_fails_and_recognised_is_recorded(
    tmp_path: Path, make_png
) -> None:
    """The build records the SPDX id for the report; no license file stops the build."""
    bundle = run_assemble(tmp_path, make_png)
    assert json.loads((bundle / "license.json").read_text()) == {"spdx": "MIT"}


@pytest.mark.parametrize(
    ("layout", "path"),
    [("src", "src/appmeta/icon_front_8x8.png"), (".", "appmeta/icon_front_8x8.png")],
)
def test_icon_paths_in_the_commit_tree_are_recorded(
    tmp_path: Path, make_png, layout: str, path: str
) -> None:
    """The build notes where the icons live in the author's tree, with or without `src/`."""
    bundle = run_assemble(tmp_path, make_png, layout=layout)
    icons = json.loads((bundle / "media-sources.json").read_text())
    assert icons == {"icon_front": path}


def test_icon_paths_skip_a_folder_link_outside(tmp_path: Path) -> None:
    """Icons reached through an `appmeta` link that leaves the app folder are left out."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "icon_back_11x11.png").write_bytes(b"png")
    app = tmp_path / "apps" / "clock"
    app.mkdir(parents=True)
    (app / "appmeta").symlink_to(tmp_path / "outside")
    root = app.resolve()
    assert author_icon_paths(tmp_path, root, root) == {}
