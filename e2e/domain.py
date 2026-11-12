"""Deterministic naming and fixture rewrite rules for isolated E2E runs."""

import hashlib
import json
import re


def app_id(fixture: str, run: str, variant: str = "") -> str:
    """
    Produce a bounded application ID while retaining a collision-resistant run suffix.

    A variant gives the same fixture a second application of its own (`remove`).
    The `valid` fixture and its variants stay within the 32 characters busy-cli accepts.
    """
    # busy-cli refuses an id longer than 32 characters: a variant is shortened to fit.
    name = f"{fixture[:5]}{variant[:3]}" if variant else fixture
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)[:17]
    suffix = hashlib.sha256(run.encode()).hexdigest()[:10]
    return f"e2e.catalog.{safe}_{suffix}"


def fixture_readme(fixture: str) -> str:
    """
    The description a fixture's manifest imports (`@README.md`), long enough for the 50-character limit.

    The run rewrites it in its own commit, so the fixtures repository need not carry it.
    """
    return (
        f"# E2E {fixture}\n\n"
        f"Catalog verification fixture {fixture}: a description of the required length.\n"
    )


def rewrite_manifest(source: bytes, identifier: str, version: str) -> bytes:
    """
    Replace identity and release version without touching fixture-specific fields.

    Missing api_version remains missing so negative fixtures retain their purpose.
    """
    data = json.loads(source)
    data.update(id=identifier, version=version)
    result = json.dumps(data, separators=(",", ":")).encode()
    if len(result) > 512:
        raise ValueError("Rewritten fixture manifest exceeds 512 bytes")
    return result


def ref_gone(error_text: str) -> bool:
    """
    Whether a failed branch DELETE means the branch is already absent.

    GitHub itself deletes the branch of a merged PR and answers 422 "Reference does not
    exist" (404 for some repositories); the goal of the cleanup is reached then.
    """
    status = re.search(r"HTTP (\d{3})", error_text)
    if not status:
        return False
    code = int(status.group(1))
    return (
        code == 404 or code == 422 and "reference does not exist" in error_text.lower()
    )


def owned_ref(ref: str, run: str) -> bool:
    """
    Restrict cleanup to branches in this exact isolated run namespace.

    A prefix boundary prevents a run from deleting a similarly named neighbour.
    """
    return ref == f"e2e/{run}" or ref.startswith(f"e2e/{run}/")


ROW = re.compile(r"^\| (✅|❌|⚠️) \| (.+?) \| (.+?) \| (.+?) \|$", re.MULTILINE)


def red_rows(comment: str) -> list[dict[str, str]]:
    """
    The ❌ rows of the Checks comment: registry id, problem text and fix text.

    The comment shows titles, so each title is mapped back to its registry id.
    """
    from tools.check_registry import CHECKS
    from tools.lib_comment import trusted

    by_title: dict[str, list] = {}
    for check in CHECKS:
        by_title.setdefault(trusted(check.title), []).append(check)

    def registry_id(title: str, problem: str) -> str:
        """Checks may share a title (`Application ID`): the problem text tells them apart."""
        candidates = by_title.get(title, [])
        for check in candidates:
            if problem.startswith(trusted(check.problem)):
                return check.id
        return candidates[0].id if len(candidates) == 1 else title

    return [
        {"id": registry_id(title, problem), "problem": problem, "fix": fix}
        for icon, title, problem, fix in ROW.findall(comment)
        if icon == "❌"
    ]


def report_problems(
    comment: str, expect: tuple[str, ...], detail: str = ""
) -> list[str]:
    """
    Everything wrong with the comment of an error case, or an empty list.

    Exactly the expected checks are red, each with a problem and a fix, there is no
    traceback, and `detail` (if given) appears in the text.
    """
    problems = []
    rows = red_rows(comment)
    found = {row["id"] for row in rows}
    if found != set(expect):
        problems.append(f"red checks {sorted(found)}, expected {sorted(expect)}")
    for row in rows:
        if not row["problem"].strip() or not row["fix"].strip():
            problems.append(f"{row['id']}: empty problem or fix")
    if "Traceback" in comment or 'File "' in comment:
        problems.append("the comment contains a traceback")
    if detail and detail.lower() not in comment.lower():
        problems.append(f"the comment does not mention {detail!r}")
    return problems
