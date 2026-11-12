"""Orchestrate isolated submissions, evidence checks and cleanup."""

import base64
import json
import os
from datetime import UTC, datetime
from urllib.parse import quote

import httpx2

from e2e.cases import Case, base_manifest, plan_files
from e2e.domain import app_id, fixture_readme, owned_ref, rewrite_manifest
from e2e.infrastructure import GitHub, HarnessNetworkError, fetch, wait_for
from tools import lib_http

VARIANTS = ("remove",)
FIXTURES = (
    "valid",
    "no-api-version",
    "build-fails",
    "big-js",
    "fetch-bypass",
    "extra-string",
    "screenshot-wrong-size",
    "screenshot-not-png",
    "no-lockfile",
)
HARNESS_ERRORS = (
    RuntimeError,
    lib_http.HttpError,
    httpx2.HTTPError,
    OSError,
    KeyError,
    TypeError,
)
ATTESTATION = "- [x] I'm the author of the app or have write access to its repository"


class Run:
    """Keep all remote resource identities scoped to one execution."""

    def __init__(self, environment: str, keep: bool = False) -> None:
        """
        Read configuration without requiring deployment for independent CI scenarios.

        Each role uses its own credential; absent moderator/admin blocks publication.
        """
        if environment == "stage" and not all(
            os.getenv(key) for key in ("E2E_CATALOG_REPO", "E2E_API")
        ):
            raise ValueError(
                "Configure E2E_CATALOG_REPO and E2E_API explicitly for stage"
            )
        self.environment = environment
        self.keep = keep
        self.id = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        self.repo = os.getenv("E2E_CATALOG_REPO", "busy-app/busy-apps-catalog-dev")
        self.fixtures_repo = os.getenv(
            "E2E_FIXTURES_REPO", "busy-app/busy-apps-catalog-fixtures"
        )
        self.api = os.getenv(
            "E2E_API", "https://cloud.dev.busy.app/api/catalog/v1"
        ).rstrip("/")
        self.timeout = float(os.getenv("E2E_TIMEOUT_S", "900"))
        self.author = GitHub(os.getenv("E2E_AUTHOR_TOKEN", ""))
        self.source_sha = None
        self.submissions = {}
        self.refs = []
        self.published = []
        self.published_ids = []
        self.merged_ids = set()
        self.removed_ids = set()
        self.publications = {}
        self.evidence = []
        self.cleanup_network = False

    def source(self) -> str:
        """
        Create a single immutable fixture commit with rewritten app identities.

        The source repository is never executed by this orchestration process.
        """
        if self.source_sha:
            return self.source_sha
        root = f"repos/{self.fixtures_repo}"
        base = self.author.request("GET", root + "/git/ref/heads/main")["object"]["sha"]
        commit = self.author.request("GET", root + "/git/commits/" + base)
        entries = []
        for fixture in FIXTURES:
            path = f"{fixture}/src/appmeta/manifest.json"
            content = self.author.request("GET", root + f"/contents/{path}?ref={base}")
            payload = rewrite_manifest(
                base64.b64decode(content["content"]), app_id(fixture, self.id), "1.0.0"
            )
            entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": payload.decode(),
                }
            )
            entries.append(
                {
                    "path": f"{fixture}/README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": fixture_readme(fixture),
                }
            )
        entries += self.variant_entries(root, commit["tree"]["sha"], base)
        tree = self.author.request(
            "POST",
            root + "/git/trees",
            {"base_tree": commit["tree"]["sha"], "tree": entries},
        )
        result = self.author.request(
            "POST",
            root + "/git/commits",
            {
                "message": f"E2E fixture identities {self.id}",
                "tree": tree["sha"],
                "parents": [base],
            },
        )
        # Not `e2e/<id>`: git cannot hold that branch next to `e2e/<id>/version2`.
        ref = f"e2e/{self.id}/source"
        self.author.request(
            "POST",
            root + "/git/refs",
            {"ref": "refs/heads/" + ref, "sha": result["sha"]},
        )
        self.refs.append((self.fixtures_repo, ref))
        self.source_sha = result["sha"]
        return self.source_sha

    def variant_entries(self, root: str, tree_sha: str, base: str) -> list[dict]:
        """
        A copy of the `valid` fixture for each variant, with an identity of its own.

        Files are shared by blob; only the app manifest and the README are written anew.
        """
        listing = self.author.request(
            "GET", root + f"/git/trees/{tree_sha}?recursive=1"
        )
        assert not listing.get("truncated"), "The fixtures tree is too large to copy"
        entries = []
        for variant in VARIANTS:
            folder = f"valid-{variant}"
            for item in listing["tree"]:
                path = item["path"]
                if item["type"] != "blob" or not path.startswith("valid/"):
                    continue
                if path in ("valid/src/appmeta/manifest.json", "valid/README.md"):
                    continue
                entries.append(
                    {
                        "path": folder + path.removeprefix("valid"),
                        "mode": item["mode"],
                        "type": "blob",
                        "sha": item["sha"],
                    }
                )
            manifest = self.author.request(
                "GET", root + f"/contents/valid/src/appmeta/manifest.json?ref={base}"
            )
            payload = rewrite_manifest(
                base64.b64decode(manifest["content"]),
                app_id("valid", self.id, variant),
                "1.0.0",
            )
            entries.append(
                {
                    "path": f"{folder}/src/appmeta/manifest.json",
                    "mode": "100644",
                    "type": "blob",
                    "content": payload.decode(),
                }
            )
            entries.append(
                {
                    "path": f"{folder}/README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": fixture_readme(folder),
                }
            )
        return entries

    def submit(
        self,
        fixture: str,
        case: str = "",
        invalid: bool = False,
        extra: bool = False,
        variant: str = "",
    ) -> dict:
        """
        Create a catalog branch and PR that changes only this case's manifest.

        Deliberate schema/scope violations remain isolated from other submissions.
        """
        case = case or (f"{fixture}-{variant}" if variant else fixture)
        if case in self.submissions:
            return self.submissions[case]
        identifier = app_id(fixture, self.id, variant)
        subdir = f"{fixture}-{variant}" if variant else fixture
        manifest = base_manifest(
            f"https://github.com/{self.fixtures_repo}", self.source(), subdir, case
        )
        if case == fixture or variant:
            manifest["description"] = "@README.md"
        if invalid:
            manifest["unexpected"] = True
        entries = [
            {
                "path": f"applications/{identifier}/manifest.yml",
                "content": json.dumps(manifest),
            }
        ]
        if extra:
            entries.append(
                {
                    "path": f"e2e-extra-{self.id}.txt",
                    "content": "scope rejection fixture",
                }
            )
        return self.open_pull(entries, case, identifier)

    def submit_case(self, case: Case) -> dict:
        """Open the PR of one error case, built from the valid fixture's source."""
        if case.name in self.submissions:
            return self.submissions[case.name]
        identifier = app_id(case.fixture, self.id)
        manifest = base_manifest(
            f"https://github.com/{self.fixtures_repo}",
            self.source(),
            case.fixture,
            case.name,
        )
        existing = self.published_folder() if case.delete_existing else None
        entries = plan_files(case, identifier, manifest, existing)
        return self.open_pull(entries, case.name, identifier)

    def submit_removal(self, folder: str, case: str) -> dict:
        """Open a PR that deletes the whole folder `applications/<folder>/`."""
        entries = [{"path": f"applications/{folder}/manifest.yml", "content": None}]
        return self.open_pull(entries, case, folder)

    def remove_published(self) -> dict:
        """
        The author removes a fixture app of their own (not the shared one): PR, approval, merge, 404.

        Needs the same moderator and admin credentials as a publication.
        """
        published = self.publish("remove")
        moderator = GitHub(os.environ["E2E_MODERATOR_TOKEN"])
        submission = self.submit_removal(published["id"], "remove-own")
        self.check(submission, "catalog-checks", "success")
        root = f"repos/{self.repo}/pulls/{submission['number']}"
        moderator.request(
            "POST",
            root + "/reviews",
            {
                "event": "APPROVE",
                "body": "E2E removal reviewed",
                "commit_id": submission["head"],
            },
        )
        merged = moderator.request(
            "PUT",
            root + "/merge",
            {"merge_method": "squash", "sha": submission["head"]},
        )
        assert merged["merged"], "Approved removal was not merged"
        self.removed_ids.add(published["id"])
        wait_for(
            lambda: self.author.collection(
                f"repos/{self.repo}/actions/workflows/publish.yml/runs?head_sha={merged['sha']}",
                "workflow_runs",
            ),
            lambda rows: any(row["status"] == "completed" for row in rows),
            "removal completion",
            self.timeout,
        )
        submission["merge"] = merged["sha"]
        return submission

    def published_folder(self) -> str:
        """The folder of any manifest already on main, for the deletion case."""
        rows = self.author.request("GET", f"repos/{self.repo}/contents/applications")
        folders = sorted(row["name"] for row in rows if row["type"] == "dir")
        assert folders, "No published manifest to delete"
        return folders[0]

    def open_pull(self, entries: list[dict], case: str, identifier: str) -> dict:
        """
        Commit `entries` (`content` None deletes) on a new branch and open the PR.

        The branch and the PR are recorded for cleanup under this run's namespace.
        """
        root = f"repos/{self.repo}"
        base = self.author.request("GET", root + "/git/ref/heads/main")["object"]["sha"]
        commit = self.author.request("GET", root + "/git/commits/" + base)
        tree = self.author.request(
            "POST",
            root + "/git/trees",
            {
                "base_tree": commit["tree"]["sha"],
                "tree": [
                    {
                        "path": entry["path"],
                        "mode": "100644",
                        "type": "blob",
                        **(
                            {"content": entry["content"]}
                            if entry["content"] is not None
                            else {"sha": None}
                        ),
                    }
                    for entry in entries
                ],
            },
        )
        head = self.author.request(
            "POST",
            root + "/git/commits",
            {
                "message": f"test(e2e): {case} {self.id}",
                "tree": tree["sha"],
                "parents": [base],
            },
        )["sha"]
        ref = f"e2e/{self.id}/{case}"
        self.author.request(
            "POST", root + "/git/refs", {"ref": "refs/heads/" + ref, "sha": head}
        )
        self.refs.append((self.repo, ref))
        pull = self.author.request(
            "POST",
            root + "/pulls",
            {
                "title": f"E2E {case} {self.id}",
                "head": ref,
                "base": "main",
                "body": ATTESTATION + "\n\nIsolated catalog E2E fixture.",
            },
        )
        result = {"number": pull["number"], "head": head, "id": identifier, "ref": ref}
        self.submissions[case] = result
        self.evidence.append(pull["html_url"])
        return result

    def check(self, submission: dict, name: str, expected: str) -> dict:
        """
        Require a completed check with the expected outcome on the exact submitted SHA.

        Missing infrastructure is a descriptive failure rather than a skip.
        """
        path = f"repos/{self.repo}/commits/{submission['head']}/check-runs"
        checks = wait_for(
            lambda: self.author.collection(path, "check_runs"),
            lambda rows: any(
                row["name"] == name and row["status"] == "completed" for row in rows
            ),
            f"{name} on {submission['head']}",
            self.timeout,
        )
        check = next(
            row
            for row in checks
            if row["name"] == name and row["status"] == "completed"
        )
        assert check["conclusion"] == expected, (
            f"{name}: expected {expected}, got {check['conclusion']}: {check.get('html_url')}"
        )
        return check

    def comment(self, submission: dict) -> str:
        """
        Read the single workflow-owned catalog report for the PR.

        Duplicate marked comments are a failure of report idempotency.
        """
        comments = self.author.collection(
            f"repos/{self.repo}/issues/{submission['number']}/comments"
        )
        reports = [
            row["body"]
            for row in comments
            if row.get("user", {}).get("type") == "Bot"
            and "<!-- catalog-checks -->" in row["body"]
        ]
        assert len(reports) == 1, f"Expected one catalog report, found {len(reports)}"
        return reports[0]

    def cleanup_error(self, error: Exception) -> str:
        """The error text; a lost network is remembered for the report."""
        if isinstance(error, HarnessNetworkError) or (
            isinstance(error, lib_http.HttpError) and error.status is None
        ):
            self.cleanup_network = True
        return str(error)

    def api_get(self, path: str):
        """
        Query the configured environment with a bounded HTTP request.

        HTTP errors identify the missing deployment endpoint.
        """
        url = self.api + path
        reply = fetch("GET", url, timeout=20, max_bytes=16 * 1024 * 1024)
        if reply.status != 200 or not reply.content.strip():
            raise AssertionError(
                f"{url}: expected a JSON body, got HTTP {reply.status}"
            )
        return json.loads(reply.content)

    def publish(self, variant: str = "") -> dict:
        """
        Exercise request-changes and independent approval before merging a valid fixture.

        A variant is an application of its own (`remove`), so the scenario that removes
        it never touches the shared one. Cleanup credentials are required up front.
        """
        if variant in self.publications:
            return self.publications[variant]
        token = os.getenv("E2E_MODERATOR_TOKEN", "")
        assert token, "Not configured: E2E_MODERATOR_TOKEN for independent approval"
        assert os.getenv("E2E_ADMIN_TOKEN"), (
            "Not configured: E2E_ADMIN_TOKEN for publication cleanup"
        )
        moderator = GitHub(token)
        actor = moderator.request("GET", "user")["login"]
        author = self.author.request("GET", "user")["login"]
        assert actor.lower() != author.lower(), (
            "Moderator must be different from PR author"
        )
        submission = self.submit("valid", variant=variant)
        self.check(submission, "catalog-checks", "success")
        root = f"repos/{self.repo}/pulls/{submission['number']}"
        moderator.request(
            "POST",
            root + "/reviews",
            {
                "event": "REQUEST_CHANGES",
                "body": "E2E reject path verification",
                "commit_id": submission["head"],
            },
        )
        blocked = self.author.request("GET", root)
        assert blocked["mergeable_state"] == "blocked", (
            "Change request must block merge"
        )
        moderator.request(
            "POST",
            root + "/reviews",
            {
                "event": "APPROVE",
                "body": "E2E fixture reviewed",
                "commit_id": submission["head"],
            },
        )
        # Recorded before the merge: a failure halfway must still be hidden by cleanup.
        self.published_ids.append(submission["id"])
        merged = moderator.request(
            "PUT",
            root + "/merge",
            {"merge_method": "squash", "sha": submission["head"]},
        )
        assert merged["merged"], "Approved fixture was not merged"
        self.merged_ids.add(submission["id"])
        self.published.append(submission)
        self.publications[variant] = submission
        wait_for(
            lambda: self.author.collection(
                f"repos/{self.repo}/actions/workflows/publish.yml/runs?head_sha={merged['sha']}",
                "workflow_runs",
            ),
            lambda rows: any(row["status"] == "completed" for row in rows),
            "publish completion",
            self.timeout,
        )
        application = self.api_get("/applications/" + submission["id"])
        submission["application"] = application
        submission["merge"] = merged["sha"]
        return submission

    def publish_update(self) -> dict:
        """
        Create a newer pinned fixture commit, approve its checked PR and await publication.

        The application identity stays fixed while the package and source SHA change.
        """
        original = self.publish()
        root = f"repos/{self.fixtures_repo}"
        content = self.author.request(
            "GET",
            root + f"/contents/valid/src/appmeta/manifest.json?ref={self.source_sha}",
        )
        data = rewrite_manifest(
            base64.b64decode(content["content"]), original["id"], "2.0.0"
        )
        base = self.author.request("GET", root + "/git/commits/" + self.source_sha)
        tree = self.author.request(
            "POST",
            root + "/git/trees",
            {
                "base_tree": base["tree"]["sha"],
                "tree": [
                    {
                        "path": "valid/src/appmeta/manifest.json",
                        "mode": "100644",
                        "type": "blob",
                        "content": data.decode(),
                    }
                ],
            },
        )
        commit = self.author.request(
            "POST",
            root + "/git/commits",
            {
                "message": f"E2E update {self.id}",
                "tree": tree["sha"],
                "parents": [self.source_sha],
            },
        )
        ref = f"e2e/{self.id}/version2"
        self.author.request(
            "POST",
            root + "/git/refs",
            {"ref": "refs/heads/" + ref, "sha": commit["sha"]},
        )
        self.refs.append((self.fixtures_repo, ref))
        self.source_sha = commit["sha"]
        submission = self.submit("valid", "new-version")
        self.check(submission, "catalog-checks", "success")
        assert "/compare/" in self.comment(submission), (
            "Missing previous-version source diff"
        )
        moderator = GitHub(os.environ["E2E_MODERATOR_TOKEN"])
        path = f"repos/{self.repo}/pulls/{submission['number']}"
        moderator.request(
            "POST",
            path + "/reviews",
            {
                "event": "APPROVE",
                "body": "E2E update reviewed",
                "commit_id": submission["head"],
            },
        )
        merged = moderator.request(
            "PUT",
            path + "/merge",
            {"merge_method": "squash", "sha": submission["head"]},
        )
        assert merged["merged"]
        return wait_for(
            lambda: self.api_get("/applications/" + original["id"]),
            lambda item: item["current_version"]["version"] == "2.0.0",
            "published version 2.0.0",
            self.timeout,
        )

    def sweep_refs(self) -> list[str]:
        """
        Delete this run's branches that were never recorded, and close their open PRs.

        A POST that answered 5xx may still have been applied. Only `e2e/<run id>/...` is touched: another person's run shares the `e2e/` prefix.
        """
        errors = []
        known = set(self.refs)
        repo = self.repo
        try:
            names = self.author.refs_with_prefix(repo, f"e2e/{self.id}/")
        except HARNESS_ERRORS as error:
            return [self.cleanup_error(error)]
        for ref in names:
            if not ref.startswith(f"e2e/{self.id}/") or (repo, ref) in known:
                continue
            # Deleting the branch closes its PR anyway, so a failed close does not stop it.
            for step in (self.close_open_pulls, self.author.delete_ref):
                try:
                    step(repo, ref)
                except HARNESS_ERRORS as error:
                    errors.append(self.cleanup_error(error))
        return errors

    def close_open_pulls(self, repo: str, ref: str) -> None:
        """Close the open PRs whose head is `ref`."""
        owner = repo.split("/")[0]
        pulls = self.author.collection(
            f"repos/{repo}/pulls?state=open&head={owner}:{quote(ref, safe='/')}"
        )
        for pull in pulls:
            self.author.request(
                "PATCH", f"repos/{repo}/pulls/{pull['number']}", {"state": "closed"}
            )

    def cleanup(self) -> list[str]:
        """
        Remove only resources recorded for this run, unless keep was requested.

        Failed cleanup is returned as evidence and must make the suite unsuccessful.
        """
        if self.keep:
            return []
        errors = []
        for case in self.submissions.values():
            try:
                if owned_ref(case["ref"], self.id):
                    pull = self.author.request(
                        "GET", f"repos/{self.repo}/pulls/{case['number']}"
                    )
                    if pull["state"] == "open":
                        self.author.request(
                            "PATCH",
                            f"repos/{self.repo}/pulls/{case['number']}",
                            {"state": "closed"},
                        )
            except HARNESS_ERRORS as error:
                errors.append(self.cleanup_error(error))
        for repo, ref in self.refs:
            if owned_ref(ref, self.id):
                try:
                    self.author.delete_ref(repo, ref)
                except HARNESS_ERRORS as error:
                    errors.append(self.cleanup_error(error))
        errors += self.sweep_refs()
        for application_id in self.published_ids:
            try:
                reply = lib_http.request(
                    "PATCH",
                    self.api + "/applications/" + application_id,
                    json={"hidden": True},
                    headers={
                        "Authorization": "Bearer " + os.environ["E2E_ADMIN_TOKEN"]
                    },
                    timeout=20,
                    accept={404},
                )
                # 404 is fine for an app that was never merged or was removed by its PR;
                # for a merged one it may be a publication that has not landed yet.
                if (
                    reply.status == 404
                    and application_id in self.merged_ids
                    and application_id not in self.removed_ids
                ):
                    errors.append(
                        f"Could not hide merged app {application_id}: HTTP 404"
                    )
            except HARNESS_ERRORS as error:
                errors.append(self.cleanup_error(error))
        return errors
