"""Removal of an app through a PR: who may do it and what the comment says."""

import json
from collections.abc import Callable
from urllib.parse import quote

from tools.check_registry import outcome
from tools.lib_app_id import is_valid
from tools.lib_github import GitHubGateway
from tools.lib_http import HttpError, request

WRITE_ROLES = frozenset({"admin", "maintain", "write"})
FIELDS = ("id", "name", "version", "author")


def published_app(catalog_url: str, app_id: str) -> dict | None:
    """
    The catalog's public card of the app, or None if the catalog does not know it.

    An id that cannot have been published is not looked up. Any other failure is raised
    as `HttpError`, so the caller can tell "not there" from "cannot ask".
    """
    if not catalog_url or not is_valid(app_id):
        return None
    url = f"{catalog_url.rstrip('/')}/api/catalog/v1/applications/{quote(app_id, safe='')}"
    try:
        card = json.loads(request("GET", url, timeout=10).content)
    except HttpError as error:
        if error.status == 404:
            return None
        raise
    return card if isinstance(card, dict) else None


def removal_info(app_id: str, card: dict | None) -> dict[str, object]:
    """What the removal comment shows: the app as the catalog has it, or that it has none."""
    version = card.get("current_version") if card else None
    version = version.get("version") if isinstance(version, dict) else None
    return {
        "id": app_id,
        "name": str(card.get("name") or "") if card else "",
        "version": str(version or ""),
        "author": str(card.get("author") or "") if card else "",
        "published": card is not None,
    }


def may_remove(
    gateway: GitHubGateway, login: str, card: dict, permission: Callable | None = None
) -> bool:
    """The PR author is the app's author (any case) or has write access to the catalog."""
    author = card.get("author")
    if isinstance(author, str) and author.lower() == login.lower():
        return True
    role = (permission or gateway.collaborator_permission)(login)
    return role in WRITE_ROLES


def removal_findings(
    gateway: GitHubGateway, login: str, app_id: str, catalog_url: str
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """
    The checks of a removal PR and the facts for its comment.

    An app the catalog does not have is a warning, not an error. If the catalog cannot be
    asked, only a maintainer can be sure to be allowed, so everyone else gets a check that
    could not run.
    """
    try:
        card = published_app(catalog_url, app_id)
    except HttpError:
        allowed = gateway.collaborator_permission(login) in WRITE_ROLES
        findings = (
            []
            if allowed
            else [outcome("removal_not_owner", "not_run", "catalog unavailable")]
        )
        return findings, removal_info(app_id, None) | {"published": True}
    if card is None:
        return [outcome("removal_not_published", "fail", app_id)], removal_info(
            app_id, None
        )
    if not may_remove(gateway, login, card):
        return [
            outcome("removal_not_owner", "fail", f"{login} is not the author")
        ], removal_info(app_id, card)
    return [], removal_info(app_id, card)
