"""The error-case table itself: unique names, valid plans and the offline expectations."""

import json
from pathlib import Path

import pytest

from e2e.cases import CASES, base_manifest, plan_files
from e2e.domain import app_id, red_rows, report_problems
from tools.check_manifest import validate
from tools.check_registry import CheckError, blocks, get
from tools.lib_comment import render_checks
from tools.validate_scope import select_from_changes

RUN = "20260101000000000000"
ORIGIN = "https://github.com/busy-app/busy-apps-catalog-fixtures"
SHA = "a" * 40
NEEDS_NETWORK = {"category_public"}


def plan(case):
    """The case's PR entries for a made-up run."""
    identifier = app_id("valid", RUN)
    manifest = base_manifest(ORIGIN, SHA, "valid", case.name)
    return plan_files(case, identifier, manifest, existing="app.busy.existing")


def test_case_names_are_unique_and_branch_safe() -> None:
    """Names become branch names; they must not collide or hold odd characters."""
    names = [case.name for case in CASES]
    assert len(names) == len(set(names))
    assert all(name.replace("-", "").isalnum() for name in names)


def test_every_expected_id_is_registered() -> None:
    """A typo in an expectation would make a case unpassable."""
    for case in CASES:
        for check_id in case.expect:
            assert get(check_id).required, case.name


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_expectations_hold_offline_for_scope_and_manifest_cases(case, tmp_path) -> None:
    """
    Scope and manifest checks need no network: run them on the planned PR files.

    Cases that expect build-stage checks are only verified live.
    """
    entries = plan(case)
    changes = [("D" if e["content"] is None else "A", e["path"]) for e in entries]
    try:
        path = select_from_changes(changes)
    except CheckError as error:
        assert (error.id,) == case.expect, case.name
        return
    stages = {get(check_id).stage for check_id in case.expect}
    if stages != {"manifest"}:
        pytest.skip("needs the build or the public category list")
    folder = path.split("/")[1]
    manifest = tmp_path / folder / "manifest.yml"
    manifest.parent.mkdir()
    manifest.write_text(entries[0]["content"], encoding="utf-8")
    found = {
        item["id"]
        for item in validate(manifest, folder=folder)
        if blocks(item["id"], item["result"])
    }
    assert found == set(case.expect) - NEEDS_NETWORK, case.name


def test_the_valid_manifest_has_no_manifest_findings(tmp_path: Path) -> None:
    """The starting point of every case is itself clean, so a red check is the case's doing."""
    identifier = app_id("valid", RUN)
    manifest = tmp_path / identifier / "manifest.yml"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(base_manifest(ORIGIN, SHA, "valid", "x")))
    assert not [
        i for i in validate(manifest, folder=identifier) if blocks(i["id"], i["result"])
    ]


def comment_for(*ids: str) -> str:
    """A rendered Checks comment with the given checks red."""
    return "\n".join(
        render_checks([{"id": i, "result": "fail", "detail": "d"} for i in ids])
    )


def test_report_problems_accepts_exactly_the_expected_red_checks() -> None:
    """The comment parser maps rows back to ids and compares them as a set."""
    comment = comment_for("pr_scope")
    assert [row["id"] for row in red_rows(comment)] == ["pr_scope"]
    assert report_problems(comment, ("pr_scope",), "d") == []


def test_report_problems_names_every_deviation() -> None:
    """An extra red check, a missing detail and a traceback are all reported."""
    comment = (
        comment_for("pr_scope", "manifest_schema")
        + "\nTraceback (most recent call last)"
    )
    problems = report_problems(comment, ("pr_scope",), "no-such-text")
    assert any("expected" in text for text in problems)
    assert any("traceback" in text for text in problems)
    assert any("no-such-text" in text for text in problems)


def test_a_removal_with_another_change_is_a_scope_error_not_a_removal() -> None:
    """The deletion case now carries an extra file: only a whole-folder deletion is a removal."""
    from e2e.cases import CASES

    case = next(item for item in CASES if item.name == "scope-delete-and-add")
    entries = plan_files(case, "x.y.z", {}, existing="app.busy.existing")
    assert [e["content"] is None for e in entries] == [True, False]


def test_every_generated_fixture_text_meets_the_manifest_limits() -> None:
    """The valid manifest and each fixture README pass the real text checks of `check_manifest`."""
    from e2e.application import FIXTURES
    from e2e.domain import fixture_readme
    from tools.check_manifest import text_findings

    manifest = base_manifest(ORIGIN, SHA, "valid", "valid")
    assert text_findings(manifest) == []
    for fixture in FIXTURES:
        resolved = {**manifest, "description": fixture_readme(fixture)}
        assert text_findings(resolved) == [], fixture


def test_red_rows_tell_apart_checks_that_share_a_title() -> None:
    """`app_id_match` and `invalid_id` are both titled "Application ID"; the real PR #138 row is the first."""
    comment = (
        "| | Check | Problem | How to fix |\n|---|---|---|---|\n"
        "| ❌ | Application ID | Application ID doesn't match. (Folder &#96;applications/"
        "e2e.catalog.valid_c5496ad69d_x/&#96;, but &#96;src/appmeta/manifest.json&#96; has "
        "&#96;e2e.catalog.valid_c5496ad69d&#96;) | Rename the folder to match the `id`. |\n"
        "| ❌ | Application ID | The application ID is not valid. | Use a valid id. |\n"
    )
    assert [row["id"] for row in red_rows(comment)] == ["app_id_match", "invalid_id"]
