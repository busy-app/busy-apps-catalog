# Maintaining the catalog

This guide is for the people who run the catalog repositories. App authors need only the
[README](../README.md), the [Contribution Guide](../CONTRIBUTING.md) and the [Terms](../TERMS.md).

## Contents

- [Repository setup](#repository-setup)
- [Secrets and variables](#secrets-and-variables)
- [CI flow](#ci-flow)
- [Repository layout](#repository-layout)
- [Local checks](#local-checks)
- [End-to-end tests](#end-to-end-tests)
- [Check on a real bar](#check-on-a-real-bar)
- [Environments: dev, stage, prod](#environments-dev-stage-prod)
- [Publication did not run](#publication-did-not-run)
- [Removing a check](#removing-a-check)
- [Workflow audit boundary](#workflow-audit-boundary)
- [Dependabot](#dependabot)

## Repository setup

Protect main with required validate and catalog-checks, one independent CODEOWNERS
approval, stale approval dismissal and approval by someone other than the last pusher.
Allow squash merges and forbid force-push/deletion. Remove temporary bootstrap admin
bypasses after initial setup. A maintenance label is for reviewed tool/documentation
changes; application PRs may change exactly one manifest.

CODEOWNERS points to @busy-app/catalog-moderators; ensure that team exists and has
repository access before requiring code-owner reviews.

## Secrets and variables

| Name | Kind | Who provides it | Used by |
|---|---|---|---|
| `CATALOG_API` | variable | maintainers | pipeline, publish, review, E2E: the backend address |
| `CATALOG_CI_TOKEN` | secret | backend CLI, role `ci-upload` | publish and review jobs |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | secrets | DevOps (Cloudflare Access service token) | steps that call the backend; set both or neither |
| `E2E_AUTHOR_TOKEN`, `E2E_MODERATOR_TOKEN` | secrets | maintainers: GitHub tokens of two different users, the author and the independent moderator | E2E only |
| `E2E_ADMIN_TOKEN` | secret | maintainers: a backend token with the admin role | E2E cleanup (hides published fixtures) |

Keep every secret out of Git. The Access secrets are sent to the backend host only and never
to the build job; without them the CI works only while Access is off.

## CI flow

1. **PR validation.** `validate.yml` checks the PR scope, validates the manifest against the schema, and runs tool tests.
2. **Package build and checks.** `pipeline.yml` reads the manifest as data, builds the pinned source without secrets, and checks the resulting package. Then, it updates a single PR comment and check run.
3. **Independent review.** An independent reviewer verifies ownership and reviews the build results.
   Unresolved change requests block publication. Approval must match the current head SHA.
4. **Publication.** After the PR is merged, `publish.yml` uploads the approved artifacts and notifies the backend. If `CATALOG_API` is not set, the workflow runs in dry-run mode.

## Repository layout

- `applications/` — application manifests
- `tools/` — CI commands and pinned Python dependencies
- `schema/` — Catalog manifest and backend publication contracts
- `tests/` — offline tests and optional backend integration test
- `.github/` — workflows and submission template

## Local checks

Use Python 3.13 or newer from the repository root:

```sh
python -m pip install -r tools/requirements.txt -r e2e/requirements.txt pytest
python -m pytest -q
python -m tools.validate_manifest applications/APP_ID/manifest.yml
python -m tools.check_package package.tgz APP_ID --output results.json
```

For backend integration, set `CATALOG_BACKEND_SOURCE` to the backend checkout and
`CATALOG_TEST_POSTGRES_DSN` to a disposable PostgreSQL instance. The test creates
and removes its own schema.

A green local test suite alone does not establish successful publication.

## End-to-end tests

The E2E suite runs real submissions against an environment repository and its backend.
Setup, options and waits are in [e2e/README.md](../e2e/README.md); run it with `make e2e ENV=dev`.

## Check on a real bar

The `bar` job of `pipeline.yml` runs on the organisation-level runner `BusyBarTest`, a real
BUSY Bar on a private USB network. It installs the checked package, watches it for 60 seconds,
saves the front and back screens and the application log, removes the application and uploads
`bar-result-<sha>` as an artifact. The bar has no network, so apps that need one are checked
only for crashes. Re-flashing the bar and isolating the stand belong to the stand's owners.

What it means for a pull request:

- **Pass:** `Application runtime on BAR` is green.
- **The application failed:** the check is red and blocks the merge, with the reason and a
  pointer to the log and screenshots. That is decided only from the result's fields, for a
  result of this very run: a JS exception or a restart seen in the log after the script ran,
  the script never starting although log lines arrived, the bar restarting during the minute
  (same bar, boot time reset), the bar refusing the package (HTTP 400, 413, 415 or 422 on
  stage or install), or the installed app missing or of another version.
- **The stand failed or it is unclear:** `Application runtime on BAR (not run)` is a warning
  and does not block; the moderator decides whether to merge without it. This covers a job that
  did not run or crashed, a missing, broken or foreign result, an unreachable or silent bar
  (it may have hung because of the app, but that cannot be told apart), firmware that did not
  install, a lost or silent serial log, a swapped bar, other HTTP answers (408, 409, 429, 5xx)
  and cleanup failures. The comment says why. When unsure, the result is "not run": the author
  is never blamed for the stand.
- **Time limits:** the `bar` job may wait in the queue for 30 minutes (`BAR_QUEUE_TIMEOUT` in
  `tools/bar_watchdog.py`) and then run for 20 minutes (`timeout-minutes`). A small job,
  `bar-watchdog`, cancels the run when the bar is still queued after 30 minutes (it is the only
  job with `actions: write`; it has no secrets, runs only trusted tools and, like every job that
  may write, only on the default branch, so a manual branch preview has no queue guard). The `report` job
  still runs after a cancel and writes "not run, the BAR stand was not available or did not
  finish in time". Both limits end as not run: the moderator decides without the bar.
- **Not run at all:** if an earlier check already failed (or the PR removes an app), the bar
  stage is skipped.

Publication does not depend on this check. A result with no bar evidence (or one from before the
check existed) publishes normally; evidence that is present must still belong to the package.

## Environments: dev, stage, prod

There are three repositories, dev → stage → prod. The code is identical; only `applications/`,
the variables and the secrets differ (endpoint and credentials per environment; do not copy
secrets). Every change goes to dev first, then a synchronising pull request with the
`maintenance` label carries the same reviewed code to the next repository.

Publish consumes the approved artifact without rebuilding. Backend upload/notify retries
5xx only; missing artifacts or active change requests block publication.

## Publication did not run

If a pull request was merged without an independent approval, or the publication step failed:
approve the merged pull request, then start `publish.yml` by hand with the `merge_sha` of the merge
commit.

## Removing a check

A check that is removed (only a check that never blocked) must have its id added to `RETIRED` in `tools/check_registry.py`:
results recorded before its removal still carry it and are read without it.

## Workflow audit boundary

The required two-stage `workflow_run` trigger has one explicit `zizmor` exception for
`dangerous-triggers`, documented at the trigger. Privileged jobs execute default-branch
tools only, PR content is read as data, and submitted source runs in a tokenless isolated
build job. Artifact results are parsed as data before creating comments/checks. The audit otherwise runs offline and uses no global suppression for this finding.

## Dependabot

Dependabot pull requests carry the `maintenance` label from `.github/dependabot.yml`, so they
go through the maintenance path (validate, audit and tests; no application publication) and
still need an independent approval before merge.
