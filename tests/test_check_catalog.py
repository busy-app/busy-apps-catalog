"""Tests for published version and firmware compatibility checks."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from tools.check_catalog import (
    CatalogUnavailableError,
    HttpCatalogSource,
    compare_catalog,
    compare_catalog_with_diff,
    repository_key,
    source_compare_url,
)


def test_compare_catalog_versions() -> None:
    """
    Reject repeated application versions and newer required firmware APIs.

    Versions are compared numerically rather than lexicographically.
    """
    source = Mock()
    source.application.return_value = {"current_version": {"version": "1.10.0"}}
    source.api_lines.return_value = [
        {"is_latest_release": True, "is_release": True, "api": "27.14.0"}
    ]
    assert {
        item["id"]
        for item in compare_catalog(source, "app.busy.clock", "1.2.0", "28.0.0")
    } == {"version_not_newer", "firmware_too_old"}
    assert compare_catalog(source, "app.busy.clock", "1.11.0", "27.14.0") == []


def test_unavailable_catalog_is_explicitly_skipped() -> None:
    """
    Distinguish an unavailable catalog from a successful compatibility check.

    Neither API call failure is interpreted as a positive validation result.
    """
    source = Mock()
    source.application.side_effect = CatalogUnavailableError
    source.api_lines.side_effect = CatalogUnavailableError
    findings = compare_catalog(source, "app.busy.clock", "1.0.0", "27.14.0")
    assert {item["result"] for item in findings} == {"not_run"}
    assert {item["id"] for item in findings} == {
        "version_unavailable",
        "firmware_unavailable",
    }


def test_source_compare_url_requires_same_repository_and_full_shas() -> None:
    """Create a compare link only when both published and submitted source refs match."""
    existing = {
        "current_version": {
            "links": {
                "source_code": {
                    "uri": "https://github.com/acme/clock.git",
                    "commit_sha": "a" * 40,
                }
            }
        }
    }
    assert (
        source_compare_url(existing, "https://github.com/acme/clock", "b" * 40)
        == f"https://github.com/acme/clock/compare/{'a' * 40}...{'b' * 40}"
    )
    assert (
        source_compare_url(existing, "https://github.com/other/clock", "b" * 40) is None
    )
    assert (
        source_compare_url(existing, "https://github.com/acme/clock", "short") is None
    )


def published_from(origin: str, version: str = "1.0.0") -> Mock:
    """A catalog source with `version` published from `origin`."""
    source = Mock()
    source.application.return_value = {
        "current_version": {
            "version": version,
            "links": {"source_code": {"uri": origin, "commit_sha": "a" * 40}},
        }
    }
    source.api_lines.return_value = [
        {"is_latest_release": True, "is_release": True, "api": "27.14.0"}
    ]
    return source


def check_update(source: Mock, origin: str) -> tuple[list[dict[str, str]], str | None]:
    """Compare an update to version 1.1.0 from `origin` with the published app."""
    return compare_catalog_with_diff(
        source, "app.busy.clock", "1.1.0", "27.14.0", origin, "b" * 40
    )


STORED = "https://github.com/iMalkav/busybar-world-clock.git"


@pytest.mark.parametrize(
    "origin",
    [
        STORED,
        "https://github.com/iMalkav/busybar-world-clock",
        "https://github.com/imalkav/busybar-world-clock.git",
        "https://github.com/IMALKAV/BusyBar-World-Clock",
        "https://github.com/iMalkav/busybar-world-clock.GIT",
    ],
)
def test_spellings_of_one_repository_match(origin: str) -> None:
    """The same cases as the backend rule: case and `.git` in any case."""
    assert repository_key(origin) == "imalkav/busybar-world-clock"
    findings, url = check_update(published_from(STORED), origin)
    assert findings == []
    assert url == (
        f"https://github.com/iMalkav/busybar-world-clock/compare/{'a' * 40}...{'b' * 40}"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/iMalkav/busybar-world-clock-2",
        "https://github.com/someone/busybar-world-clock.git",
        "https://github.com/iMalkav/busybar-world-clock.git.git",
        "https://github.com/\u212aelvin/busybar-world-clock",
    ],
)
def test_update_from_another_repository_fails(origin: str) -> None:
    """Another owner or name, a double `.git` or a non-ASCII name is another repository."""
    findings, url = check_update(published_from(STORED), origin)
    assert findings == [
        {
            "id": "source_repository_changed",
            "result": "fail",
            "detail": f"published from {STORED}, this update is from {origin}",
        }
    ]
    assert url is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://gitlab.com/iMalkav/busybar-world-clock",
        "https://github.com/iMalkav/.git",
        "https://github.com/a%2Fb/clock",
    ],
)
def test_origins_without_a_key_match_only_themselves(origin: str) -> None:
    """Like the backend: no key, so only the exact same string is the same origin."""
    assert repository_key(origin) is None
    assert check_update(published_from(origin), origin)[0] == []
    assert check_update(published_from(origin), STORED)[0][0]["id"] == (
        "source_repository_changed"
    )


def test_missing_published_link_is_not_a_silent_pass() -> None:
    """A published version without a source link is reported as not checked."""
    source = published_from(STORED)
    source.application.return_value = {"current_version": {"version": "1.0.0"}}
    findings, url = check_update(source, "https://github.com/someone/clock")
    assert findings == [{"id": "version_unavailable", "result": "not_run"}]
    assert url is None


def test_repository_is_checked_even_if_the_published_version_is_odd() -> None:
    """A published version string that does not parse does not skip the repository."""
    findings, _ = check_update(
        published_from(STORED, version="1.0"), "https://github.com/someone/clock"
    )
    assert [item["id"] for item in findings] == [
        "source_repository_changed",
        "version_unavailable",
    ]


def test_new_app_and_no_origin_give_no_repository_finding() -> None:
    """A new app has nothing to compare; a call without an origin skips the rule."""
    source = published_from(STORED)
    source.application.return_value = None
    assert check_update(source, "https://github.com/someone/clock")[0] == []
    assert (
        compare_catalog(published_from(STORED), "app.busy.clock", "1.1.0", "27.14.0")
        == []
    )


def test_catalog_comparison_returns_previous_source_diff() -> None:
    """Reuse the published application lookup to report a source comparison link."""
    source = Mock()
    source.application.return_value = {
        "current_version": {
            "version": "1.0.0",
            "links": {
                "source_code": {
                    "uri": "https://github.com/acme/clock",
                    "commit_sha": "a" * 40,
                }
            },
        }
    }
    source.api_lines.return_value = [
        {"is_latest_release": True, "is_release": True, "api": "27.14.0"}
    ]

    findings, url = compare_catalog_with_diff(
        source,
        "app.busy.clock",
        "1.1.0",
        "27.14.0",
        "https://github.com/acme/clock",
        "b" * 40,
    )

    assert findings == []
    assert url == f"https://github.com/acme/clock/compare/{'a' * 40}...{'b' * 40}"


def test_public_get_sends_ci_user_agent() -> None:
    """Public catalog reads carry an explicit User-Agent, not Python-urllib."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("User-Agent", ""))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = HttpCatalogSource(f"http://127.0.0.1:{server.server_port}")
        assert source.api_lines() == []
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["busy-apps-catalog-ci"]
