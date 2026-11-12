# BUSY Bar application catalog

Application submissions live in `applications/<app_id>/manifest.yml`.
Submit one manifest per pull request and confirm in the PR template that you're the author of the app
or have write access to its repository.

## CI flow

1. `validate.yml` validates PR scope, manifest schema and tool tests.
2. `pipeline.yml` reads the manifest as data, builds pinned source without secrets,
   checks the resulting package, and updates one PR comment and check run.
3. An independent reviewer verifies ownership and reviews the result.
   Current change requests block publication; approval must match the current head SHA.
4. After merge, `publish.yml` uploads the approved artifacts and notifies the backend.
   Missing `CATALOG_API` enables dry-run mode.

## Repository layout

- `applications/`: application manifests.
- `tools/`: CI commands and pinned Python dependencies.
- `schema/`: catalog manifest and backend publication contracts.
- `tests/`: offline tests and optional backend integration test.
- `.github/`: workflows and submission template.

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

The required two-stage `workflow_run` trigger has one explicit zizmor exception for
`dangerous-triggers`, documented at the trigger. Privileged jobs execute default-branch
tools only, PR content is read as data, and submitted source runs in a tokenless isolated
build job. Artifact results are parsed as data before creating comments/checks.
The audit otherwise runs offline without a global suppression for this finding.

## Authors

Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [terms for authors](TERMS.md).
[example/com.example.weather/manifest.yml](example/com.example.weather/manifest.yml) pins the public valid fixture.
Maintainers should read [environment setup](documentation/Maintainers.md).
