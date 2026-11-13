"""Render and identify the catalog checks comment for a pull request."""

from collections.abc import Mapping, Sequence
from urllib.parse import quote, urlsplit

from tools.check_registry import REGISTRY, STAGE_TITLES, STAGES, blocks, get, icon
from tools.lib_media import render_media

MARKER = "<!-- catalog-checks -->"


def escape_text(value: str) -> str:
    """
    Render untrusted check text without introducing Markdown or HTML markup.

    Whitespace is collapsed so a finding cannot forge a new comment section.
    """
    value = " ".join(value.split())
    replacements = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "`": "&#96;", "|": "&#124;"}
    return "".join(replacements.get(character, character) for character in value)


HEADLINES = {
    "fail": "❌ Action required",
    "warn": "⚠️ Passed with warnings",
    "pass": "✅ Passed",
}
SKIP_REASONS = {
    "build": "build failed",
    "manifest": "fix the manifest first",
    "internal": "the checks could not finish",
    "removal": "nothing is built when an app is removed",
}
UNCOUNTED = {
    "internal_error",
    "removal_not_owner",
    "removal_not_published",
    "bar_unavailable",
}
REVIEWER_NOTE = (
    "Verify that the PR author is the author of the app or has write access to "
    "its repository."
)


def trusted(value: str) -> str:
    """Escape registry text, which is written by us, only as far as tables need."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
    )


def plural(count: int) -> str:
    """Format a check count."""
    return f"{count} check" + ("" if count == 1 else "s")


def verdict(findings: Sequence[Mapping[str, str]]) -> str:
    """Reduce findings to `fail` (a required check broke), `warn` or `pass`."""
    if any(blocks(item["id"], item["result"]) for item in findings):
        return "fail"
    return "warn" if any(item["result"] != "pass" for item in findings) else "pass"


def stage_order(stage: str) -> list[str]:
    """Ids of the counted checks of a stage, in the registry order."""
    return [
        check.id
        for check in REGISTRY.values()
        if check.stage == stage and check.id not in UNCOUNTED
    ]


def passed_count(stage: str, items: Sequence[Mapping[str, str]]) -> int:
    """How many checks of the stage passed; the build stops at its first failure."""
    order = stage_order(stage)
    if stage == "build" and items:
        return min(order.index(item["id"]) for item in items)
    failed = {item["id"] for item in items}
    if "bar_unavailable" in failed:
        failed.add("bar_smoke")  # not run is not a pass
    return len([check_id for check_id in order if check_id not in failed])


def problem_row(item: Mapping[str, str]) -> str:
    """One table row: icon, check, what is wrong (with detail) and how to fix it."""
    check = get(item["id"])
    problem = (
        f"Could not run: {trusted(check.problem)}"
        if item["result"] == "not_run" and check.required
        else trusted(check.problem)
    )
    if item.get("detail"):
        problem += f" ({escape_text(item['detail'])})"
    return (
        f"| {icon(item['id'], item['result'])} | {trusted(check.title)} "
        f"| {problem} | {trusted(check.fix)} |"
    )


def render_stage(
    stage: str, items: Sequence[Mapping[str, str]], heading: str
) -> list[str]:
    """A passed stage is one line; a failed one is a table of problems and a pass count."""
    passed = passed_count(stage, items)
    if not items:
        return [f"- ✅ **{STAGE_TITLES[stage]}** — {plural(passed)} passed"]
    lines = ["", f"{heading} {STAGE_TITLES[stage]}", ""]
    lines += ["| | Check | Problem | How to fix |", "|---|---|---|---|"]
    lines += [problem_row(item) for item in items]
    if passed:
        lines.extend(["", f"- ✅ {plural(passed)} passed"])
    return lines


def render_checks(
    findings: Sequence[Mapping[str, str]],
    skipped: Sequence[str] = (),
    heading: str = "###",
    stages: Sequence[str] = STAGES,
    skip_reason: str = "build",
) -> list[str]:
    """
    Render every stage: one line when it passed, a table of problems otherwise.

    Stages that could not run are collapsed into a single skipped line.
    """
    reported: dict[str, list[Mapping[str, str]]] = {}
    for item in findings:
        if item["result"] != "pass":
            reported.setdefault(get(item["id"]).stage, []).append(item)
    lines = [HEADLINES[verdict(findings)], ""]
    for stage in stages:
        if stage not in skipped:
            lines.extend(render_stage(stage, reported.get(stage, []), heading))
    skipped_titles = [STAGE_TITLES[stage] for stage in stages if stage in skipped]
    if skipped_titles:
        reason = SKIP_REASONS[skip_reason]
        lines.append(f"- ➖ **{', '.join(skipped_titles)}** — Skipped: {reason}")
    return lines


GROUP_TITLES = {
    "scripts": "Code",
    "sounds": "Sounds",
    "appmeta": "App metadata",
    "images": "Images",
}


def human_size(size: int) -> str:
    """Format a byte count for people."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def package_summary(inventory: Sequence[Mapping[str, object]]) -> list[str]:
    """
    Summarize the package facts: file counts and sizes per top-level directory.

    The first path part is the application ID; files outside any directory are `Other`.
    """
    groups: dict[str, list[int]] = {}
    for item in inventory:
        parts = str(item["path"]).split("/")
        name = parts[1] if len(parts) > 2 else ""
        groups.setdefault(name, []).append(int(item["size"]))
    total = sum(sum(sizes) for sizes in groups.values())
    lines = [f"- **{len(inventory)} files, {human_size(total)} unpacked**"]
    for name, sizes in sorted(
        groups.items(), key=lambda row: (row[0] != "scripts", -sum(row[1]))
    ):
        title = GROUP_TITLES.get(name, f"`{escape_text(name)}/`" if name else "Other")
        lines.append(f"- {title}: {len(sizes)} files, {human_size(sum(sizes))}")
    return lines


