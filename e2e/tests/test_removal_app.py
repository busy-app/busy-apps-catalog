"""The removal scenario works on its own application."""

import re
from unittest.mock import Mock

import pytest

from e2e.domain import app_id


def test_variant_id_differs_and_is_well_formed() -> None:
    from e2e.domain import app_id

    run = "20261111174253198166"
    shared, own = app_id("valid", run), app_id("valid", run, "remove")
    assert shared == app_id("valid", run, "")
    assert own != shared
    assert own.startswith("e2e.catalog.") and re.fullmatch(r"[A-Za-z0-9_.]+", own)
    assert len(own) <= 64
    assert own == app_id("valid", run, "remove")
    assert own.endswith(shared[-10:])


def test_variant_ids_differ_between_runs() -> None:
    from e2e.domain import app_id

    assert app_id("valid", "a", "remove") != app_id("valid", "b", "remove")


def test_submit_variant_uses_variant_app_id() -> None:
    """The manifest id comes from app_id(fixture, run, variant); checked via the id helper."""
    from e2e.application import Run
    from e2e.domain import app_id

    run = Run("dev")
    assert app_id("valid", run.id, "remove") != app_id("valid", run.id)


class Stop(Exception):
    """Raised by a stub to end a scenario halfway."""


def run_with_stubs(monkeypatch):
    """A Run whose GitHub clients and PR helpers are stubs."""
    from e2e import application
    from e2e.application import Run

    moderator = Mock()
    moderator.request.side_effect = lambda method, path, data=None: (
        {"login": "moderator"}
        if path == "user"
        else (_ for _ in ()).throw(Stop("merge failed"))
        if method == "PUT"
        else {}
    )
    monkeypatch.setattr(application, "GitHub", Mock(return_value=moderator))
    monkeypatch.setenv("E2E_MODERATOR_TOKEN", "m")
    monkeypatch.setenv("E2E_ADMIN_TOKEN", "a")
    run = Run("dev")
    run.id = "20261111000000000000"
    run.author = Mock()
    run.author.request.return_value = {"login": "author", "mergeable_state": "blocked"}
    run.author.refs_with_prefix.return_value = []
    run.submit = Mock(
        side_effect=lambda fixture, case="", invalid=False, extra=False, variant="": {
            "id": app_id(fixture, run.id, variant),
            "number": 1,
            "head": "h",
            "ref": f"e2e/{run.id}/{fixture}",
        }
    )
    run.check = Mock()
    return run


def test_the_id_is_recorded_before_the_merge_and_cleanup_hides_both(monkeypatch):
    """A scenario that dies at the merge still gets its app hidden; the shared app is separate."""
    from e2e import application

    run = run_with_stubs(monkeypatch)
    for variant in ("", "remove"):
        try:
            run.publish(variant)
        except Stop:
            pass
    assert run.published_ids == [
        app_id("valid", run.id),
        app_id("valid", run.id, "remove"),
    ]
    sent = []
    monkeypatch.setattr(
        application.lib_http,
        "request",
        lambda method, url, **kw: sent.append((method, url, kw)) or Mock(status=200),
    )
    assert run.cleanup() == []
    hidden = [url.rsplit("/", 1)[1] for method, url, kw in sent if method == "PATCH"]
    assert sorted(hidden) == sorted(run.published_ids)
    assert all(kw["json"] == {"hidden": True} for _, _, kw in sent)


def test_removal_scenario_publishes_its_own_variant(monkeypatch):
    """`remove_published` asks for the `remove` application, never the shared one."""
    run = run_with_stubs(monkeypatch)
    run.publish = Mock(side_effect=Stop)
    with pytest.raises(Stop):
        run.remove_published()
    run.publish.assert_called_once_with("remove")


def test_a_merged_app_that_cannot_be_hidden_is_an_error(monkeypatch):
    """404 means "gone" only for an app that was never merged or was removed by its PR."""
    from e2e import application

    run = run_with_stubs(monkeypatch)
    shared = app_id("valid", run.id)
    run.published_ids = [shared]
    run.merged_ids = {shared}
    monkeypatch.setattr(
        application.lib_http, "request", lambda *a, **k: Mock(status=404)
    )
    assert "Could not hide" in run.cleanup()[0]
    run.removed_ids = {shared}
    assert run.cleanup() == []


def test_the_buildable_ids_fit_the_busy_cli_limit() -> None:
    """The real run failed on a 35-character id: `manifest.id must be at most 32 characters`."""
    run = "20261111205705691618"
    assert len(app_id("valid", run)) <= 32
    assert len(app_id("valid", run, "remove")) <= 32


def test_the_fixture_source_branch_does_not_block_other_run_branches(monkeypatch):
    """Git refuses `e2e/<id>/version2` while `e2e/<id>` exists: the source branch lives under the run namespace."""
    from e2e import application
    from e2e.domain import owned_ref

    run = application.Run("dev")
    run.id = "20261111000000000000"
    created = []
    run.author = Mock()

    def request(method, path, data=None):
        if method == "POST" and path.endswith("/git/refs"):
            created.append(data["ref"])
        if path.endswith("/git/trees?recursive=1") or "/git/trees/" in path:
            return {"tree": [], "truncated": False}
        return {
            "object": {"sha": "s"},
            "tree": {"sha": "t"},
            "sha": "c",
            "content": "e30=",
        }

    run.author.request.side_effect = request
    run.source()
    assert created and all(
        ref.startswith(f"refs/heads/e2e/{run.id}/") for ref in created
    )
    assert all(owned_ref(ref, run.id) for _, ref in run.refs)
