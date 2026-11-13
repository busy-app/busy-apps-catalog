"""Cloudflare Access service-token headers in the shared HTTP client, without network."""

import httpx2
import pytest

from tools import lib_http
from tools.lib_http import HttpError, request

ID = "cf-id-0123"
SECRET = "cf-secret-4567"
BACKEND = "https://cloud.dev.busy.app"
MESSAGE = (
    "Cloudflare Access refused the request: "
    "check the CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET secrets"
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test with no Access or backend variables."""
    for name in (
        "CF_ACCESS_CLIENT_ID",
        "CF_ACCESS_CLIENT_SECRET",
        "CATALOG_API",
        "E2E_API",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lib_http.time, "sleep", lambda _seconds: None)


@pytest.fixture
def token(monkeypatch):
    """Both secrets set, backend given by CATALOG_API."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", ID)
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("CATALOG_API", BACKEND)


def recorder(answers):
    """A handler answering from a list (exceptions are raised); returns (handler, seen)."""
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler, seen


def ok():
    return httpx2.Response(200, content=b"{}")


def has_access_headers(req) -> bool:
    return any(name.lower().startswith("cf-access") for name in req.headers)


def assert_no_secrets(error: BaseException) -> None:
    for text in (str(error), repr(error)):
        assert ID not in text and SECRET not in text


def test_headers_added_for_backend(token, mock_http) -> None:
    """Both secrets and a backend URL: both headers go to the backend."""
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", f"{BACKEND}/api/catalog/v1/apps")
    assert seen[0].headers["CF-Access-Client-Id"] == ID
    assert seen[0].headers["cf-access-client-secret"] == SECRET


def test_headers_added_when_backend_from_e2e_api(monkeypatch, mock_http) -> None:
    """E2E_API with a path identifies the backend by its origin only."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", ID)
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("E2E_API", f"{BACKEND}/api/catalog/v1")
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", f"{BACKEND}/other/path")
    assert seen[0].headers["cf-access-client-id"] == ID


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/repos/x/y",
        "https://bucket.r2.cloudflarestorage.com/obj?X-Amz-Signature=1",
        "https://raw.githubusercontent.com/x/y/main/f",
        "https://cdn.example.test/file",
    ],
)
def test_no_headers_to_foreign_hosts(url, token, mock_http) -> None:
    """GitHub, presigned storage and CDNs never get the service token."""
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", url)
    assert not has_access_headers(seen[0])


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.dev.busy.app.evil.com/x",
        "https://evil.com/cloud.dev.busy.app",
        "https://evil.com/?u=https://cloud.dev.busy.app",
        "https://xcloud.dev.busy.app/x",
        "http://cloud.dev.busy.app/x",
        "https://cloud.dev.busy.app:8443/x",
        "https://cloud.dev.busy.app@evil.com/x",
    ],
)
def test_lookalike_hosts_are_not_backend(url, token, mock_http) -> None:
    """Substring, other scheme, other port and userinfo tricks do not match."""
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", url)
    assert not has_access_headers(seen[0])


@pytest.mark.parametrize(
    ("configured", "url"),
    [
        ("https://CLOUD.dev.busy.app", "https://cloud.dev.busy.app/x"),
        ("https://cloud.dev.busy.app", "https://Cloud.Dev.Busy.App/x"),
        ("https://cloud.dev.busy.app:443", "https://cloud.dev.busy.app/x"),
        ("https://cloud.dev.busy.app", "https://cloud.dev.busy.app:443/x"),
        ("http://cloud.dev.busy.app:80", "http://cloud.dev.busy.app/x"),
    ],
)
def test_host_normalisation(configured, url, monkeypatch, mock_http) -> None:
    """Host case and default ports do not change the match."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", ID)
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("CATALOG_API", configured)
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", url)
    assert seen[0].headers["cf-access-client-id"] == ID


def test_no_secrets_means_no_headers_even_to_backend(monkeypatch, mock_http) -> None:
    """Without the secrets the request is as before."""
    monkeypatch.setenv("CATALOG_API", BACKEND)
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", f"{BACKEND}/x")
    assert not has_access_headers(seen[0])


