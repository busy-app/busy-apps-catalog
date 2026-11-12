"""Compare an application build with published catalog data."""

import json
import re
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import quote, urlsplit

from tools.check_package import SEMVER
from tools.check_registry import outcome
from tools.lib_http import HttpError, request

COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
# The names GitHub allows for owners and repositories, ASCII only.
GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class CatalogUnavailableError(Exception):
    """The catalog cannot provide trustworthy comparison data."""


class CatalogSource(Protocol):
    """Read the current app and latest released device API."""

    def application(self, app_id: str) -> dict | None:
        """
        Return an existing application or None for an unknown ID.

        Unavailable data is reported separately from a missing application.
        """
        ...

    def api_lines(self) -> list[dict]:
        """
        Return published API lines from the catalog.

        An unavailable source raises CatalogUnavailableError.
        """
        ...


def version(value: str) -> tuple[int, int, int]:
    """
    Parse a numeric semantic version for integer comparison.

    Invalid or non-string inputs cannot silently pass compatibility checks.
    """
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ValueError("Invalid semantic version")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def compare_catalog(
    source: CatalogSource, app_id: str, app_version: str, api_version: str
) -> list[dict[str, str]]:
    """
    Compare a built app against its published version and latest firmware API.

    Unavailable catalog data produces explicit skipped findings instead of false passes.
    """
    findings, _ = compare_catalog_with_diff(source, app_id, app_version, api_version)
    return findings


def compare_catalog_with_diff(
    source: CatalogSource,
    app_id: str,
    app_version: str,
    api_version: str,
    new_origin: str = "",
    new_commit: str = "",
) -> tuple[list[dict[str, str]], str | None]:
    """Compare release requirements and produce a source diff for the PR report."""
    incoming = version(app_version)
    required = version(api_version)
    findings = []
    diff_url = None
    try:
        existing = source.application(app_id)
        if existing is not None:
            if new_origin:
                published, _ = published_source(existing)
                findings += repository_findings(published, new_origin)
                diff_url = source_compare_url(existing, new_origin, new_commit)
            current = existing["current_version"]["version"]
            if incoming <= version(current):
                findings.append(
                    outcome("version_not_newer", "fail", f"published: {current}")
                )
    except (CatalogUnavailableError, KeyError, TypeError, ValueError):
        findings.append(outcome("version_unavailable", "not_run"))
    try:
        lines = source.api_lines()
        latest = [
            item
            for item in lines
            if item.get("is_latest_release") is True and item.get("is_release") is True
        ]
        if len(latest) != 1:
            raise CatalogUnavailableError
        current_api = latest[0]["api"]
        if required > version(current_api):
            findings.append(
                outcome(
                    "firmware_too_old", "fail", f"latest released API: {current_api}"
                )
            )
    except (CatalogUnavailableError, KeyError, TypeError, ValueError):
        findings.append(outcome("firmware_unavailable", "not_run"))
    return findings, diff_url


def published_source(existing: Mapping[str, object]) -> tuple[str, object]:
    """
    The source origin and commit of the published version.

    The catalog always sends them; a missing or odd link raises KeyError or TypeError,
    which the caller reports as an unavailable published version.
    """
    source_code = existing["current_version"]["links"]["source_code"]
    origin = source_code["uri"]
    if not isinstance(origin, str):
        raise TypeError("Published source origin is not a string")
    return origin, source_code.get("commit_sha")


def repository_findings(published: str, new_origin: str) -> list[dict[str, str]]:
    """An update must come from the repository the app was published from."""
    if same_origin(published, new_origin):
        return []
    return [
        outcome(
            "source_repository_changed",
            "fail",
            f"published from {published}, this update is from {new_origin}",
        )
    ]


def source_compare_url(
    existing: Mapping[str, object], new_origin: str, new_commit: str
) -> str | None:
    """Build a GitHub compare link only for the same repository and valid SHAs."""
    if not COMMIT_SHA.fullmatch(new_commit):
        return None
    try:
        old_origin, old_commit = published_source(existing)
    except (KeyError, TypeError):
        return None
    old_repo = github_repository(old_origin)
    if (
        old_repo is None
        or not same_origin(old_origin, new_origin)
        or not isinstance(old_commit, str)
        or not COMMIT_SHA.fullmatch(old_commit)
    ):
        return None
    return f"https://github.com/{old_repo}/compare/{old_commit}...{new_commit}"


def github_repository(origin: object) -> str | None:
    """Normalize an HTTPS GitHub origin to its owner/repository path."""
    if not isinstance(origin, str):
        return None
    parsed = urlsplit(origin)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def repository_key(origin: str) -> str | None:
    """
    `owner/repo` of an `https://github.com/<owner>/<repo>` origin, or None.

    The key is in lower case and without `.git`: GitHub matches these names in any
    case and serves a repository with or without `.git`, so such spellings are one
    repository. Anything else (another host, more path segments, non-ASCII names)
    has no key. This is a copy of the backend rule (busy-application-archivarius,
    `catalog/core/repository_origin.py`); keep the two identical.
    """
    parsed = urlsplit(origin)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or not all(GITHUB_NAME.fullmatch(part) for part in parts)
    ):
        return None
    owner, repo = (part.lower() for part in parts)
    repo = repo.removesuffix(".git")
    return f"{owner}/{repo}" if repo else None


def same_origin(first: str, second: str) -> bool:
    """True when the origins are equal or both name one GitHub repository."""
    if first == second:
        return True
    key = repository_key(first)
    return key is not None and key == repository_key(second)


class HttpCatalogSource:
    """Fetch public catalog data without credentials or redirect following."""

    def __init__(self, base_url: str) -> None:
        """
        Validate the endpoint before any network request.

        HTTP is supported only for loopback development.
        """
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid catalog API URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Catalog API requires HTTPS")
        self._base = base_url.rstrip("/")

    def _get(self, path: str) -> object:
        """
        Read a bounded JSON response from the configured catalog.

        HTTP and parse errors are mapped to catalog unavailability.
        """
        try:
            reply = request(
                "GET",
                self._base + "/api/catalog/v1/" + path,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            return json.loads(reply.content)
        except HttpError as error:
            if error.status == 404 and path.startswith("applications/"):
                return None
            raise CatalogUnavailableError from None
        except ValueError:
            raise CatalogUnavailableError from None

    def application(self, app_id: str) -> dict | None:
        """
        Fetch the current published application by its ID.

        A 404 denotes an application not yet present in the catalog.
        """
        result = self._get("applications/" + quote(app_id, safe=""))
        if result is not None and not isinstance(result, dict):
            raise CatalogUnavailableError
        return result

    def api_lines(self) -> list[dict]:
        """
        Fetch API lines for selecting the latest release.

        Malformed results are treated as unavailable.
        """
        result = self._get("api-lines")
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise CatalogUnavailableError
        return result
