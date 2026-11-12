"""Security checks for catalog GitHub Actions workflow definitions."""

import re
from pathlib import Path

import yaml


def test_workflow_permissions_and_action_pins() -> None:
    """
    Enforce minimal workflow defaults and immutable third-party actions.

    Validation jobs may only request repository read access and no secrets.
    """
    directory = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflows = list(directory.glob("*.yml"))
    assert workflows
    for path in workflows:
        text = path.read_text()
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        assert workflow["permissions"] == {}
        assert "pull_request_target" not in text
        assert "CATALOG_CI_TOKEN" not in text or path.name in {
            "publish.yml",
            "review.yml",
        }
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") == "false"
        if path.name == "validate.yml":
            assert "secrets." not in text
            expected = {
                "scope": {"contents": "read", "pull-requests": "read"},
                "manifest": {"contents": "read"},
                "tests": {"contents": "read"},
                "completed": {},
            }
            assert set(workflow["jobs"]) == set(expected)
            for name, job in workflow["jobs"].items():
                assert job.get("permissions", {}) == expected[name]
            scope_steps = workflow["jobs"]["scope"]["steps"]
            token_steps = [
                step for step in scope_steps if "GITHUB_TOKEN" in step.get("env", {})
            ]
            assert len(token_steps) == 1 and token_steps[0]["id"] == "labels"
            selector = next(step for step in scope_steps if step.get("id") == "select")
            assert "GITHUB_TOKEN" not in selector.get("env", {})
        if path.name == "pipeline.yml":
            assert workflow["jobs"]["build"]["permissions"] == {}
            assert "secrets." not in str(workflow["jobs"]["build"])
            assert workflow["jobs"]["report"]["permissions"]["checks"] == "write"
            assert workflow["jobs"]["report"]["permissions"]["pull-requests"] == "write"
        if path.name == "publish.yml":
            jobs = workflow["jobs"]
            assert "secrets." not in str(jobs["discover"])
            assert "secrets." not in str(jobs["report"])
            assert "CATALOG_CI_TOKEN" in str(jobs["publish"])
            assert "needs: discover" in text
            assert "--results checked/results.json" in text


def test_review_is_manual_authorized_and_keeps_secrets_out_of_discovery() -> None:
    """Only people with write access start it; only the last job holds the token."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "review.yml"
    text = path.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    jobs = workflow["jobs"]
    assert jobs["authorize"]["if"] == "github.ref == 'refs/heads/main'"
    assert "collaborators/$ACTOR/permission" in str(jobs["authorize"])
    assert jobs["discover"]["needs"] == "authorize"
    assert jobs["review"]["needs"] == "discover"
    for name in ("authorize", "discover"):
        assert "secrets." not in str(jobs[name]), name
    assert "secrets.CATALOG_CI_TOKEN" in str(jobs["review"])
    assert jobs["review"]["permissions"]["pull-requests"] == "write"
    # The author's code is only read as data: nothing is built or executed here.
    assert "pnpm" not in text and "node " not in text
    assert "pull-requests: write" not in str(jobs["discover"]["permissions"])


def test_publish_can_be_dispatched_for_a_merge_on_main() -> None:
    """Allow republishing a merged PR, only from main and for a named commit."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    text = path.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]["merge_sha"]
    assert dispatch["required"] == "true"
    assert workflow["jobs"]["discover"]["if"] == "github.ref == 'refs/heads/main'"
    assert "inputs.merge_sha || github.sha" in text


