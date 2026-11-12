"""Screenshots and icons of the author's repository, shown in the Checks comment."""

import html
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

SIZES = {
    "screenshot": (72, 16),
    "back_screenshot": (160, 80),
    "icon_front": (8, 8),
    "icon_back": (11, 11),
}
TITLES = {
    "screenshot": "Screenshots",
    "back_screenshot": "Back screenshots",
    "icon_front": "Icons",
    "icon_back": "Icons",
}
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_ITEMS = 25


def repo_path(subdir: str, path: str) -> str | None:
    """Repository-relative path of a manifest file, or None if it leaves the repository."""
    joined = PurePosixPath(subdir or ".") / path
    if (
        not path
        or "\\" in path
        or ".." in joined.parts
        or ".git" in joined.parts
        or joined.is_absolute()
    ):
        return None
    result = str(PurePosixPath(*joined.parts)) if joined.parts else ""
    return result or None


def build_view(
    manifest: Mapping[str, object], icons: Mapping[str, object] | None = None
) -> dict[str, object] | None:
    """
    Describe the images of a manifest for the report, pinned to the full commit.

    Only the facts needed to build links are kept; the report validates them again.
    """
    location = (manifest.get("sourcecode") or {}).get("location") or {}
    origin = urlsplit(str(location.get("origin", "")))
    parts = [part for part in origin.path.split("/") if part]
    commit = location.get("commit_sha")
    subdir = location.get("subdir", ".")
    if (
        origin.scheme != "https"
        or origin.netloc != "github.com"
        or len(parts) != 2
        or not isinstance(commit, str)
        or not SHA.fullmatch(commit)
        or not isinstance(subdir, str)
    ):
        return None
    items = []
    for kind, field in (
        ("screenshot", "screenshots"),
        ("back_screenshot", "back_screenshots"),
    ):
        paths = manifest.get(field)
        for path in paths if isinstance(paths, list) else []:
            resolved = repo_path(subdir, path) if isinstance(path, str) else None
            if resolved:
                items.append({"kind": kind, "path": resolved})
    for kind in ("icon_front", "icon_back"):
        path = (icons or {}).get(kind)
        if isinstance(path, str) and repo_path(".", path):
            items.append({"kind": kind, "path": path})
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return {"repo": repo, "commit_sha": commit, "items": items}


def parse_view(data: object) -> dict[str, object] | None:
    """Validate a view read from untrusted results; anything odd means no images."""
    if not isinstance(data, dict):
        return None
    repo, commit, items = data.get("repo"), data.get("commit_sha"), data.get("items")
    if (
        not isinstance(repo, str)
        or not REPO.fullmatch(repo)
        or not isinstance(commit, str)
        or not SHA.fullmatch(commit)
        or not isinstance(items, list)
        or len(items) > MAX_ITEMS
    ):
        return None
    clean = []
    for item in items:
        if not isinstance(item, dict):
            return None
        kind, path = item.get("kind"), item.get("path")
        if kind not in SIZES or not isinstance(path, str) or len(path) > 300:
            return None
        if repo_path(".", path) != path:
            return None
        clean.append({"kind": kind, "path": path})
    return {"repo": repo, "commit_sha": commit, "items": clean}


def render_media(view: Mapping[str, object] | None) -> list[str]:
    """
    The Screenshots section: each image at its real size, linking to the pinned file.

    Paths come from the author, so they are URL-encoded and the alt text is escaped.
    """
    if not view or not view["items"]:
        return []
    repo, commit = view["repo"], view["commit_sha"]
    lines = ["", "### Screenshots", ""]
    shown = []
    for item in view["items"]:
        kind, path = item["kind"], item["path"]
        width, height = SIZES[kind]
        encoded = quote(path, safe="/")
        raw = f"https://raw.githubusercontent.com/{repo}/{commit}/{encoded}"
        page = f"https://github.com/{repo}/blob/{commit}/{encoded}"
        alt = html.escape(
            f"{TITLES[kind].removesuffix('s')}: {path}",
            quote=True,
        )
        shown.append(
            f'<a href="{page}"><img src="{raw}" width="{width}" height="{height}" alt="{alt}"></a>'
        )
    lines.append(" ".join(shown))
    return lines
