"""Observable MVP readiness checks against the configured dev or stage environment."""

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import pytest

from e2e.cases import CASES
from e2e.domain import access_secret_problem, red_rows, report_problems
from e2e.infrastructure import fetch, wait_for


@pytest.mark.task("10.26-25", "10.26-26")
def test_e2e_01_repository_configuration(run):
    """
    Verify author files, branch rules and backend CI settings through GitHub APIs.

    Readiness requires enforcement configuration, not merely local workflow files.
    """
    root = f"repos/{run.repo}"
    for path in [
        "schema/catalog-manifest.schema.json",
        "tools/validate_manifest.py",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/CODEOWNERS",
    ]:
        assert run.author.request("GET", root + "/contents/" + path), f"Missing {path}"
    rules = run.author.request("GET", root + "/rules/branches/main")
    checks = next(
        item["parameters"]["required_status_checks"]
        for item in rules
        if item["type"] == "required_status_checks"
    )
    assert {"validate", "catalog-checks"} <= {item["context"] for item in checks}
    review = next(
        item["parameters"] for item in rules if item["type"] == "pull_request"
    )
    assert (
        review["required_approving_review_count"] >= 1
        and review["dismiss_stale_reviews_on_push"]
    )
    assert review["require_last_push_approval"]
    variables = run.author.collection(root + "/actions/variables", "variables")
    assert any(item["name"] == "CATALOG_API" and item["value"] for item in variables), (
        "Missing repository variable CATALOG_API"
    )
    secrets = run.author.collection(root + "/actions/secrets", "secrets")
    assert any(item["name"] == "CATALOG_CI_TOKEN" for item in secrets), (
        "Missing repository secret CATALOG_CI_TOKEN"
    )
    # The Access secrets are optional until Access is on, but only as a pair.
    problem = access_secret_problem(item["name"] for item in secrets)
    assert problem is None, problem


@pytest.mark.task("10.26-26")
def test_e2e_02_invalid_schema(run):
    """
    Submit an unknown manifest field and require line-specific validation failure.

    The Checks comment explains it (11.03-2) and no application build is started.
    """
    submission = run.submit("valid", "invalid-schema", invalid=True)
    check = run.check(submission, "validate", "failure")
    checks = run.author.collection(
        f"repos/{run.repo}/commits/{submission['head']}/check-runs", "check_runs"
    )
    annotations = []
    for row in checks:
        annotations += run.author.collection(
            f"repos/{run.repo}/check-runs/{row['id']}/annotations"
        )
    assert any(row.get("start_line", 0) > 0 for row in annotations), (
        f"Missing line annotation: {check['html_url']}"
    )
    run.check(submission, "catalog-checks", "failure")
    problems = report_problems(
        run.comment(submission), ("manifest_schema",), "unexpected"
    )
    assert not problems, "; ".join(problems)
    finished = run.author.collection(
        f"repos/{run.repo}/commits/{submission['head']}/check-runs", "check_runs"
    )
    assert not any(row["name"] == "build" for row in finished), (
        "The build ran after an invalid schema"
    )


@pytest.mark.task("10.26-26")
def test_e2e_03_multiple_files(run):
    """
    Require scope rejection and an actionable maintainer message for an extra file.

    This uses a dedicated PR, never the valid fixture submission.
    """
    submission = run.submit("valid", "multiple-files", extra=True)
    run.check(submission, "validate", "failure")
    checks = run.author.collection(
        f"repos/{run.repo}/commits/{submission['head']}/check-runs", "check_runs"
    )
    scope = next(row for row in checks if row["name"] == "scope")
    annotations = run.author.collection(
        f"repos/{run.repo}/check-runs/{scope['id']}/annotations"
    )
    assert any("maintainers" in row.get("message", "") for row in annotations), (
        "Missing actionable scope annotation"
    )
    run.check(submission, "catalog-checks", "failure")
    problems = report_problems(run.comment(submission), ("pr_scope",), "e2e-extra")
    assert not problems, "; ".join(problems)


@pytest.mark.task("11.03-2")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_e2e_error_cases(run, case):
    """
    Every error of an author ends in a red `catalog-checks` and one explaining comment.

    Exactly the expected checks are red, each with a problem and a fix, and the
    comment never shows a traceback.
    """
    submission = run.submit_case(case)
    run.check(submission, "catalog-checks", "failure")
    problems = report_problems(run.comment(submission), case.expect, case.detail)
    assert not problems, f"{case.name}: " + "; ".join(problems)


@pytest.mark.task("11.10-1")
def test_e2e_removal_pr_checks(run):
    """
    A maintainer's PR that deletes a whole application folder is a removal, not a scope error.

    It stays open: the checks are green and the comment says what a merge would do.
    """
    submission = run.submit_removal(run.published_folder(), "removal-checks")
    run.check(submission, "catalog-checks", "success")
    comment = run.comment(submission)
    assert "### Removal" in comment, "Missing the removal section"
    assert red_rows(comment) == [], "A removal by a maintainer must have no red check"
    assert "### Package" not in comment, "A removal builds nothing"


