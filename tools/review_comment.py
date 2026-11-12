"""The AI review comment: render the backend answer and keep one comment per PR."""

import re
from collections.abc import Mapping, Sequence
from urllib.parse import quote

from tools.lib_github import GitHubGateway

MARKER = "<!-- catalog-ai-review -->"
MAX_TEXT = 4000
MAX_FINDINGS = 30
ZERO_WIDTH = chr(0x200B)
STEP_TITLES = (
    ("content", "Content"),
    ("analyst", "Dossier"),
    ("reviewer", "Findings"),
)


def clean(value: object, limit: int = MAX_TEXT) -> str:
    """
    Make model output safe to post: no HTML, no mentions, no auto-links of issues.

    The text derives from the author's code and manifest, so it is never trusted.
    """
    text = str(value).replace("\r", "")[:limit]
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    return re.sub(r"@(?=\w)", "@" + ZERO_WIDTH, text)


def file_link(origin: str, sha: str, subdir: str, path: str, line: object) -> str:
    """Markdown link to a file of the author's repository at the reviewed commit."""
    base = origin.rstrip("/").removesuffix(".git")
    parts = [part for part in (subdir, path) if part and part != "."]
    url = f"{base}/blob/{sha}/" + quote("/".join(parts).strip("/"), safe="/")
    label = path + (f":{line}" if isinstance(line, int) else "")
    anchor = f"#L{line}" if isinstance(line, int) else ""
    return f"[{code_span(label)}]({url}{anchor})"


def render_step(step: object, title: str, source: Mapping[str, object]) -> list[str]:
    """One expert section: its failure, its text and its findings."""
    lines = [f"### {title}", ""]
    if not isinstance(step, Mapping) or step.get("status") == "failed":
        reason = step.get("error") if isinstance(step, Mapping) else "no answer"
        return [*lines, f"_step failed: {clean(reason, 300)}_", ""]
    if step.get("text"):
        lines += [clean(step["text"]), ""]
    findings = step.get("findings")
    for item in (findings if isinstance(findings, list) else [])[:MAX_FINDINGS]:
        if not isinstance(item, Mapping):
            continue
        where = ""
        if isinstance(item.get("file"), str):
            where = " — " + file_link(
                str(source.get("origin", "")),
                str(source.get("commit_sha", "")),
                str(source.get("subdir", ".")),
                item["file"],
                item.get("line"),
            )
        lines.append(
            f"- **{clean(item.get('title', 'Finding'), 200)}**{where}: "
            f"{clean(item.get('detail', ''), 800)}"
        )
    return [*lines, ""]


# Every issue is listed; this only keeps a runaway answer within GitHub's comment size.
MAX_SHOWN = 30
CONTENT_LINE = {
    "clean": "clean.",
    "review": "a person should look.",
    "block": "against the terms.",
}
STEP_LABELS = {"reviewer": "code review", "content": "content check"}


def code_span(value: object, limit: int = 200) -> str:
    """A path or name in backticks: one line, no backticks (GitHub renders nothing inside)."""
    text = " ".join(str(value).split()).replace("`", "'")[:limit]
    return f"`{text}`"


def one_line(value: object, limit: int = 400) -> str:
    """
    Model text for one line of the public comment: nothing in it renders as markup.

    On top of `clean` (no HTML, no mentions): newlines collapse; backslashes, `[` `]`
    and the emphasis marks `*` `_` `~` are escaped, so no link, image, bold or struck
    text forms; `#` before a digit and `://` get a zero-width space so no issue
    reference or URL is linked; a leading `#` or `>` cannot start a block.
    """
    text = clean(" ".join(str(value).split()), limit)
    for mark in ("\\", "[", "]", "*", "_", "~"):
        text = text.replace(mark, "\\" + mark)
    text = re.sub(r"#(?=\d)", "#" + ZERO_WIDTH, text).replace(
        "://", ":" + ZERO_WIDTH + "//"
    )
    return "\\" + text if text[:1] in {"#", ">"} else text


def strings(value: object) -> list[str]:
    """A list of non-empty strings from the answer; anything else is dropped."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def place_link(source: Mapping[str, object], item: Mapping[str, object]) -> str:
    """The link to the item's file and line, or its `where` (an image name), or nothing."""
    if isinstance(item.get("file"), str) and type(item.get("line")) is int:
        return " " + file_link(
            str(source.get("origin", "")),
            str(source.get("commit_sha", "")),
            str(source.get("subdir", ".")),
            item["file"],
            item["line"],
        )
    if isinstance(item.get("where"), str) and item["where"]:
        return " " + code_span(item["where"])
    return ""


