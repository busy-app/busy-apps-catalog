"""Fetch pinned app sources and resolve referenced text and screenshots."""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from tools.check_manifest import category_aliases
from tools.check_markdown import validate_markdown
from tools.check_png import BACK_SCREENSHOT, SCREENSHOT, validate_png
from tools.check_registry import CheckError

SHA = re.compile(r"^[0-9a-f]{40}$")


def source_directory(root: Path, relative: str) -> Path:
    """
    Resolve an application subdirectory within the checked-out repository.

    Absolute paths, traversal and symlinks escaping the repository are rejected.
    """
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or "\\" in relative
    ):
        raise CheckError("source_subdir", relative)
    directory = (root / relative).resolve()
    if not directory.is_relative_to(root.resolve()) or not directory.is_dir():
        raise CheckError("source_subdir", relative)
    return directory


APPMETA = "appmeta"
APP_META = f"{APPMETA}/manifest.json"
MAX_APP_META = 4096


def app_directory(directory: Path) -> Path | None:
    """
    The folder that holds `appmeta/manifest.json`, chosen the way busy-cli chooses it.

    `src/` comes first, as the template lays an app out; otherwise the directory
    itself, for an app written without `src/`. Any existing entry counts, as with
    busy-cli's `existsSync`, so a broken `src/` is reported instead of skipped.
    busy-cli's rule for a folder named `src` does not apply here: the build mounts
    the app folder at `/work`. Only presence is read.
    """
    for candidate in (directory / "src", directory):
        if (candidate / APP_META).exists():
            return candidate
    return None


def inside(path: Path, directory: Path) -> bool:
    """Whether `path`, with links resolved, stays inside `directory`."""
    return path.resolve().is_relative_to(directory.resolve())


def check_app_id(directory: Path, folder: str) -> Path:
    """
    Compare the app's own `id` with the catalog folder name and return the app folder.

    Nothing from the submitted source is executed; a mismatch stops the build. The
    manifest must be a file inside `directory`, the folder the build mounts.
    """
    app = app_directory(directory)
    if app is None:
        raise CheckError(
            "app_id_match", f"Neither `src/{APP_META}` nor `{APP_META}` exists"
        )
    path = app / APP_META
    shown = path.relative_to(directory).as_posix()
    if path.is_symlink() or not inside(path, directory):
        raise CheckError(
            "app_id_match", f"`{shown}` is a link or leads outside the app folder"
        )
    try:
        too_large = path.stat().st_size > MAX_APP_META
        data = None if too_large else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise CheckError(
            "app_id_match", f"`{shown}` is not a readable JSON file"
        ) from None
    if too_large:
        raise CheckError("app_id_match", f"`{shown}` is larger than 4 KiB")
    found = data.get("id") if isinstance(data, dict) else None
    if found != folder:
        raise CheckError(
            "app_id_match",
            f"Folder `applications/{folder}/`, but `{shown}` has `{found}`",
        )
    return app


def source_file(root: Path, relative: str) -> Path:
    """
    Resolve an existing regular file inside the application directory.

    Traversal and symlinks pointing outside the directory are rejected.
    """
    relative_path = PurePosixPath(relative)
    if (
        not relative
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or ".git" in relative_path.parts
        or "\\" in relative
    ):
        raise CheckError("referenced_file", relative)
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise CheckError("referenced_file", relative)
    return result


def resolve_text(
    root: Path, value: str, limit: int | None, minimum: int = 1, field: str = "text"
) -> str:
    """
    Resolve an inline string or an at-sign text reference.

    Limits are checked after reading the referenced UTF-8 text.
    """
    result = (
        source_file(root, value[1:]).read_text(encoding="utf-8")
        if value.startswith("@")
        else value
    )
    if (
        not result.strip()
        or len(result) < minimum
        or (limit is not None and len(result) > limit)
    ):
        raise CheckError(
            "text_resolve",
            f"{field} is {len(result)} characters, expected {minimum}–{limit}",
        )
    return result


