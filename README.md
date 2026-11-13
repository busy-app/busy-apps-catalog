# BUSY Bar Apps Catalog

This is the public repository for community-developed apps for BUSY Bar. The Apps Catalog is available at [apps.busy.app](http://apps.busy.app/).

The repository contains app manifests with links to source code in the authors' GitHub repositories and the app details displayed in the Catalog. App packages are built from the authors' source code.

## How to submit an app

Submit app manifests in `applications/<app_id>/manifest.yml`.

Create one pull request per app. In the PR template, confirm that you're the app's author or have write access to its repository.

See the [Contribution Guide](CONTRIBUTING.md) for details.

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
python -m pip install -r tools/requirements.txt pytest
python -m pytest -q
python -m tools.validate_manifest applications/APP_ID/manifest.yml
python -m tools.check_package package.tgz APP_ID --output results.json
```

For backend integration, set `CATALOG_BACKEND_SOURCE` to the backend checkout and
`CATALOG_TEST_POSTGRES_DSN` to a disposable PostgreSQL instance. The test creates
and removes its own schema.

GitHub Actions validation has been exercised in the dev repository. The complete
Profit build/report/publication E2E is still being verified; a green local test
suite alone does not establish successful publication.

## Workflow audit boundary

The required two-stage `workflow_run` trigger has one explicit `zizmor` exception for
`dangerous-triggers`, documented at the trigger. Privileged jobs execute default-branch
tools only, PR content is read as data, and submitted source runs in a tokenless isolated
build job. Artifact results are parsed as data before creating comments/checks. The audit otherwise runs offline and uses no global suppression for this finding.

## Authors

Read the [Contribution Guide](CONTRIBUTING.md) and [Terms for Authors](TERMS.md).

The [example manifest](example/com.example.weather/manifest.yml) is a valid public fixture.

Maintainers can find setup instructions in the [Maintainer Setup Guide](documentation/Maintainers.md).