def plural(count: int, word: str) -> str:
    """`1 issue`, `2 issues`."""
    return f"{count} {word}{'s' if count != 1 else ''}"


def first_line(count: int, partial_read: bool, content_unchecked: bool) -> str:
    """How many issues with the terms were found, and where that claim stops."""
    found = (
        "No issues with the terms found"
        if count == 0
        else f"{plural(count, 'issue')} with the terms found"
    )
    if partial_read:
        found += " in the files read"
    if content_unchecked:
        found += "; the content was not checked"
    return f"**{found}.**"


def issue_line(number: int, item: object, source: Mapping[str, object]) -> str:
    """`1. What the app does. Term: X.` and the place, or a note that it is not confirmed."""
    if not isinstance(item, Mapping):
        item = {"text": item}
    text = one_line(item.get("text", ""), 400).rstrip(". ")
    term = one_line(item.get("term", ""), 60)
    if not term:
        # Not a claim about the code (the content flag with no finding): no place to confirm.
        return f"{number}. {text}." + place_link(source, item)
    place = place_link(source, item) or " (place not confirmed)"
    return f"{number}. {text}. Term: {term}.{place}"


def checked_line(
    label: str, item: object, source: Mapping[str, object], missing: str
) -> str:
    """`- Network: …` with up to two links, or why it was not checked."""
    if not isinstance(item, Mapping) or not str(item.get("text") or "").strip():
        return f"- {label}: {missing}"
    citations = item.get("citations")
    links = [
        link
        for link in (
            place_link(source, citation).strip()
            for citation in (citations if isinstance(citations, list) else [])[:2]
            if isinstance(citation, Mapping)
        )
        if link
    ]
    text = one_line(item["text"], 300).rstrip(". ")
    return f"- {label}: {text}." + (" " + ", ".join(links) if links else "")


def read_line(
    answer: Mapping[str, object], source: Mapping[str, object], reviewed: bool
) -> str:
    """Which files were read and which were not (once the code review ran), commit, time."""
    parts = []
    if reviewed:
        read = strings(answer.get("read"))
        not_read = strings(answer.get("not_read"))
        shown = ", ".join(code_span(path) for path in read[:3])
        if len(read) > 3:
            shown += f" and {len(read) - 3} more"
        parts.append(f"Read: {shown or 'nothing'}")
        if not_read:
            hidden = ", ".join(code_span(path) for path in not_read[:5])
            if len(not_read) > 5:
                hidden += f" and {len(not_read) - 5} more"
            parts.append(f"not read: {hidden}")
    parts.append(code_span(str(source.get("commit_sha", ""))[:7]))
    if type(answer.get("duration_ms")) is int:
        parts.append(f"{answer['duration_ms'] / 1000:.0f} s")
    return "<sub>" + " · ".join(parts) + "</sub>"


def render_terms(answer: Mapping[str, object], source: Mapping[str, object]) -> str:
    """
    The comment for an answer in the terms shape: a count, the issues, what was checked.

    The count is claimed only when the code review finished; a failed or missing step is
    said plainly, so an unfinished review never reads as "no issues".
    """
    issues = [
        item for item in answer.get("issues") or [] if isinstance(item, Mapping | str)
    ]
    raw_failed = answer.get("failed")
    failed = raw_failed if isinstance(raw_failed, Mapping) else {}
    reviewed = (
        isinstance(raw_failed, Mapping)
        and "reviewer" not in failed
        and isinstance(answer.get("network"), Mapping)
    )
    content = answer.get("content")
    content_checked = content in CONTENT_LINE and "content" not in failed
    lines = [MARKER, "### AI review", ""]
    if reviewed:
        lines.append(
            first_line(
                len(issues), bool(strings(answer.get("not_read"))), not content_checked
            )
        )
    else:
        reason = failed.get("reviewer") or "the code review gave no answer"
        lines.append(f"**The review did not finish:** {one_line(reason, 200)}.")
        if issues:
            lines.append(f"{plural(len(issues), 'issue')} found by the content check.")
    for step, reason in failed.items():
        if step != "reviewer":
            label = STEP_LABELS.get(str(step), one_line(step, 20))
            lines.append(f"_The {label} did not finish: {one_line(reason, 200)}._")
    lines.append("")
    for number, item in enumerate(issues[:MAX_SHOWN], start=1):
        lines.append(issue_line(number, item, source))
    if len(issues) > MAX_SHOWN:
        hidden = plural(len(issues) - MAX_SHOWN, "more issue")
        lines += ["", f"And {hidden}, not shown."]
    if issues:
        lines.append("")
    lines += [
        "**Checked**",
        checked_line("Network", answer.get("network"), source, "not checked."),
        checked_line("Bar API", answer.get("bar_api"), source, "not checked."),
        f"- Content: {CONTENT_LINE[content] if content_checked else 'not checked.'}",
        "",
    ]
    about = answer.get("about")
    if isinstance(about, str) and about.strip():
        lines += [f"**About the app:** {one_line(about, 400)}", ""]
    lines.append(read_line(answer, source, reviewed))
    return "\n".join(lines).rstrip() + "\n"


