"""The author's error cases: how each PR is built and which check must go red."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

Edit = Callable[[dict], "dict | str"]


@dataclass(frozen=True)
class Case:
    """
    One deliberately broken submission.

    `expect` are the ids of the checks that must be red, and no others. `detail` is a
    text that must appear in the PR comment (case-insensitive). The PR changes one
    manifest unless `extra_files`, `second_manifest` or `delete_existing` say otherwise.
    """

    name: str
    expect: tuple[str, ...]
    detail: str = ""
    folder: Callable[[str], str] | None = None
    edit: Edit | None = None
    extra_files: tuple[tuple[str, str], ...] = ()
    second_manifest: bool = False
    delete_existing: bool = False
    fixture: str = "valid"
    notes: str = field(default="", compare=False)


def without(key: str) -> Edit:
    """Remove a required field."""
    return lambda manifest: {k: v for k, v in manifest.items() if k != key}


def with_field(key: str, value: object) -> Edit:
    """Add or replace a field."""
    return lambda manifest: {**manifest, key: value}


def in_location(**changes: object) -> Edit:
    """Change fields of `sourcecode.location`."""

    def edit(manifest: dict) -> dict:
        location = {**manifest["sourcecode"]["location"], **changes}
        return {
            **manifest,
            "sourcecode": {**manifest["sourcecode"], "location": location},
        }

    return edit


DEEP_HEADING = "### A heading deeper than the second level is not allowed here"

CASES = (
    Case(
        "scope-extra-file",
        ("pr_scope",),
        "e2e-extra",
        extra_files=(("e2e-extra-file.txt", "scope rejection fixture"),),
    ),
    Case("scope-two-manifests", ("pr_scope",), second_manifest=True),
    Case(
        "scope-delete-and-add",
        ("pr_scope",),
        "e2e-extra",
        extra_files=(("e2e-extra-file.txt", "scope rejection fixture"),),
        delete_existing=True,
        notes="a removal must not carry other changes",
    ),
    Case(
        "id-uppercase",
        ("app_id_format",),
        "ru.malk.chess",
        folder=lambda identifier: "Ru.Malk.Chess_" + identifier.rsplit("_", 1)[1],
    ),
    Case(
        "id-one-dot",
        ("app_id_format",),
        folder=lambda identifier: "imalkav.chess_" + identifier.rsplit("_", 1)[1],
    ),
    Case(
        "id-folder-mismatch",
        ("app_id_match",),
        folder=lambda identifier: identifier + "_x",
        notes="valid folder id that differs from the id in appmeta/manifest.json",
    ),
    Case(
        "yaml-broken",
        ("manifest_readable",),
        "line",
        edit=lambda manifest: "sourcecode: [unclosed\n",
    ),
    Case("missing-field", ("manifest_schema",), "changelog", edit=without("changelog")),
    Case(
        "extra-field",
        ("manifest_schema",),
        "unexpected",
        edit=with_field("unexpected", True),
    ),
    Case(
        "unknown-category",
        ("manifest_schema", "category_public"),
        "no-such-category",
        edit=with_field("category", "no-such-category"),
    ),
    Case(
        "markdown-heading",
        ("markdown",),
        edit=with_field("description", DEEP_HEADING),
    ),
    Case(
        "origin-not-github",
        ("manifest_schema",),
        "origin",
        edit=in_location(origin="https://example.com/someone/repo"),
    ),
    Case(
        "short-sha",
        ("manifest_schema",),
        "commit_sha",
        edit=in_location(commit_sha="abc1234"),
    ),
    Case(
        "repo-missing",
        ("source_commit",),
        "could not be cloned",
        edit=in_location(origin="https://github.com/busy-app/e2e-no-such-repository"),
    ),
    Case(
        "commit-missing",
        ("source_commit",),
        edit=in_location(commit_sha="0" * 40),
    ),
    Case(
        "subdir-missing",
        ("source_subdir",),
        "no-such-subdir",
        edit=in_location(subdir="no-such-subdir"),
    ),
    Case(
        "import-missing",
        ("referenced_file",),
        "MISSING.md",
        edit=with_field("description", "@MISSING.md"),
    ),
    Case(
        "screenshot-file-missing",
        ("referenced_file",),
        "missing.png",
        edit=with_field("screenshots", ["screenshots/missing.png"]),
    ),
    Case(
        "eleven-screenshots",
        ("manifest_schema",),
        "screenshots",
        edit=with_field(
            "screenshots", [f"screenshots/clock-{n}.png" for n in range(11)]
        ),
    ),
    Case(
        "shot-wrong-size",
        ("screenshot_png",),
        "73×16",
        fixture="screenshot-wrong-size",
        notes="needs the screenshot-wrong-size fixture (73×16)",
    ),
    Case(
        "shot-not-png",
        ("screenshot_png",),
        "not a PNG",
        fixture="screenshot-not-png",
        notes="needs the screenshot-not-png fixture",
    ),
    Case(
        "no-lockfile",
        ("build_lockfile",),
        fixture="no-lockfile",
        notes="needs the no-lockfile fixture (no pnpm-lock.yaml)",
    ),
)


def base_manifest(origin: str, sha: str, subdir: str, case_name: str) -> dict:
    """The valid manifest every case starts from."""
    return {
        "sourcecode": {
            "type": "git",
            "location": {"origin": origin, "commit_sha": sha, "subdir": subdir},
        },
        "category": "clocks",
        "short_description": "E2E clock fixture",
        "description": f"E2E error case {case_name}: all other fields of this manifest are valid.",
        "changelog": "Initial version.",
        "screenshots": ["screenshots/clock.png"],
    }


def plan_files(
    case: Case,
    identifier: str,
    manifest: dict,
    existing: str | None = None,
) -> list[dict]:
    """
    The tree entries of the case's PR: `content` is None for a deletion.

    `identifier` is the valid application id of this run; `existing` is the folder
    of a published manifest, needed only by `delete_existing`.
    """
    changed = case.edit(manifest) if case.edit else manifest
    text = changed if isinstance(changed, str) else json.dumps(changed)
    folder = case.folder(identifier) if case.folder else identifier
    entries = [{"path": f"applications/{folder}/manifest.yml", "content": text}]
    if case.second_manifest:
        other = {"path": f"applications/{identifier}_b/manifest.yml", "content": text}
        entries.append(other)
    entries += [{"path": path, "content": body} for path, body in case.extra_files]
    if case.delete_existing:
        if existing is None:
            raise ValueError("delete_existing needs a published manifest folder")
        entries = [
            {"path": f"applications/{existing}/manifest.yml", "content": None},
            *({"path": p, "content": c} for p, c in case.extra_files),
        ]
    return entries
