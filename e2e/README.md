# Catalog E2E

Install `tools/requirements.txt` and `e2e/requirements.txt` in Python 3.13+ with `gh`
available, authenticate gh as the author, and export the settings in `.env.example`.
Explicit role tokens override gh credentials. The moderator must be a different user.
`E2E_ADMIN_TOKEN` is required before publication so cleanup can hide published fixtures.

Run `make e2e ENV=dev`; failures are expected for incomplete MVP tasks and are written
to `e2e-report.md`. No scenario is skipped or xfailed. Run `E2E_ARGS=--keep make e2e`
to retain branches and PRs for investigation. Normally only resources recorded under
the current exact `e2e/<run>` namespace are removed; foreign resources are untouched.

Default waits are 900 seconds, or 1800 for the real device report. For a fast readiness
baseline, override `E2E_TIMEOUT_S` and `E2E_REAL_TIMEOUT_S`; short deadlines do not make
missing features pass. Use the normal deadlines for acceptance.

The harness uses the exported `/categories` contract, corresponding to the singular
`/category` shorthand in the older task text. No API routes are changed by this suite.