def test_empty_secrets_count_as_unset(monkeypatch, mock_http) -> None:
    """Empty strings behave like absent variables."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "")
    monkeypatch.setenv("CATALOG_API", BACKEND)
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", f"{BACKEND}/x")
    assert not has_access_headers(seen[0])


@pytest.mark.parametrize("present", ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"])
@pytest.mark.parametrize("other", [None, ""])
def test_half_configured_is_a_config_error(
    present, other, monkeypatch, mock_http
) -> None:
    """One variable without the other: error naming both, no request sent, no value leaked."""
    from tools.lib_http import AccessConfigError

    monkeypatch.setenv(present, ID if present.endswith("ID") else SECRET)
    if other is not None:
        monkeypatch.setenv(
            "CF_ACCESS_CLIENT_SECRET"
            if present.endswith("ID")
            else "CF_ACCESS_CLIENT_ID",
            other,
        )
    monkeypatch.setenv("CATALOG_API", BACKEND)
    handler, seen = recorder([ok()])
    mock_http(handler)
    with pytest.raises(AccessConfigError) as error:
        request("GET", f"{BACKEND}/x")
    assert "CF_ACCESS_CLIENT_ID" in str(error.value)
    assert "CF_ACCESS_CLIENT_SECRET" in str(error.value)
    assert_no_secrets(error.value)
    assert seen == []


def test_half_configured_does_not_break_foreign_hosts(monkeypatch, mock_http) -> None:
    """The configuration error is raised for backend requests only."""
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", ID)
    monkeypatch.setenv("CATALOG_API", BACKEND)
    handler, seen = recorder([ok()])
    mock_http(handler)
    request("GET", "https://api.github.com/x")
    assert not has_access_headers(seen[0])


def test_redirect_to_access_login_is_refused(token, mock_http) -> None:
    """302 to *.cloudflareaccess.com is AccessRefused with the exact sentence."""
    from tools.lib_http import AccessRefused

    location = "https://team.cloudflareaccess.com/cdn-cgi/access/login/cloud.dev.busy.app?kid=1"
    handler, seen = recorder([httpx2.Response(302, headers={"Location": location})])
    mock_http(handler)
    with pytest.raises(AccessRefused) as error:
        request("GET", f"{BACKEND}/x")
    assert str(error.value) == MESSAGE
    assert error.value.status == 302
    assert isinstance(error.value, HttpError)
    assert "cloudflareaccess" not in repr(error.value)
    assert_no_secrets(error.value)
    assert len(seen) == 1


def test_403_with_access_header_is_refused(token, mock_http) -> None:
    """403 with a cf-access* header is AccessRefused."""
    from tools.lib_http import AccessRefused

    handler, _ = recorder(
        [httpx2.Response(403, headers={"cf-access-authenticated-user-email": "a"})]
    )
    mock_http(handler)
    with pytest.raises(AccessRefused) as error:
        request("GET", f"{BACKEND}/x")
    assert str(error.value) == MESSAGE and error.value.status == 403
    assert_no_secrets(error.value)


def test_403_with_access_body_is_refused(token, mock_http) -> None:
    """403 whose body mentions Cloudflare Access is AccessRefused."""
    from tools.lib_http import AccessRefused

    handler, _ = recorder(
        [httpx2.Response(403, content=b"<html>Forbidden - Cloudflare Access</html>")]
    )
    mock_http(handler)
    with pytest.raises(AccessRefused) as error:
        request("GET", f"{BACKEND}/x")
    assert str(error.value) == MESSAGE


def test_access_refusal_without_secrets_is_still_refused(
    monkeypatch, mock_http
) -> None:
    """A guarded backend answering a request with no token is also reported as Access."""
    from tools.lib_http import AccessRefused

    monkeypatch.setenv("CATALOG_API", BACKEND)
    location = "https://team.cloudflareaccess.com/login"
    handler, _ = recorder([httpx2.Response(302, headers={"Location": location})])
    mock_http(handler)
    with pytest.raises(AccessRefused):
        request("GET", f"{BACKEND}/x")


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(403, content=b"forbidden"),
        httpx2.Response(403, headers={"x-other": "1"}, content=b"nope"),
    ],
)
def test_plain_403_keeps_old_behaviour(answer, token, mock_http) -> None:
    """A 403 without Access markers is a plain HttpError."""
    from tools.lib_http import AccessRefused

    handler, _ = recorder([answer])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", f"{BACKEND}/x")
    assert not isinstance(error.value, AccessRefused)
    assert str(error.value) == "HTTP 403" and error.value.status == 403


def test_plain_302_keeps_old_behaviour(token, mock_http) -> None:
    """A 302 elsewhere is a plain HttpError and is not followed."""
    from tools.lib_http import AccessRefused

    handler, seen = recorder(
        [httpx2.Response(302, headers={"Location": "https://evil.test/x"})]
    )
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", f"{BACKEND}/x")
    assert not isinstance(error.value, AccessRefused)
    assert str(error.value) == "HTTP 302"
    assert len(seen) == 1 and seen[0].url.host == "cloud.dev.busy.app"


def test_lookalike_cloudflareaccess_location_is_plain(token, mock_http) -> None:
    """Location on cloudflareaccess.com.evil.test is not Access."""
    from tools.lib_http import AccessRefused

    handler, _ = recorder(
        [
            httpx2.Response(
                302, headers={"Location": "https://cloudflareaccess.com.evil.test/"}
            )
        ]
    )
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", f"{BACKEND}/x")
    assert not isinstance(error.value, AccessRefused)


def test_redirect_never_followed_so_headers_stay(token, mock_http) -> None:
    """A redirect from the backend to a foreign host triggers no second request."""
    handler, seen = recorder(
        [httpx2.Response(307, headers={"Location": "https://bucket.r2.test/obj"}), ok()]
    )
    mock_http(handler)
    with pytest.raises(HttpError):
        request("GET", f"{BACKEND}/x")
    assert [r.url.host for r in seen] == ["cloud.dev.busy.app"]


def test_retry_carries_headers_each_attempt(token, mock_http) -> None:
    """5xx retried: every attempt has the same two headers."""
    handler, seen = recorder([httpx2.Response(503), httpx2.Response(502), ok()])
    mock_http(handler)
    reply = request("GET", f"{BACKEND}/x")
    assert reply.status == 200 and len(seen) == 3
    for req in seen:
        assert req.headers["cf-access-client-id"] == ID
        assert req.headers["cf-access-client-secret"] == SECRET


def test_retry_after_network_error_carries_headers(token, mock_http) -> None:
    """A network error then success: both attempts carry the headers."""
    handler, seen = recorder([httpx2.ConnectError("boom"), ok()])
    mock_http(handler)
    request("GET", f"{BACKEND}/x")
    assert len(seen) == 2
    assert all(r.headers["cf-access-client-id"] == ID for r in seen)


def test_http_error_text_has_no_secrets(token, mock_http) -> None:
    """A final 500 after retries: the error text holds no secret."""
    handler, _ = recorder([httpx2.Response(500, content=SECRET.encode())])
    mock_http(handler)
    with pytest.raises(HttpError) as error:
        request("GET", f"{BACKEND}/x")
    assert_no_secrets(error.value)