def render_review(
    answer: Mapping[str, object],
    source: Mapping[str, object],
    requested_by: str,
) -> str:
    """
    The full comment for one backend answer; never raises on a malformed one.

    An answer with `issues` is the terms shape; anything else is the earlier report,
    kept until the backend that answers in the terms shape is deployed everywhere.
    """
    if isinstance(answer.get("issues"), list):
        return render_terms(answer, source)
    return render_legacy(answer, source, requested_by)


def render_legacy(
    answer: Mapping[str, object],
    source: Mapping[str, object],
    requested_by: str,
) -> str:
    """The earlier report (triage flags and three expert sections)."""
    lines = [
        MARKER,
        "## AI review — a hint for moderators, not a decision",
        "",
        "Commit `{}` · run by `{}`".format(
            clean(source.get("commit_sha", ""), 40), clean(requested_by, 40)
        ),
        "",
    ]
    triage = answer.get("triage")
    flags = triage.get("flags") if isinstance(triage, Mapping) else None
    if isinstance(triage, Mapping) and triage.get("status") == "failed":
        lines += ["### Triage flags", "", "_step failed; default context was used_", ""]
    elif isinstance(flags, list) and flags:
        lines += [
            "### Triage flags",
            "",
            *[f"- {clean(flag, 300)}" for flag in flags],
            "",
        ]
    steps = answer.get("steps")
    steps = steps if isinstance(steps, Mapping) else {}
    for key, title in STEP_TITLES:
        lines += render_step(steps.get(key), title, source)
    models, usage = answer.get("models"), answer.get("usage")
    footer = []
    if isinstance(models, Mapping):
        footer.append(
            "models: "
            + ", ".join(f"{clean(k, 20)}={clean(v, 40)}" for k, v in models.items())
        )
    if isinstance(usage, Mapping):
        footer.append("tokens (in/out): " + usage_line(usage))
    if isinstance(answer.get("duration_ms"), int):
        footer.append(f"{answer['duration_ms'] / 1000:.1f} s")
    if footer:
        lines.append("<sub>" + " · ".join(footer) + "</sub>")
    return "\n".join(lines).rstrip() + "\n"


def usage_line(usage: Mapping[str, object]) -> str:
    """Per step `in/out`, where `in` adds cache reads and writes; a step with odd data is skipped."""
    parts = []
    for step, counts in usage.items():
        if not isinstance(counts, Mapping):
            continue
        spent_in = sum(
            counts.get(key, 0)
            for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
            if type(counts.get(key, 0)) is int
        )
        spent_out = counts.get("output_tokens", 0)
        if type(spent_out) is int:
            parts.append(f"{clean(step, 20)} {spent_in}/{spent_out}")
    return ", ".join(parts) or "n/a"


def find_review_comment(
    comments: Sequence[Mapping[str, object]], bot_login: str
) -> int | None:
    """The bot's own AI review comment; a marker in another user's comment is ignored."""
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


def post_review(
    gateway: GitHubGateway,
    number: int,
    body: str,
    bot_login: str = "github-actions[bot]",
) -> None:
    """Create the comment, or update the bot's previous one so there is never a second."""
    comment_id = find_review_comment(gateway.comments(number), bot_login)
    if comment_id is None:
        gateway.create_comment(number, body)
    else:
        gateway.update_comment(comment_id, body)