@pytest.mark.task("11.10-1")
def test_e2e_author_removes_own_app(run):
    """
    Publish a fixture app, remove it through a PR, and see the card disappear.

    Needs the moderator and admin tokens, like the other publication scenarios.
    """
    submission = run.remove_published()
    reply = fetch("GET", run.api + "/applications/" + submission["id"], accept={404})
    assert reply.status == 404, "The removed app is still in the catalog"
    comments = run.author.collection(
        f"repos/{run.repo}/issues/{submission['number']}/comments"
    )
    assert any("Removed from the catalog" in row.get("body", "") for row in comments)


@pytest.mark.task("10.26-26")
def test_e2e_04_valid_build_and_report(run):
    """
    Require a green app check, immutable build output and resolved imported descriptions.

    The build output is downloaded as data and never executed by E2E.
    """
    submission = run.submit("valid")
    run.check(submission, "catalog-checks", "success")
    comment = run.comment(submission)
    assert "## Checks" in comment and "Passed" in comment
    assert "### Package" in comment and "unpacked" in comment
    manifest, digest = download_build(run, submission)
    assert (
        not manifest["description"].startswith("@")
        and "E2E valid" in manifest["description"]
    )
    submission["package_hash"] = digest


def download_package(run, version: dict) -> bytes:
    """
    The package bytes of a version: the API answers 302, the storage serves the file.

    The redirect is not followed by the client, so its status is asserted.
    """
    query = urlencode({"api": version["api_version"]})
    redirect = fetch(
        "GET",
        run.api + f"/versions/{version['id']}/build/compatible?{query}",
        timeout=30,
        accept={302},
    )
    assert redirect.status == 302, f"Expected 302 download, got {redirect.status}"
    return fetch(
        "GET", redirect.headers["location"], timeout=60, max_bytes=200 * 1024 * 1024
    ).content


def download_build(run, submission):
    """
    Locate the matching trusted build artifact artifact and return its manifest and package hash.

    Artifact identity is bound to the exact application head SHA.
    """
    import json

    runs = run.author.collection(
        f"repos/{run.repo}/actions/workflows/pipeline.yml/runs?status=success",
        "workflow_runs",
    )
    name = "build-" + submission["head"]
    for workflow in runs:
        artifacts = run.author.collection(
            f"repos/{run.repo}/actions/runs/{workflow['id']}/artifacts", "artifacts"
        )
        if any(item["name"] == name and not item["expired"] for item in artifacts):
            with tempfile.TemporaryDirectory() as temporary:
                subprocess.run(
                    [
                        "gh",
                        "run",
                        "download",
                        str(workflow["id"]),
                        "--repo",
                        run.repo,
                        "--name",
                        name,
                        "--dir",
                        temporary,
                    ],
                    env=run.author.environment,
                    check=True,
                    timeout=120,
                    capture_output=True,
                )
                path = Path(temporary)
                return json.loads(
                    (path / "manifest.resolved.json").read_text()
                ), hashlib.sha256((path / "package.tgz").read_bytes()).hexdigest()
    raise AssertionError(f"Missing immutable artifact {name}")


@pytest.mark.task("10.26-26")
@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("no-api-version", "api_version"),
        ("build-fails", "syntax"),
        ("big-js", "256000"),
    ],
)
def test_e2e_05_rejected_fixtures(run, fixture, reason):
    """
    Require formal or build failures to be red and explained in the PR comment.

    A generic success or absent report cannot satisfy a negative fixture.
    """
    submission = run.submit(fixture)
    run.check(submission, "catalog-checks", "failure")
    assert reason in run.comment(submission).lower(), (
        f"Missing reason {reason} in report"
    )


@pytest.mark.task("10.26-26", "10.26-27")
def test_e2e_06_publish_and_download(run):
    """
    Approve and merge independently, then compare API/download bytes with the CI artifact.

    Follow-up publication tests reuse this app instead of creating unrelated records.
    """
    submission = run.publish()
    application = submission["application"]
    current = application["current_version"]
    _, digest = download_build(run, submission)
    package = download_package(run, current)
    assert hashlib.sha256(package).hexdigest() == digest == current["sha256"]
    comments = run.author.collection(
        f"repos/{run.repo}/issues/{submission['number']}/comments"
    )
    assert any("Published:" in row["body"] for row in comments), (
        "Missing Published confirmation"
    )


