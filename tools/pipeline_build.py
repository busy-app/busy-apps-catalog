"""Assemble an immutable CI build output from a pinned Git repository."""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from tools.check_png import (
    BACK_SCREENSHOT_FINAL,
    ICON_BACK,
    ICON_FRONT,
    SCREENSHOT_FINAL,
    validate_png,
)
from tools.check_registry import CheckError
from tools.lib_build import run_build, validate_main_script
from tools.lib_fetch import (
    APPMETA,
    check_app_id,
    fetch_checkout,
    inside,
    resolve_manifest,
    source_directory,
    source_file,
)
from tools.lib_license import check_license
from tools.lib_package import create_package, resolve_built_root
from tools.lib_png import normalize_png


def source_archive(checkout: Path, subdir: str, destination: Path) -> None:
    """
    Archive the committed source tree without untracked build output.

    The supplied subdirectory has already been checked inside the checkout.
    """
    tree = "HEAD" if subdir == "." else f"HEAD:{subdir}"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    with tempfile.TemporaryFile() as raw:
        subprocess.run(
            ["git", "-C", str(checkout), "archive", "--format=tar", tree],
            stdout=raw,
            check=True,
            timeout=120,
            env=environment,
        )
        if raw.tell() > 100 * 1024 * 1024:
            raise CheckError("source_archive_size")
        raw.seek(0)
        with (
            destination.open("wb") as output,
            gzip.GzipFile(
                fileobj=output, mode="wb", filename="", mtime=0
            ) as compressed,
        ):
            shutil.copyfileobj(raw, compressed, length=1024 * 1024)


def copy_image(
    source: Path, target: Path, rules: tuple[tuple[int, int], str, int], label: str
) -> None:
    """
    Copy an image into the build output, check it and rewrite it as a clean PNG.

    The check runs on the copy right before normalization, so a file the build
    swapped after the early manifest checks is still held to the PNG-only rules.
    """
    dimensions, check_id, _ = rules
    shutil.copyfile(source, target)
    try:
        validate_png(target, *rules)
    except CheckError as error:
        raise CheckError(error.id, f"{label} {error.detail}") from None
    try:
        normalize_png(target, dimensions)
    except ValueError as error:
        raise CheckError(
            check_id, f"{label} could not be normalized: {error}"
        ) from None


ICON_SOURCES = (
    ("icon_front", "icon_front_8x8.png"),
    ("icon_back", "icon_back_11x11.png"),
)
BUILT_ICONS = (
    ("appmeta/icon_front_8x8.png", "icon.png", ICON_FRONT),
    ("appmeta/icon_back_11x11.png", "icon-back.png", ICON_BACK),
)


def author_icon_paths(checkout: Path, root: Path, app: Path) -> dict[str, str]:
    """
    Icons the author kept in `app/appmeta/`, as paths inside the checkout.

    `app` is the folder `check_app_id` chose; an icon reached through a link or
    leading outside `root`, the folder the build mounts, is left out.
    """
    icons = {}
    for kind, name in ICON_SOURCES:
        candidate = app / APPMETA / name
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and inside(candidate, root)
        ):
            icons[kind] = str(candidate.relative_to(checkout.resolve()).as_posix())
    return icons


def copy_screenshots(paths: list, media: Path, pattern: str, rules: tuple) -> list[str]:
    """Copy screenshots under neutral names (`pattern` has one `{}`), each re-checked."""
    names = []
    for index, path in enumerate(paths):
        name = pattern.format(index + 1)
        copy_image(path, media / name, rules, name)
        names.append(name)
    return names


def copy_built_icons(built: Path, media: Path) -> None:
    """Copy the icons the build produced, if any, as clean PNGs."""
    for source_name, target_name, rules in BUILT_ICONS:
        if (built / source_name).is_file():
            icon = source_file(built, source_name)
            copy_image(icon, media / target_name, rules, f"`{source_name}`")


def build_package(
    source: Path, app_id: str, output: Path, work: Path
) -> tuple[Path, str]:
    """Run the build, pack `<id>/` and check its scripts; return the built root and toolchain."""
    home = work / "home"
    home.mkdir()
    toolchain = run_build(source, home)
    built = resolve_built_root(source / "dist", app_id, work / "built")
    create_package(source / "dist", app_id, output / "package.tgz", root=built)
    validate_main_script(output / "package.tgz", app_id)
    return built, toolchain


def assemble(manifest_path: Path, app_id: str, output: Path, work: Path) -> None:
    """
    Fetch sources, resolve manifest references, build and collect the artifacts.

    This scenario runs only in a disposable, unprivileged CI job without secrets.
    """
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    location = manifest["sourcecode"]["location"]
    checkout = work / "source"
    output.mkdir(parents=True, exist_ok=False)
    fetch_checkout(location["origin"], location["commit_sha"], checkout)
    subdir = location.get("subdir", ".")
    source = source_directory(checkout, subdir)
    app = check_app_id(source, app_id)
    spdx = check_license(checkout, source)
    (output / "license.json").write_text(json.dumps({"spdx": spdx}), encoding="utf-8")
    resolved = resolve_manifest(manifest, checkout)
    icons = author_icon_paths(checkout, source, app)
    (output / "media-sources.json").write_text(json.dumps(icons), encoding="utf-8")
    source_archive(checkout, subdir, output / "sources.tar.gz")
    built, toolchain = build_package(source, app_id, output, work)
    appmeta = json.loads(
        source_file(built, "appmeta/manifest.json").read_text(encoding="utf-8")
    )
    media = output / "media"
    media.mkdir()
    copied = copy_screenshots(
        resolved["screenshots"], media, "screenshot-{}.png", SCREENSHOT_FINAL
    )
    copied_back = copy_screenshots(
        resolved.get("back_screenshots", []),
        media,
        "back-screenshot-{}.png",
        BACK_SCREENSHOT_FINAL,
    )
    copy_built_icons(built, media)
    resolved["screenshots"] = copied
    if copied_back:
        resolved["back_screenshots"] = copied_back
    resolved["appmeta"] = appmeta
    resolved["toolchain"] = toolchain
    (output / "manifest.resolved.json").write_text(
        json.dumps(resolved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    """
    Assemble a build output in a disposable temporary working directory.

    On failure the process exits non-zero and no publishing step is invoked.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("app_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="catalog-ci-") as directory:
        try:
            assemble(args.manifest, args.app_id, args.output, Path(directory))
        except CheckError as error:
            Path("build-error.json").write_text(
                json.dumps({"id": error.id, "detail": error.detail[:2000]}),
                encoding="utf-8",
            )
            print(f"Check failed: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
