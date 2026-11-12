"""Create firmware-compatible packages from an isolated build directory."""

import argparse
import gzip
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from tools.check_package import check_package
from tools.check_registry import CheckError
from tools.lib_app_id import APP_ID
from tools.lib_tar import MAX_ENTRIES, is_plain_entry, path_is_safe

MAX_BUILD_BYTES = 100 * 1024 * 1024


def extract_built_archive(archive: Path, app_id: str, destination: Path) -> Path:
    """
    Safely unpack `dist/<id>.tgz`, written by newer busy-cli, into an empty directory.

    Only regular files and directories under `<id>/` are accepted, within the same
    size and entry limits as the package checks. Returns `destination/<id>`.
    """
    total = count = 0
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                count += 1
                if count > MAX_ENTRIES:
                    raise CheckError("archive_entries", f"`{archive.name}`")
                if not path_is_safe(member.name, app_id):
                    raise CheckError(
                        "package_path", f"`{member.name}` in `{archive.name}`"
                    )
                if not is_plain_entry(member):
                    raise CheckError("archive_entry", f"`{member.name}`")
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.size
                if total > MAX_BUILD_BYTES:
                    raise CheckError("unpacked_size", f"`{archive.name}`")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise CheckError("archive_entry", f"`{member.name}`")
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (tarfile.TarError, EOFError, OSError, UnicodeError):
        raise CheckError("archive_invalid", f"`{archive.name}`") from None
    return destination / app_id


def resolve_built_root(dist: Path, app_id: str, scratch: Path) -> Path:
    """
    Find the built application as `dist/<id>/` or, from newer busy-cli, `dist/<id>.tgz`.

    The archive is unpacked into the empty `scratch` directory; with neither output
    the detail lists up to 10 names found in `dist/`.
    """
    directory = dist / app_id
    if directory.is_dir() and not directory.is_symlink():
        return directory
    archive = dist / f"{app_id}.tgz"
    if archive.is_file() and not archive.is_symlink():
        scratch.mkdir(parents=True, exist_ok=True)
        return extract_built_archive(archive, app_id, scratch)
    names = sorted(entry.name for entry in dist.iterdir()) if dist.is_dir() else []
    found = ", ".join(f"`{name}`" for name in names[:10]) or "nothing"
    raise CheckError(
        "build",
        f"busy-cli finished but produced neither `dist/{app_id}/` nor "
        f"`dist/{app_id}.tgz`; found: {found}",
    )


def checked_entries(root: Path) -> list[Path]:
    """Every file and directory under `root`; links, special files and huge trees are refused."""
    entries = sorted(root.rglob("*"))
    total = 0
    for entry in entries:
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise CheckError("archive_entry", "in the build output")
        if entry.is_file():
            total += entry.stat().st_size
            if total > MAX_BUILD_BYTES:
                raise CheckError("unpacked_size", "in the build output")
    return entries


def write_archive(target: Path, root: Path, entries: list[Path], app_id: str) -> None:
    """Write a deterministic USTAR `.tgz`: no owners, no times, fixed order."""
    with (
        target.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as tar,
    ):
        for entry in [root, *entries]:
            name = str(Path(app_id) / entry.relative_to(root))
            info = tar.gettarinfo(str(entry), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            if entry.is_file():
                with entry.open("rb") as file:
                    tar.addfile(info, file)
            else:
                tar.addfile(info)


def create_package(
    build_dir: Path, app_id: str, destination: Path, root: Path | None = None
) -> None:
    """
    Create a deterministic USTAR package from a single built application.

    Reject symlinks and oversized input before writing the output archive.
    `root` is the already resolved application directory, `build_dir/<id>` by default.
    """
    if not APP_ID.fullmatch(app_id):
        raise CheckError("invalid_id")
    if root is None:
        root = resolve_built_root(build_dir, app_id, build_dir / ".unpacked")
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package output must be outside the built application")
    entries = checked_entries(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".package-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        write_archive(temporary, root, entries, app_id)
        findings = check_package(temporary, app_id)
        if findings:
            raise CheckError(findings[0]["id"], findings[0].get("detail", ""))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """
    Build an archive and report actionable validation errors to the caller.

    The input is the already-built output, not source code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_dir", type=Path)
    parser.add_argument("app_id")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        create_package(args.build_dir, args.app_id, args.destination)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"Package creation failed: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