@pytest.mark.task("10.26-26", "10.26-27")
def test_e2e_07_publication_idempotency(run):
    """
    Rerun the same publish workflow and require a single unchanged API version.

    Workflow attempt ordering ensures the check observes the repeated execution.
    """
    submission = run.publish()
    path = f"repos/{run.repo}/actions/workflows/publish.yml/runs?head_sha={submission['merge']}"
    previous = run.author.collection(path, "workflow_runs")[0]
    run.author.request("POST", f"repos/{run.repo}/actions/runs/{previous['id']}/rerun")
    updated = wait_for(
        lambda: run.author.request(
            "GET", f"repos/{run.repo}/actions/runs/{previous['id']}"
        ),
        lambda item: (
            item["run_attempt"] > previous["run_attempt"]
            and item["status"] == "completed"
        ),
        "repeated publish completion",
        run.timeout,
    )
    assert updated["conclusion"] == "success"
    versions = run.api_get("/applications/" + submission["id"] + "/versions")
    assert (
        len(versions) == 1
        and versions[0]["id"] == submission["application"]["current_version"]["id"]
    )


@pytest.mark.task("10.26-26", "10.26-27")
def test_e2e_08_version_update(run):
    """
    Reject a same-version update and require a newer source release to publish with a diff.

    All update state remains bound to this run's valid application.
    """
    run.publish()
    old = run.submit("valid", "same-version")
    run.check(old, "catalog-checks", "failure")
    assert "version" in run.comment(old).lower()
    updated = run.publish_update()
    assert updated["current_version"]["version"] == "2.0.0"


@pytest.mark.task("10.28-14", "10.26-27")
def test_e2e_09_categories_and_read_routes(run):
    """
    Verify the public categories and the read routes of the application contract.

    The OpenAPI document is served only at the service root (11.09-5), so the contract
    is checked through the routes it describes. The list must have its shape even when
    the catalog is empty; the card and the versions are read only if there is an app.
    """
    categories = run.api_get("/categories")
    assert [item["id"] for item in categories] == [
        "productivity",
        "clocks",
        "info-feeds",
        "social",
        "dev-monitoring",
        "music",
        "art",
        "games",
        "smart-home",
        "utilities",
    ]
    page = run.api_get("/applications?limit=1")
    assert isinstance(page.get("items"), list) and "total" in page
    if page["items"]:
        card = page["items"][0]
        assert run.api_get(f"/applications/{card['alias']}")["id"] == card["id"]
        assert isinstance(run.api_get(f"/applications/{card['alias']}/versions"), list)


@pytest.mark.task("10.28-11")
def test_e2e_10_attestation(run, tmp_path):
    """
    Verify artifact provenance against GitHub and reject a modified package.

    Null transparency fields are an explicit unfinished-feature failure.
    """
    submission = run.publish()
    version = submission["application"]["current_version"]
    assert version.get("ci_run_url") and version.get("attestation_url"), (
        "Missing CI or attestation URL"
    )
    content = download_package(run, version)
    package = tmp_path / "package.tgz"
    package.write_bytes(content)
    command = ["gh", "attestation", "verify", str(package), "--repo", run.repo]
    subprocess.run(
        command, env=run.author.environment, check=True, capture_output=True, timeout=60
    )
    package.write_bytes(content + b"changed")
    assert (
        subprocess.run(
            command,
            check=False,
            env=run.author.environment,
            capture_output=True,
            timeout=60,
        ).returncode
        != 0
    )


@pytest.mark.task("10.28-11")
def test_e2e_11_fsim_and_facts(run):
    """
    Require network/package facts and fsim screenshots in successful fixture reports.

    Missing report sections expose the unfinished fsim task.
    """
    for fixture, evidence in [
        ("fetch-bypass", "Bar API & network"),
        ("extra-string", "E2E_GENERATED_PACKAGE_ONLY_MARKER"),
    ]:
        submission = run.submit(fixture)
        run.check(submission, "catalog-checks", "success")
        comment = run.comment(submission)
        assert evidence in comment, f"Missing {fixture} report fact: {evidence}"
        assert "Run on fsim" in comment and re.search(r"!\[.*?\]\(", comment), (
            "Missing fsim screenshot evidence"
        )


@pytest.mark.task("10.28-10")
def test_e2e_12_environment_isolation(run):
    """
    Verify that this run's published dev app cannot be found in stage.

    An absent stage endpoint remains a failure until stage has been configured.
    """
    stage = os.getenv("E2E_STAGE_API")
    assert stage, "Not configured: E2E_STAGE_API"
    submission = run.publish()
    reply = fetch(
        "GET",
        stage.rstrip("/") + "/applications/" + submission["id"],
        timeout=20,
        accept={404},
    )
    assert reply.status == 404, "Dev application leaked into stage"


@pytest.mark.task("10.28-12")
def test_e2e_13_real_bar_report(run):
    """
    Require real-device firmware, installation and both-display screenshot evidence.

    Use the longer device-test deadline without changing other CI waits.
    """
    submission = run.submit("valid")
    comment = wait_for(
        lambda: run.comment(submission),
        lambda value: "Run on real bar" in value,
        "Run on real bar report",
        float(os.getenv("E2E_REAL_TIMEOUT_S", "1800")),
    )
    for evidence in ["firmware", "install", "front", "back"]:
        assert evidence in comment.lower(), f"Missing real bar evidence: {evidence}"
