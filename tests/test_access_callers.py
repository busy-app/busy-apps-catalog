"""Where the Access refusal and the token travel: every caller of the backend, not only lib_http."""

import httpx2
import pytest

from tools import lib_http
from tools.lib_http import ACCESS_REFUSED

BACKEND = "https://cloud.dev.busy.app"
SECRET = "cf-secret-4567"


@pytest.fixture(autouse=True)
def access(monkeypatch):
    """Access on: the token is set and the backend is known."""
    monkeypatch.setenv("CATALOG_API", BACKEND)
    monkeypatch.delenv("E2E_API", raising=False)
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "cf-id-0123")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", SECRET)


def refuse(request: httpx2.Request) -> httpx2.Response:
    """What Access answers a request without a valid token."""
    return httpx2.Response(
        302, headers={"Location": "https://team.cloudflareaccess.com/login"}
    )


def test_review_cli_names_the_access_secrets(mock_http) -> None:
    """`review.yml` must not report a bare "HTTP 302"."""
    from tools.review_cli import ReviewError, ask_backend

    mock_http(refuse)
    with pytest.raises(ReviewError) as caught:
        ask_backend(BACKEND, "ci-token", {"pr_number": 1})
    assert str(caught.value) == ACCESS_REFUSED and SECRET not in str(caught.value)


def test_review_cli_sends_the_token_to_the_backend_only(mock_http) -> None:
    """The review request carries the Access headers; the answer is read as before."""
    from tools.review_cli import ask_backend

    seen = []

    def handler(request):
        seen.append(request.headers.get("cf-access-client-id"))
        return httpx2.Response(200, json={"ok": True})

    mock_http(handler)
    assert ask_backend(BACKEND, "ci-token", {}) == (200, {"ok": True})
    assert seen == ["cf-id-0123"]


def test_publication_error_text_carries_the_access_message(mock_http) -> None:
    """The publish step shows why, not "HTTP 403: " with nothing after it."""
    from tools.publish_upload import BackendTransport, CatalogApiError

    mock_http(refuse)
    with pytest.raises(CatalogApiError) as caught:
        BackendTransport(BACKEND, "ci-token").notify({"alias": "x"})
    assert ACCESS_REFUSED in str(caught.value) and SECRET not in str(caught.value)


def test_removal_publication_error_carries_the_access_message(mock_http) -> None:
    """Same for the removal request."""
    from tools import publish_remove

    mock_http(refuse)
    with pytest.raises(publish_remove.RemovalError) as caught:
        publish_remove.remove_application(
            BACKEND, "ci-token", "e2e.catalog.valid_0123456789", 1, "reviewer"
        )
    assert ACCESS_REFUSED in str(caught.value)


def test_category_check_says_access_refused(mock_http) -> None:
    """A pipeline step that cannot read the categories names the Access secrets."""
    from tools.check_manifest import public_categories

    mock_http(refuse)
    with pytest.raises(lib_http.AccessRefused):
        public_categories(BACKEND + "/api/catalog/v1/categories")


def test_catalog_lookup_says_access_refused(mock_http) -> None:
    """The comparison with the catalog fails with the Access text, not blank."""
    from tools.check_catalog import CatalogUnavailableError, HttpCatalogSource

    mock_http(refuse)
    with pytest.raises(CatalogUnavailableError) as caught:
        HttpCatalogSource(BACKEND).application("e2e.catalog.valid_0123456789")
    assert ACCESS_REFUSED in str(caught.value)


def test_a_403_with_an_empty_body_and_error_body_is_read_once(mock_http) -> None:
    """The body is a one-shot stream: sniffing it twice would crash the publish."""
    mock_http(
        lambda request: httpx2.Response(
            403, headers={"cf-access-domain": "team"}, content=iter([b""])
        )
    )
    with pytest.raises(lib_http.AccessRefused):
        lib_http.request("GET", BACKEND + "/x", error_body=512)


def test_a_403_body_is_kept_for_the_caller_when_it_is_not_access(mock_http) -> None:
    """A plain backend 403 keeps its own text within `error_body`, once read."""
    mock_http(
        lambda request: httpx2.Response(403, content=iter([b"no role: ci-upload"]))
    )
    with pytest.raises(lib_http.HttpError) as caught:
        lib_http.request("POST", BACKEND + "/x", error_body=7)
    assert not isinstance(caught.value, lib_http.AccessRefused)
    assert caught.value.body == b"no role"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test\\@cloud.dev.busy.app/x",
        "https://evil.test%40cloud.dev.busy.app/x",
        "https://cloud.dev.busy.app@evil.test/x",
        "https://cloud.dev.busy.app\t.evil.test/x",
        "https://evilcloud.dev.busy.app/x",
    ],
)
def test_parser_tricks_do_not_get_the_token(url, mock_http) -> None:
    """The host is decided by the same parser that sends the request."""
    assert not lib_http.is_backend(url)


def test_a_trailing_dot_host_is_the_backend() -> None:
    """`cloud.dev.busy.app.` is the same host."""
    assert lib_http.is_backend("https://cloud.dev.busy.app./x")


def test_a_lookalike_login_host_is_not_access(mock_http) -> None:
    """Only cloudflareaccess.com and its subdomains count as the Access login."""
    mock_http(
        lambda request: httpx2.Response(
            302, headers={"Location": "https://evilcloudflareaccess.com/x"}
        )
    )
    with pytest.raises(lib_http.HttpError) as caught:
        lib_http.request("GET", BACKEND + "/x")
    assert not isinstance(caught.value, lib_http.AccessRefused)