def render_comment(
    findings: Sequence[Mapping[str, str]],
    compare_url: str | None = None,
    inventory: Sequence[Mapping[str, object]] = (),
    skipped: Sequence[str] = (),
    author_attested: bool = False,
    skip_reason: str = "build",
    license_name: str | None = None,
    media: Mapping[str, object] | None = None,
    removal: Mapping[str, object] | None = None,
    bar_media: Mapping[str, object] | None = None,
) -> str:
    """
    Render all checks into one concise PR comment.

    An absent comparison is omitted instead of fabricating a link. A removal PR gets
    the checks and a short note on what the merge does, nothing else.
    """
    if removal is not None:
        lines = [MARKER, "## Checks", "", *render_removal_checks(findings)]
        return "\n".join([*lines, *render_removal(removal)]) + "\n"
    lines = [
        MARKER,
        "## Checks",
        "",
        *render_checks(findings, skipped, skip_reason=skip_reason),
    ]
    notes = ([f"- {REVIEWER_NOTE}"] if author_attested else []) + (
        [f"- License: {license_name}"] if license_name else []
    )
    if notes:
        lines.extend(["", "### For the reviewer", "", *notes])
    lines.extend(render_media(media, bar_media))
    if inventory:
        lines.extend(["", "### Package", "", *package_summary(inventory)])
    if compare_url:
        parsed = urlsplit(compare_url)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "github.com"
            and not parsed.query
            and not parsed.fragment
        ):
            safe_url = "https://github.com" + quote(parsed.path, safe="/%-._~")
            lines.extend(
                ["", "### Changes", "", f"[Compare with previous version]({safe_url})"]
            )
    return "\n".join(lines) + "\n"


def render_removal_checks(findings: Sequence[Mapping[str, str]]) -> list[str]:
    """The verdict and, if there is anything to say, a table of the problems; no pass counts."""
    lines = [HEADLINES[verdict(findings)], ""]
    if findings:
        lines += ["| | Check | Problem | How to fix |", "|---|---|---|---|"]
        lines += [problem_row(item) for item in findings]
    return lines


def render_removal(removal: Mapping[str, object]) -> list[str]:
    """What the PR removes and what happens after the merge, in a few lines."""
    app = f"`{clean_field(removal.get('id'))}`"
    if removal.get("name"):
        app = f"**{clean_field(removal['name'])}** ({app})"
    if not removal.get("published"):
        return [
            "",
            "### Removal",
            "",
            f"This PR deletes the folder of {app}, which is not in the catalog (never "
            + "published or already removed). Merging only cleans the repository.",
        ]
    details = ", ".join(
        part
        for part in (
            f"version {clean_field(removal['version'])}"
            if removal.get("version")
            else "",
            f"by {clean_field(removal['author'])}" if removal.get("author") else "",
        )
        if part
    )
    return [
        "",
        "### Removal",
        "",
        f"This PR removes {app}"
        + (f", {details}" if details else "")
        + " from the catalog.",
        "",
        "After the merge:",
        "- the app disappears from the catalog and from search;",
        "- its versions can no longer be downloaded;",
        "- copies already installed on bars stay as they are.",
        "",
        "Only the app's author or the catalog maintainers can remove an app.",
    ]


def clean_field(value: object, limit: int = 80) -> str:
    """A catalog text made safe for a comment: one line, no markup that renders."""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("`", "'").replace("<", "&lt;").replace("@", "@\u200b")


def find_comment(
    comments: Sequence[Mapping[str, object]], bot_login: str
) -> int | None:
    """
    Locate a previous bot-owned checks comment for updating.

    A marker in another user's comment must never be edited by the workflow.
    """
    for comment in comments:
        user = comment.get("user")
        if (
            isinstance(user, dict)
            and user.get("login") == bot_login
            and isinstance(comment.get("body"), str)
            and MARKER in comment["body"]
            and type(comment.get("id")) is int
        ):
            return comment["id"]
    return None