GIT_OPTIONS = (
    "git",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "http.followRedirects=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
)


def check_origin(origin: str, commit: str) -> None:
    """Only `https://github.com/<owner>/<repo>` at a full 40-character commit."""
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or url.netloc != "github.com"
        or url.query
        or url.fragment
        or len([part for part in url.path.split("/") if part]) != 2
        or not SHA.fullmatch(commit)
    ):
        raise CheckError("source_origin", f"{origin} @ {commit}")


def git_environment() -> dict[str, str]:
    """A clean environment: no inherited GIT_* settings, no prompts, no LFS downloads."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    return environment


def checked_out_head(target: Path, commit: str, environment: dict[str, str]) -> str:
    """Detach at `commit` and return the commit git reports as HEAD."""
    subprocess.run(
        [*GIT_OPTIONS, "-C", str(target), "checkout", "--detach", "--force", commit],
        check=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env=environment,
    )
    return subprocess.run(
        [*GIT_OPTIONS, "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def fetch_checkout(origin: str, commit: str, target: Path) -> None:
    """
    Clone an HTTPS GitHub repository and detach at the exact supplied commit.

    Git disallows local and external protocols, prompts and HTTP redirects.
    """
    check_origin(origin, commit)
    if target.exists():
        raise ValueError("Checkout destination already exists")
    environment = git_environment()
    try:
        subprocess.run(
            [*GIT_OPTIONS, "clone", "--no-checkout", "--", origin, str(target)],
            check=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    except subprocess.CalledProcessError:
        raise CheckError(
            "source_commit", f"repository {origin} could not be cloned"
        ) from None
    try:
        head = checked_out_head(target, commit, environment)
    except subprocess.CalledProcessError:
        raise CheckError("source_commit", commit) from None
    if head != commit:
        raise CheckError("source_commit", commit)


def resolve_manifest(manifest: dict, checkout: Path) -> dict:
    """
    Resolve description, changelog and screenshot paths from a checkout.

    This prepares data for later image validation and package construction.
    """
    root = source_directory(
        checkout, manifest["sourcecode"]["location"].get("subdir", ".")
    )
    result = dict(manifest)
    if "category" in result:
        result["category"] = category_aliases().get(
            result["category"], result["category"]
        )
    result["description"] = resolve_text(
        root, manifest["description"], 4000, 50, "description"
    )
    result["changelog"] = resolve_text(
        root, manifest["changelog"], 4000, 10, "changelog"
    )
    for field in ("description", "changelog"):
        violations = validate_markdown(result[field])
        if violations:
            raise CheckError("markdown", f"{field}: {violations[0]}")
    screenshots = manifest["screenshots"]
    if not isinstance(screenshots, list) or not 1 <= len(screenshots) <= 10:
        raise CheckError("screenshots_count", f"{len(screenshots)} screenshots")
    resolved_screenshots = [source_file(root, path) for path in screenshots]
    for screenshot, name in zip(resolved_screenshots, screenshots, strict=True):
        try:
            validate_png(screenshot, *SCREENSHOT)
        except CheckError as error:
            raise CheckError(error.id, f"{name} {error.detail}") from None
    result["screenshots"] = [str(path) for path in resolved_screenshots]
    back_screenshots = manifest.get("back_screenshots", [])
    if not isinstance(back_screenshots, list) or len(back_screenshots) > 10:
        raise CheckError(
            "back_screenshots_count", f"{len(back_screenshots)} back screenshots"
        )
    resolved_back = [source_file(root, path) for path in back_screenshots]
    for screenshot, name in zip(resolved_back, back_screenshots, strict=True):
        try:
            validate_png(screenshot, *BACK_SCREENSHOT)
        except CheckError as error:
            raise CheckError(error.id, f"{name} {error.detail}") from None
    if resolved_back:
        result["back_screenshots"] = [str(path) for path in resolved_back]
    return result
