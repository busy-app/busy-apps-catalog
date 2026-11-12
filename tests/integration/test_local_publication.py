"""Local end-to-end test of the CI client and backend publication endpoint."""

import asyncio
import hashlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4

import pytest

from tools.publish_upload import BackendTransport, publish, validate_publication

DSN = os.environ.get("CATALOG_TEST_POSTGRES_DSN")
BACKEND = os.environ.get("CATALOG_BACKEND_SOURCE")
pytestmark = pytest.mark.skipif(
    not DSN or not BACKEND, reason="Local backend source and PostgreSQL are required"
)


def test_ci_client_to_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Upload objects and publish twice through the real HTTP backend and PostgreSQL.

    All records live in a temporary test schema and all files are synthetic.
    """
    import asyncpg
    import uvicorn

    sys.path.insert(0, BACKEND)
    from catalog.api.app import app

    schema = "test_" + uuid4().hex
    migrations = Path(BACKEND) / "migrations"

    async def prepare() -> None:
        """
        Create a test-only schema, apply migrations and register a test CI token.

        No application data from the default schema is modified.
        """
        connection = await asyncpg.connect(DSN)
        try:
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET search_path TO "{schema}", public')
            for path in sorted(migrations.glob("*.up.sql")):
                await connection.execute(path.read_text())
            await connection.execute(
                "INSERT INTO api_token (name, role, token_hash) VALUES ('ci', 'ci-upload', $1)",
                hashlib.sha256(b"local-test-token").hexdigest(),
            )
        finally:
            await connection.close()

    async def cleanup() -> None:
        """
        Delete the temporary schema created for this isolated test.

        Other PostgreSQL schemas and local storage remain untouched.
        """
        connection = await asyncpg.connect(DSN)
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await connection.close()

    asyncio.run(prepare())
    parts = urlsplit(DSN)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema},public"
    monkeypatch.setenv(
        "POSTGRES_DSN",
        urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        ),
    )
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "storage"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        payload = b"local-only-artifact"
        digest = hashlib.sha256(payload).hexdigest()
        for suffix in (".png", ".tgz", ".tar.gz"):
            (tmp_path / (digest + suffix)).write_bytes(payload)
        asset = {"key": f"assets/{digest}.png", "sha256": digest, "size": len(payload)}
        data = {
            "alias": "local-clock",
            "name": "Clock",
            "version": "1.0.0",
            "api_version": "27.14.0",
            "author": "owner",
            "origin": "https://github.com/owner/clock",
            "commit_sha": "b" * 40,
            "category": "clocks",
            "short_description": "A small clock for the bar",
            "description": "A small clock for the BUSY Bar that shows the time and the date.",
            "changelog": "Initial release",
            "package": {
                "key": f"packages/{digest}.tgz",
                "sha256": digest,
                "size": len(payload),
            },
            "sources": {
                "key": f"sources/{digest}.tar.gz",
                "sha256": digest,
                "size": len(payload),
            },
            "icon": asset,
            "screenshots": [asset],
            "toolchain": "0.2.1",
            "pr_number": 1,
            "head_sha": "c" * 40,
            "merge_sha": "d" * 40,
            "approved_by": "reviewer",
        }
        results = tmp_path / "results.json"
        results.write_text('{"status":"pass","checks":[]}')
        files = validate_publication(data, results, tmp_path)
        client = BackendTransport(f"http://127.0.0.1:{port}", "local-test-token")
        first = publish(client, data, files)
        assert publish(client, data, files) == first
        with urlopen(
            f"http://127.0.0.1:{port}/api/catalog/v1/applications/local-clock",
            timeout=5,
        ) as response:
            assert json.load(response)["alias"] == "local-clock"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        asyncio.run(cleanup())
        sys.path.remove(BACKEND)
