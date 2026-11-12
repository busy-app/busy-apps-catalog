# Maintainer setup

For each environment repository, configure CATALOG_API and secret CATALOG_CI_TOKEN.
Issue the token through the backend CLI with role ci-upload; keep the secret out of Git.
Set E2E author, independent moderator and cleanup admin tokens in that environment.

Protect main with required validate and catalog-checks, one independent CODEOWNERS
approval, stale approval dismissal and approval by someone other than the last pusher.
Allow squash merges and forbid force-push/deletion. Remove temporary bootstrap admin
bypasses after initial setup. A maintenance label is for reviewed tool/documentation
changes; application PRs may change exactly one manifest.

CODEOWNERS points to @busy-app/catalog-moderators; ensure that team exists and has
repository access before requiring code-owner reviews.

Promote the same reviewed CI code dev → stage → prod under the environment-repository
task. Change only endpoint and credentials per environment. Do not copy secrets.
Publish consumes the approved artifact without rebuilding. Backend upload/notify retries
5xx only; missing artifacts or active change requests block publication.

## Dependabot

Dependabot pull requests carry the `maintenance` label from `.github/dependabot.yml`, so they
go through the maintenance path (validate, audit and tests; no application publication) and
still need an independent approval before merge.