def test_validate_runs_trusted_tools_and_tests_only_for_maintenance() -> None:
    """
    Check out the merge commit for scope and manifest, never the author's head.

    The workflow and tools/ then come from one commit; tests run only for
    maintenance pull requests, which are the ones that change the tools.
    """
    path = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "validate.yml"
    )
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    for name in ("scope", "manifest"):
        for step in workflow["jobs"][name]["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert "ref" not in step.get("with", {}), name
    tests = workflow["jobs"]["tests"]
    assert tests["needs"] == "scope"
    assert tests["if"] == "needs.scope.outputs.maintenance == 'true'"
    completed = workflow["jobs"]["completed"]["steps"][0]["run"]
    assert '"$MAINTENANCE" != true && "$TESTS" == skipped' in completed


def test_pipeline_runs_on_any_validate_outcome_and_always_reports() -> None:
    """Scope and manifest errors need a comment, so no job may depend on validate success."""
    directory = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflow = yaml.load(
        (directory / "pipeline.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    assert "conclusion" not in jobs["prepare"]["if"]
    assert "always()" in jobs["report"]["if"]
    assert set(jobs["report"]["needs"]) == {"prepare", "build", "check"}
    for name in ("prepare", "report"):
        checkout = next(
            step
            for step in jobs[name]["steps"]
            if "actions/checkout" in step.get("uses", "")
        )
        assert (
            checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
        )
    assert "early" in jobs["build"]["if"] and "early" in jobs["check"]["if"]


def test_no_bare_exceptions_on_author_data() -> None:
    """Author data errors must be `CheckError`s, which the report turns into a fix."""
    tools = Path(__file__).resolve().parents[1] / "tools"
    # Internal preconditions, not author data: a jump to a local CheckError handler
    # and a checkout path that must not exist.
    allowed = {
        "raise ValueError",
        'raise ValueError("Checkout destination already exists")',
    }
    for name in ("pipeline_prepare", "pipeline_build", "lib_fetch", "lib_build"):
        for line in (tools / f"{name}.py").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("raise ValueError", "raise KeyError")):
                assert name == "lib_fetch" and stripped in allowed, (name, stripped)


def test_every_job_that_runs_the_tools_installs_their_requirements() -> None:
    """
    A tool that imports `lib_http` needs httpx2, which the runner image does not have.

    Only the scope job of validate.yml runs tools that use the standard library alone.
    """
    directory = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for path in sorted(directory.glob("*.yml")):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for name, job in workflow["jobs"].items():
            text = str(job.get("steps", []))
            if "python -m tools." not in text and "from tools." not in text:
                continue
            if (path.name, name) == ("validate.yml", "scope"):
                continue
            assert "tools/requirements.txt" in text, (path.name, name)


def test_publish_removal_job_is_the_only_other_holder_of_the_token() -> None:
    """Removing an app needs the CI token; discovery and the comment must not get it."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert "secrets.CATALOG_CI_TOKEN" in str(jobs["remove"])
    assert jobs["remove"]["permissions"] == {"contents": "read"}
    assert jobs["remove"]["needs"] == "discover"
    assert "kind == 'remove'" in jobs["remove"]["if"]
    assert "kind == 'publish'" in jobs["publish"]["if"]
    for name in ("discover", "report"):
        assert "secrets." not in str(jobs[name]), name
    assert set(jobs["report"]["needs"]) == {"discover", "publish", "remove"}


def test_publish_reports_a_failure_but_not_a_cancellation() -> None:
    """The report runs on success or failure of publish/remove, with the failure file."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    condition = workflow["jobs"]["report"]["if"]
    for result in (
        "needs.publish.result == 'success'",
        "needs.publish.result == 'failure'",
        "needs.remove.result == 'failure'",
    ):
        assert result in condition
    assert "cancelled" not in condition
    name = "failure-${{ github.sha }}-${{ github.run_attempt }}"
    for job in ("publish", "remove"):
        kept = [
            step
            for step in workflow["jobs"][job]["steps"]
            if step.get("with", {}).get("name") == name
        ]
        assert len(kept) == 1 and kept[0]["if"] == "failure()"
    fetched = [
        step
        for step in workflow["jobs"]["report"]["steps"]
        if step.get("with", {}).get("name") == name
    ]
    assert len(fetched) == 1 and fetched[0]["continue-on-error"] == "true"
