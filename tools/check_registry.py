"""Single registry of catalog checks: what each one means and how to fix it."""

from dataclasses import dataclass

from tools.lib_app_id import EXAMPLES, RULE

STAGES = ("manifest", "build", "package", "catalog", "review")
STAGE_TITLES = {
    "manifest": "Manifest",
    "build": "Build",
    "package": "Package",
    "catalog": "Catalog",
    "review": "Review",
}
RESULTS = ("pass", "fail", "not_run")
ICON_PASS, ICON_FAIL, ICON_WARN = "✅", "❌", "⚠️"


@dataclass(frozen=True)
class Check:
    """One check: a stable id, its stage, whether it blocks and the author text."""

    id: str
    stage: str
    title: str
    required: bool
    problem: str
    fix: str


class CheckError(ValueError):
    """A failed check raised from code that cannot continue, such as a build step."""

    def __init__(self, check_id: str, detail: str = "") -> None:
        """Keep only the registry id and the concrete detail."""
        super().__init__(f"{check_id}: {detail}" if detail else check_id)
        self.id = check_id
        self.detail = detail


# Every check once; the order inside a stage is the run order.
CHECKS: tuple[Check, ...] = (
    # manifest
    Check(
        "manifest_path",
        "manifest",
        "Manifest location",
        True,
        "The manifest is not at applications/<app_id>/manifest.yml, or the application ID is not valid.",
        f"Put the manifest at applications/<app_id>/manifest.yml. {RULE} {EXAMPLES}",
    ),
    Check(
        "id_style",
        "manifest",
        "Application ID style",
        False,
        "The application ID does not start with a domain-like part.",
        f"Recommended: a reverse-domain ID (Java package style), {EXAMPLES}",
    ),
    Check(
        "pr_scope",
        "manifest",
        "Changed files",
        True,
        "PR must change exactly one application manifest.",
        "Keep only `applications/<id>/manifest.yml` in this PR; other changes are made by maintainers.",
    ),
    Check(
        "app_id_format",
        "manifest",
        "Application ID format",
        True,
        "The application ID in the folder name is not valid.",
        f"{RULE} {EXAMPLES}",
    ),
    Check(
        "removal_not_owner",
        "manifest",
        "Who removes the app",
        True,
        "Only the app's author or catalog maintainers can remove an app, and this pull request's author is neither.",
        "Ask the maintainers to remove it: open an issue in this repository and name the app.",
    ),
    Check(
        "removal_not_published",
        "manifest",
        "App in the catalog",
        False,
        "The app is not in the catalog: it was never published or is already removed.",
        "Nothing else to do: merging only cleans the repository.",
    ),
    Check(
        "manifest_readable",
        "manifest",
        "Manifest syntax",
        True,
        "The manifest is not readable YAML.",
        "Fix the YAML syntax of manifest.yml at the line named in the details.",
    ),
    Check(
        "manifest_schema",
        "manifest",
        "Manifest fields",
        True,
        "A manifest field is missing or has a wrong value.",
        "In manifest.yml, add the missing field or correct the value of the field named in the details.",
    ),
    Check(
        "manifest_references",
        "manifest",
        "Manifest paths",
        True,
        "A path or file reference in the manifest is not allowed.",
        "Use relative paths inside the application directory; `@file` is allowed only in description.",
    ),
    Check(
        "changelog_text",
        "manifest",
        "Changelog",
        True,
        "The changelog is a file reference, or shorter than 10 or longer than 4000 characters.",
        "Write the changelog in `manifest.yml` as inline text of 10 to 4000 characters.",
    ),
    Check(
        "official_namespace",
        "manifest",
        "Official namespace",
        False,
        "The ID starts with `app.busy.`, which official BUSY apps use, but the repository is not from the busy-app organization.",
        "Check this is not an impersonation of an official app; otherwise use an ID of your own (for example `io.github.<your-user>.<app>`).",
    ),
    Check(
        "category_public",
        "manifest",
        "Category",
        True,
        "The category is not in the public category list.",
        "Set `category` in manifest.yml to one of the allowed categories named in the details.",
    ),
    Check(
        "category_alias",
        "manifest",
        "Category name",
        False,
        "The category is an alias of another category.",
        "Use the main category name in `category`.",
    ),
    Check(
        "category_offline",
        "manifest",
        "Category list",
        False,
        "The public category list could not be loaded.",
        "Nothing to do: the local copy of the categories was used.",
    ),
    # build
    Check(
        "source_origin",
        "build",
        "Source repository",
        True,
        "The source is not a GitHub HTTPS repository or the commit is not a full SHA.",
        "Set `sourcecode.location.origin` to https://github.com/<owner>/<repo> and `commit_sha` to the full 40-character SHA.",
    ),
    Check(
        "source_commit",
        "build",
        "Source commit",
        True,
        "No commit found.",
        "Check that the corresponding commit is available in your repository and that `commit_sha` in the manifest is the full 40-character SHA.",
    ),
    Check(
        "source_subdir",
        "build",
        "Source directory",
        True,
        "The source subdirectory is missing or outside the repository.",
        "Set `sourcecode.location.subdir` to a directory that exists at that commit.",
    ),
    Check(
        "app_id_match",
        "build",
        "Application ID",
        True,
        "Application ID doesn't match.",
        (
            "Rename the folder to match the `id` in your app's "
            f"appmeta/manifest.json, or change that `id` and push a new commit. {RULE} {EXAMPLES}"
        ),
    ),
    Check(
        "license",
        "build",
        "Open source license",
        True,
        "The repository has no license file at the submitted commit.",
        "Add a LICENSE file with an open source license (for example MIT or GPL) to your repository, and submit a commit that contains it.",
    ),
    Check(
        "license_unrecognised",
        "build",
        "License text",
        False,
        "The license text was not recognised, the moderator will check it.",
        "Use the unmodified text of a standard open source license, for example from choosealicense.com.",
    ),
    Check(
        "referenced_file",
        "build",
        "Referenced files",
        True,
        "A file referenced from the manifest is missing or outside the application directory.",
        "Commit the file at the path used in the manifest, inside the application directory.",
    ),
    Check(
        "text_resolve",
        "build",
        "Description and changelog",
        True,
        "A description or changelog is too short or too long.",
        "Keep `description` between 50 and 4000 characters (after `@file` is read) and `changelog` between 10 and 4000.",
    ),
    Check(
        "markdown",
        "build",
        "Markdown",
        True,
        "The description or changelog uses unsupported Markdown.",
        "In `description` and `changelog` use only level 1–2 headings, bold, italic, lists and links; remove the element named in the details.",
    ),
    Check(
        "screenshots_count",
        "build",
        "Screenshot count",
        True,
        "There must be 1–10 screenshots.",
        "List 1–10 PNG files under `screenshots`.",
    ),
    Check(
        "back_screenshots_count",
        "build",
        "Back screenshot count",
        True,
        "There can be at most 10 back screenshots.",
        "List 0–10 PNG files under `back_screenshots`, or remove the field.",
    ),
    Check(
        "screenshot_png",
        "build",
        "Screenshot files",
        True,
        "A screenshot is not a valid 72×16 PNG.",
        "Replace the listed file with a valid PNG of exactly 72×16 pixels.",
    ),
    Check(
        "source_archive_size",
        "build",
        "Source size",
        True,
        "The committed source is larger than 100 MiB.",
        "Remove large files from the repository at that commit.",
    ),
    Check(
        "back_screenshot_png",
        "build",
        "Back screenshot files",
        True,
        "A back screenshot is not a valid 160×80 PNG.",
        "Replace the listed file with a valid 8-bit PNG of exactly 160×80 pixels.",
    ),
    Check(
        "build_lockfile",
        "build",
        "Lock file",
        True,
        "The lock file or package manager version is not pinned.",
        "Commit exactly one of package-lock.json or pnpm-lock.yaml with a pinned @busy-app/cli (and `packageManager` for pnpm).",
    ),
    Check(
        "build",
        "build",
        "Build",
        True,
        "The build failed.",
        "Check that the `id` in appmeta/manifest.json is the folder name and that `busy-cli build` succeeds locally with the pinned toolchain.",
    ),
    Check(
        "script_main",
        "build",
        "Entry script",
        True,
        "scripts/main.js is missing, unreadable or too large.",
        "Make the build output scripts/main.js of at most 256000 bytes.",
    ),
    Check(
        "script_imports",
        "build",
        "Script imports",
        True,
        "A script has a syntax error or imports a file that is not in the package.",
        "Fix the syntax and use only relative imports of files inside scripts/.",
    ),
    Check(
        "screenshot_final",
        "build",
        "Screenshot files after the build",
        True,
        "A screenshot is no longer a valid 72×16 PNG after the build, or could not be re-saved.",
        "Make sure the build doesn't change your screenshots and that they are 8-bit PNGs of exactly 72×16 pixels.",
    ),
    Check(
        "back_screenshot_final",
        "build",
        "Back screenshot files after the build",
        True,
        "A back screenshot is no longer a valid 160×80 PNG after the build, or could not be re-saved.",
        "Make sure the build doesn't change your back screenshots and that they are 8-bit PNGs of exactly 160×80 pixels.",
    ),
    Check(
        "icon_png",
        "build",
        "Application icons",
        True,
        "The application icon is not a valid 8×8 PNG (or the back icon is not a valid 11×11 PNG).",
        "Replace `appmeta/icon_front_8x8.png` in your app with a valid 8-bit PNG of exactly 8×8 pixels (`appmeta/icon_back_11x11.png`, if present, must be 11×11).",
    ),
    Check(
        "icon_missing",
        "build",
        "Application icon",
        False,
        "The application has no front icon, so the catalog shows a default one.",
        "Add `appmeta/icon_front_8x8.png` to your app: a PNG of exactly 8×8 pixels.",
    ),
    # package
    Check(
        "package_missing",
        "package",
        "Package built",
        True,
        "The build did not produce a readable package.",
        "Re-run the checks. If it fails again, ask the maintainers.",
    ),
    Check(
        "invalid_id",
        "package",
        "Application ID",
        True,
        "The application ID is not valid.",
        f"{RULE} {EXAMPLES}",
    ),
    Check(
        "package_size",
        "package",
        "Package size",
        True,
        "The package is too large.",
        "Reduce the compressed package to at most 100 MiB.",
    ),
    Check(
        "unpacked_size",
        "package",
        "Unpacked size",
        True,
        "The unpacked package is too large.",
        "Reduce the unpacked archive to at most 128 MiB.",
    ),
    Check(
        "archive_entries",
        "package",
        "Archive entries",
        True,
        "The package has too many files.",
        "Reduce the archive to at most 10000 entries.",
    ),
    Check(
        "archive_invalid",
        "package",
        "Archive format",
        True,
        "The package is not a readable gzip tar archive.",
        "Provide a readable gzip tar archive.",
    ),
    Check(
        "tar_format",
        "package",
        "Tar format",
        True,
        "The archive is not a plain USTAR archive.",
        "Create a USTAR archive without GNU or PAX headers.",
    ),
    Check(
        "package_path",
        "package",
        "Package paths",
        True,
        "A file is outside the application ID directory.",
        "Place all package files under the application ID directory.",
    ),
    Check(
        "duplicate_path",
        "package",
        "Duplicate paths",
        True,
        "The archive has duplicate paths.",
        "Remove duplicate archive paths.",
    ),
    Check(
        "archive_entry",
        "package",
        "Entry types",
        True,
        "The archive has entries other than files and directories.",
        "Use regular files and directories only.",
    ),
    Check(
        "javascript_size",
        "package",
        "JavaScript size",
        True,
        "A JavaScript file is too large.",
        "Reduce each JavaScript file to at most 256000 bytes.",
    ),
    Check(
        "main_missing",
        "package",
        "Entry script in package",
        True,
        "scripts/main.js is not in the package.",
        "Include scripts/main.js in the package.",
    ),
    Check(
        "manifest_missing",
        "package",
        "App manifest in package",
        True,
        "appmeta/manifest.json is not in the package.",
        "Include appmeta/manifest.json in the package.",
    ),
    Check(
        "manifest_size",
        "package",
        "App manifest size",
        True,
        "appmeta/manifest.json is too large.",
        "Reduce manifest.json to at most 512 bytes.",
    ),
    Check(
        "manifest_json",
        "package",
        "App manifest JSON",
        True,
        "appmeta/manifest.json is not a JSON object.",
        "Provide a valid JSON manifest object.",
    ),
    Check(
        "manifest_field",
        "package",
        "App manifest fields",
        True,
        "A required field is missing in appmeta/manifest.json.",
        "Add the missing field to manifest.json.",
    ),
    Check(
        "manifest_format_version",
        "package",
        "App manifest format",
        True,
        "format_version is not 1.",
        "Set format_version to the integer 1.",
    ),
    Check(
        "manifest_id",
        "package",
        "App manifest ID",
        True,
        "The ID in appmeta/manifest.json differs from the catalog application ID.",
        f"Rename the folder to match the `id` in your app's appmeta/manifest.json, or change that `id` and push a new commit. {RULE} {EXAMPLES}",
    ),
    Check(
        "manifest_name",
        "package",
        "App name",
        True,
        "The application name is empty, longer than 32 characters, or has characters or spaces the design does not allow.",
        "Set `name` in appmeta/manifest.json to 1–32 characters: Latin letters, digits, single spaces between words and ! ? & ' ( ) + , - . / : # $ % @ _",
    ),
    Check(
        "manifest_version",
        "package",
        "App versions",
        True,
        "version or api_version is not numeric semantic versioning.",
        "Use numeric versions like 1.2.3 for version and api_version.",
    ),
    Check(
        "manifest_heap",
        "package",
        "Heap size",
        True,
        "heap_size_kib is out of range.",
        "Set heap_size_kib between 1 and 512.",
    ),
    Check(
        "manifest_debug",
        "package",
        "Debug mode",
        True,
        "Debug mode is enabled.",
        "Disable debug mode in the published app.",
    ),
    # catalog
    Check(
        "version_not_newer",
        "catalog",
        "Version is new",
        True,
        "The version is not higher than the published one.",
        "Increase the application version above the published version.",
    ),
    Check(
        "source_repository_changed",
        "catalog",
        "Same repository",
        True,
        "The update comes from another repository than the published app.",
        "Submit the update from the repository the app was published from.",
    ),
    Check(
        "firmware_too_old",
        "catalog",
        "Firmware API",
        True,
        "api_version is higher than the latest released firmware API.",
        "Set api_version no higher than the latest released API.",
    ),
    Check(
        "catalog_unavailable",
        "catalog",
        "Catalog lookup",
        False,
        "The catalog could not be queried, so the version, repository and firmware checks were skipped.",
        "Nothing to do; the maintainers recheck before publishing.",
    ),
    Check(
        "version_unavailable",
        "catalog",
        "Published version lookup",
        False,
        "The published version and its repository could not be checked.",
        "Nothing to do; the maintainers recheck before publishing.",
    ),
    Check(
        "firmware_unavailable",
        "catalog",
        "Firmware API lookup",
        False,
        "The latest firmware API could not be checked.",
        "Nothing to do; the maintainers recheck before publishing.",
    ),
    # review
    Check(
        "author_attestation",
        "review",
        "Author confirmation",
        True,
        "The PR author did not confirm being the author or having write access.",
        "Tick the author confirmation in the pull request description.",
    ),
    Check(
        "internal_error",
        "review",
        "Checks could not finish",
        True,
        "The catalog checks could not finish.",
        "This is not a problem with your app. Re-run the checks; if it fails again, a maintainer will look at it.",
    ),
)


REGISTRY: dict[str, Check] = {check.id: check for check in CHECKS}


def get(check_id: str) -> Check:
    """Return a registered check; unknown ids are a programming error."""
    return REGISTRY[check_id]


def icon(check_id: str, result: str) -> str:
    """Derive the icon: pass ✅, required fail/not_run ❌, optional fail/not_run ⚠️."""
    if result not in RESULTS:
        raise ValueError(f"Unknown result: {result}")
    if result == "pass":
        return ICON_PASS
    return ICON_FAIL if REGISTRY[check_id].required else ICON_WARN


def blocks(check_id: str, result: str) -> bool:
    """Tell whether a result prevents merging."""
    return icon(check_id, result) == ICON_FAIL


def outcome(check_id: str, result: str = "fail", detail: str = "") -> dict[str, str]:
    """Build one machine-readable result entry for results.json."""
    if check_id not in REGISTRY or result not in RESULTS:
        raise ValueError("Unknown check or result")
    entry = {"id": check_id, "result": result}
    if detail:
        entry["detail"] = detail[:2000]
    return entry
