# Working on catalog CI

## Scope and layout

This repository stores application catalog manifests and the tools that validate,
build, report and publish them. The backend service is maintained separately.
Python dependencies belong in `tools/requirements.txt`; keep the root for repository documentation.

## Architecture

- Representation: workflow YAML and CLI entry points parse inputs and expose results.
- Application: build, check, report and publication commands orchestrate use cases.
- Domain: deterministic package, manifest and review eligibility rules.
- Infrastructure: GitHub, Git, process, archive and backend transport adapters.

Keep responsibilities separate and never execute submitted code in a job with
write permissions or secrets. Fetch application source at a full pinned SHA.

## Validation and publication

Run `python -m pytest -q` from the repository root after tool changes.
Validate workflow syntax and retain full-SHA pins for every action.
App submission PRs change exactly one manifest; tool changes use the maintainer path.
Do not weaken checks to make an E2E fixture pass. Publication must use the approved
CI artifact without rebuilding, and reject stale approvals or active change requests.
Never print tokens or commit secrets. Report skipped integrations and unfinished E2E
steps explicitly.

## Assisting an application author

Change only `applications/<app_id>/manifest.yml` in a submission PR and follow
`CONTRIBUTING.md` and `TERMS.md`. Pin a full source SHA,
keep the lock file in the source repository, and reference real native screenshots.
Do not submit a prebuilt package or duplicate appmeta fields in the catalog manifest.
Describe AI assistance accurately; do not check ownership or device-testing declarations
without evidence from the author. An update requires a newer application version.

Code, comments, docstrings, OpenAPI descriptions, docs, commit messages and PR descriptions are in English only.
