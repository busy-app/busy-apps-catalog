"""Tests for approved artifact publication without network calls."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

from tools.publish_upload import (
    BackendTransport,
    CatalogApiError,
    api_failure,
    main,
    publish,
    validate_publication,
)


def fixture(tmp_path: Path) -> tuple[dict, Path, Path]:
    """
    Prepare a valid publication and content-addressed artifact files.

    All files are synthetic and remain inside the test directory.
    """
    data = b"artifact"
    digest = hashlib.sha256(data).hexdigest()
    for suffix in (".png", ".tgz", ".tar.gz"):
        (tmp_path / (digest + suffix)).write_bytes(data)
    asset = {"key": f"assets/{digest}.png", "sha256": digest, "size": len(data)}
    record = {
        "alias": "app.busy.clock",
        "name": "Clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
        "author": "author",
        "origin": "https://github.com/busy-app/clock",
        "commit_sha": "b" * 40,
        "category": "clocks",
        "short_description": "A small clock for the bar",
        "description": "A small clock for the BUSY Bar that shows the time and the date.",
        "changelog": "Initial release",
        "package": {
            "key": f"packages/{digest}.tgz",
            "sha256": digest,
            "size": len(data),
        },
        "sources": {
            "key": f"sources/{digest}.tar.gz",
            "sha256": digest,
            "size": len(data),
        },
        "icon": asset,
        "screenshots": [asset],
        "toolchain": "1.0.0",
        "pr_number": 1,
        "head_sha": "c" * 40,
        "merge_sha": "d" * 40,
        "approved_by": "reviewer",
    }
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"status": "pass", "checks": []}))
    return record, results, tmp_path


def test_texts_up_to_the_manifest_limits_pass_the_notify_schema(tmp_path: Path) -> None:
    """
    The notify schema takes what the manifest check takes: 120 characters of short description.

    The schema copy here once kept 80, so a 90-character short description passed every check
    and then failed the publication with "does not match notify schema".
    """
    data, results, directory = fixture(tmp_path)
    data["short_description"] = "x" * 120
    data["description"] = "d" * 4000
    data["changelog"] = "c" * 4000
    assert len(validate_publication(data, results, directory)) == 3
    data["short_description"] = "x" * 121
    with pytest.raises(ValueError, match="notify schema"):
        validate_publication(data, results, directory)


def test_upload_before_notify(tmp_path: Path) -> None:
    """
    Validate files and upload each unique object before sending notify.

    A failed upload prevents the catalog from receiving incomplete publication metadata.
    """
    data, results, directory = fixture(tmp_path)
    files = validate_publication(data, results, directory)
    assert len(files) == 3
    backend = Mock()
    backend.notify.return_value = {"url": "https://example.test/app"}
    assert publish(backend, data, files)["url"] == "https://example.test/app"
    assert backend.method_calls[-1][0] == "notify"
    backend.reset_mock()
    backend.upload_files.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        publish(backend, data, files)
    backend.notify.assert_not_called()


def test_bad_artifacts_and_results_fail(tmp_path: Path) -> None:
    """
    Reject changed artifacts and failed checks before any network request.

    The publication payload must match the shared schema and immutable files.
    """
    data, results, directory = fixture(tmp_path)
    results.write_text('{"status":"fail","checks":[{"id":"build","result":"fail"}]}')
    with pytest.raises(ValueError, match="Checks did not pass"):
        validate_publication(data, results, directory)
    results.write_text('{"status":"pass","checks":[]}')
    file = directory / (data["package"]["sha256"] + ".tgz")
    file.write_bytes(b"corrupt!")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_publication(data, results, directory)


def test_reject_insecure_backend() -> None:
    """
    Refuse to send CI credentials over non-local plaintext HTTP.

    Local development and HTTPS endpoints remain supported.
    """
    with pytest.raises(ValueError, match="HTTPS"):
        BackendTransport("http://example.test", "secret")
    BackendTransport("http://127.0.0.1:8000", "secret")


class FakeBackend:
    """Local HTTP server recording requests and replaying scripted statuses."""

    def __init__(self, statuses: list[int], body: bytes | list[bytes]) -> None:
        """Serve the given statuses in order, then the last one forever."""
        self.requests: list[dict] = []
        bodies = body if isinstance(body, list) else [body]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length),
                    }
                )
                index = min(len(outer.requests), len(statuses)) - 1
                answer = bodies[min(index, len(bodies) - 1)]
                self.send_response(statuses[index])
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            do_PUT = do_POST

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend_factory():
    """Create fake backends and stop them after the test."""
    created = []

    def create(statuses: list[int], body: bytes | list[bytes]) -> FakeBackend:
        backend = FakeBackend(statuses, body)
        created.append(backend)
        return backend

    yield create
    for backend in created:
        backend.close()


def test_notify_sends_json(backend_factory) -> None:
    """Send the metadata as a JSON body with the token and User-Agent."""
    backend = backend_factory([200], b'{"url":"https://example.test/app"}')
    result = BackendTransport(backend.url, "fake").notify({"merge_sha": "d" * 40})
    assert result["url"] == "https://example.test/app"
    request = backend.requests[0]
    assert request["path"] == "/api/catalog/v1/ci/notify"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["User-Agent"] == "busy-apps-catalog-ci"
    assert json.loads(request["body"]) == {"merge_sha": "d" * 40}


class Storage:
    """Fake bucket: records PUTs and answers with a scripted status."""

    def __init__(self, backend_factory, status: int = 200) -> None:
        """Start a fake HTTP server standing in for R2."""
        self.server = backend_factory([status], b"")
        self.status = status

    @property
    def requests(self) -> list[dict]:
        """Requests seen by the fake bucket."""
        return self.server.requests


def presign_body(storage: Storage, names: dict[str, str]) -> bytes:
    """Backend answer: `exists` or an upload instruction pointing at the fake bucket."""
    files = []
    for key, status in names.items():
        item = {"key": key, "status": status}
        if status == "upload":
            item["url"] = f"{storage.server.url}/{key}?X-Amz-Signature=s"
            item["headers"] = {"If-None-Match": "*", "x-amz-checksum-sha256": "abc"}
        files.append(item)
    return json.dumps({"files": files}).encode()


def one_file(tmp_path: Path):
    """A single approved asset on disk."""
    path = tmp_path / "artifact"
    path.write_bytes(b"a")
    filename = hashlib.sha256(b"a").hexdigest() + ".png"
    return [(path, filename)], "assets/" + filename


def test_upload_files_puts_to_bucket_with_issued_headers(
    tmp_path: Path, backend_factory
) -> None:
    """Presign first, then PUT with exactly the issued headers and no CI token."""
    files, key = one_file(tmp_path)
    storage = Storage(backend_factory)
    backend = backend_factory([200], presign_body(storage, {key: "upload"}))
    BackendTransport(backend.url, "fake-token").upload_files(files)
    assert backend.requests[0]["path"] == "/api/catalog/v1/ci/uploads"
    assert json.loads(backend.requests[0]["body"])["files"] == [
        {"type": "assets", "sha256": key.split("/")[1][:-4], "size": 1}
    ]
    put = storage.requests[0]
    assert put["body"] == b"a"
    assert put["headers"]["If-None-Match"] == "*"
    assert put["headers"]["x-amz-checksum-sha256"] == "abc"
    assert "Token" not in put["headers"]


def test_existing_object_is_not_uploaded(tmp_path: Path, backend_factory) -> None:
    """`exists` means no PUT at all."""
    files, key = one_file(tmp_path)
    storage = Storage(backend_factory)
    backend = backend_factory([200], presign_body(storage, {key: "exists"}))
    BackendTransport(backend.url, "t").upload_files(files)
    assert storage.requests == []


def test_put_412_is_not_an_error_and_403_is(tmp_path: Path, backend_factory) -> None:
    """412 = already there; any other 4xx names the file and shows the storage answer."""
    files, key = one_file(tmp_path)
    exists = Storage(backend_factory, 412)
    backend = backend_factory([200], presign_body(exists, {key: "upload"}))
    BackendTransport(backend.url, "t").upload_files(files)
    denied = Storage(backend_factory, 403)
    backend = backend_factory([200], presign_body(denied, {key: "upload"}))
    with pytest.raises(RuntimeError, match=r"Storage rejected.*HTTP 403"):
        BackendTransport(backend.url, "t").upload_files(files)
    assert len(denied.requests) == 1


def test_catalog_requests_carry_the_identity(tmp_path: Path, backend_factory) -> None:
    """The upload-URL request has the explicit User-Agent and the token header."""
    files, key = one_file(tmp_path)
    backend = backend_factory(
        [200], presign_body(Storage(backend_factory), {key: "exists"})
    )
    BackendTransport(backend.url, "fake-token").upload_files(files)
    headers = backend.requests[0]["headers"]
    assert headers["User-Agent"] == "busy-apps-catalog-ci"
    assert headers["Token"] == "fake-token"


def test_retry_only_transient_errors(tmp_path: Path, backend_factory) -> None:
    """Retry a transient 503 and stop immediately on a client error (Cloudflare 403 shown)."""
    files, key = one_file(tmp_path)
    body = presign_body(Storage(backend_factory), {key: "exists"})
    flaky = backend_factory([503, 200], body)
    BackendTransport(flaky.url, "fake").upload_files(files)
    assert len(flaky.requests) == 2
    blocked = backend_factory([403], b"error code: 1010")
    with pytest.raises(RuntimeError, match="did not issue upload URLs.*403.*1010"):
        BackendTransport(blocked.url, "fake").upload_files(files)
    assert len(blocked.requests) == 1


def test_redirect_is_not_followed(tmp_path: Path, backend_factory) -> None:
    """Never follow a redirect, so the token cannot leave the configured host."""
    files, _ = one_file(tmp_path)
    backend = backend_factory([302], b"")
    with pytest.raises(RuntimeError, match="302"):
        BackendTransport(backend.url, "fake").upload_files(files)
    assert len(backend.requests) == 1


def test_a_non_200_from_uploads_is_a_clear_error_without_fallback(
    tmp_path: Path, backend_factory
) -> None:
    """404 or 501 from `ci/uploads` stops the publication: no other upload path exists."""
    files, _ = one_file(tmp_path)
    for status in (404, 501):
        backend = backend_factory([status], b'{"detail":"nope"}')
        with pytest.raises(RuntimeError, match=rf"did not issue upload URLs.*{status}"):
            BackendTransport(backend.url, "t").upload_files(files)
        assert [r["path"] for r in backend.requests] == ["/api/catalog/v1/ci/uploads"]


def test_notify_is_not_retried_after_a_lost_connection(mock_http, monkeypatch) -> None:
    """A lost answer may hide a finished publication; a repeat would get 409."""
    import httpx2

    from tools import lib_http

    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    calls = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        calls.append(req)
        raise httpx2.ReadError("lost", request=req)

    mock_http(handler)
    with pytest.raises(RuntimeError, match="unreachable"):
        BackendTransport("https://api.test", "t").notify({"merge_sha": "d" * 40})
    assert len(calls) == 1


def test_notify_still_retries_a_5xx_and_presign_retries_lost_connections(
    mock_http, monkeypatch
) -> None:
    """A 503 is retried for notify; a lost connection is retried for the upload plan."""
    import httpx2

    from tools import lib_http

    monkeypatch.setattr(lib_http.time, "sleep", lambda _: None)
    answers = [httpx2.Response(503), httpx2.Response(200, json={"url": "u"})]
    mock_http(lambda req: answers.pop(0))
    assert BackendTransport("https://api.test", "t").notify({"a": 1}) == {"url": "u"}

    state = {"calls": 0}

    def flaky(req: httpx2.Request) -> httpx2.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx2.ConnectError("down", request=req)
        return httpx2.Response(200, json={"files": []})

    mock_http(flaky)
    assert BackendTransport("https://api.test", "t")._post("uploads", b"{}") == {
        "files": []
    }
    assert state["calls"] == 2


def test_api_failure_finds_the_backend_answer() -> None:
    """The reason comes from the API error itself or from the error it caused."""
    refused = CatalogApiError(409, '{"detail": "Version must be higher than current"}')
    assert api_failure(refused) == {
        "status": 409,
        "detail": "Version must be higher than current",
    }
    try:
        raise RuntimeError("The catalog did not issue upload URLs") from refused
    except RuntimeError as wrapped:
        assert api_failure(wrapped)["status"] == 409
    assert api_failure(RuntimeError("Storage is unreachable")) == {
        "status": None,
        "detail": "",
    }


def test_main_writes_the_refusal_for_the_report(monkeypatch, tmp_path: Path) -> None:
    """With `--failure`, a refused notify leaves the status and reason in the file."""
    (tmp_path / "notice.json").write_text(json.dumps({"merge_sha": "c" * 40}))
    monkeypatch.setattr("tools.publish_upload.validate_publication", lambda *a: [])

    def refuse(*args) -> dict:
        raise CatalogApiError(
            409, '{"detail": "Application belongs to another repository"}'
        )

    monkeypatch.setattr("tools.publish_upload.publish", refuse)
    monkeypatch.setenv("CATALOG_API", "https://api.test")
    monkeypatch.setenv("CATALOG_CI_TOKEN", "secret-token")
    failure = tmp_path / "failure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "x",
            "--manifest",
            str(tmp_path / "notice.json"),
            "--results",
            str(tmp_path / "results.json"),
            "--artifacts",
            str(tmp_path),
            "--failure",
            str(failure),
        ],
    )
    with pytest.raises(CatalogApiError):
        main()
    assert json.loads(failure.read_text()) == {
        "status": 409,
        "detail": "Application belongs to another repository",
    }
