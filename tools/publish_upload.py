"""Publish an already approved build output through the catalog backend."""

import argparse
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from tools.check_registry import blocks
from tools.lib_http import HttpError, request
from tools.lib_publication_failure import (
    MAX_ERROR_BODY,
    failure_record,
    write_failure,
)
from tools.pipeline_report import read_results

CHUNK = 1024 * 1024
MAX_PRESIGN_FILES = 50
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
KIND = {".tgz": "packages", ".gz": "sources", ".png": "assets"}
SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "notify.schema.json"


class CatalogApiError(RuntimeError):
    """The catalog API answered with an error status."""

    def __init__(self, status: int, body: str) -> None:
        """Keep the status and the answer so callers can react and report the reason."""
        super().__init__(f"Catalog API returned HTTP {status}: {body}")
        self.status = status
        self.body = body


def api_failure(error: BaseException) -> dict[str, object]:
    """The backend's answer anywhere in the error's chain, or an empty record."""
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, CatalogApiError):
            return failure_record(cause.status, cause.body)
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return failure_record(None, "")


class PublicationTransport(Protocol):
    """Backend operations used by the publication use case."""

    def upload_files(self, files: list[tuple[Path, str]]) -> None:
        """
        Upload approved files under their content hashes.

        The storage verifies the hash before keeping the object.
        """
        ...

    def notify(self, data: dict) -> dict:
        """
        Publish approved metadata after all object uploads succeeded.

        The response contains identifiers and the published application URL.
        """
        ...


EXPECTED_PREFIX = {".tgz": "packages", ".tar.gz": "sources", ".png": "assets"}


def artifact_records(data: dict) -> list[dict]:
    """Every artifact record of the notice: package, sources, screenshots and icons."""
    records = [
        data["package"],
        data["sources"],
        *data["screenshots"],
        *data.get("back_screenshots", []),
    ]
    records.extend(
        data[name] for name in ("icon", "icon_back") if data.get(name) is not None
    )
    return records


def verified_file(item: dict, artifact_dir: Path) -> tuple[Path, str]:
    """
    The local file of one record, after checking its key, size and SHA-256.

    The file name is derived from the expected hash, so it cannot leave `artifact_dir`.
    """
    key, expected = item["key"], item["sha256"]
    prefix, separator, filename = key.partition("/")
    suffix = next(
        (ext for ext in EXPECTED_PREFIX if filename == expected + ext),
        None,
    )
    if not separator or suffix is None or prefix != EXPECTED_PREFIX[suffix]:
        raise ValueError("Artifact key does not match its SHA-256")
    path = artifact_dir / filename
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
        raise ValueError(f"Artifact missing or size mismatch: {filename}")
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"Artifact hash mismatch: {filename}")
    return path, filename


def validate_publication(
    data: dict, results: Path, artifact_dir: Path
) -> list[tuple[Path, str]]:
    """
    Check the approved result, metadata and every artifact before transmission.

    Filenames are derived from expected hashes and cannot escape the build output directory.
    """
    if any(blocks(item["id"], item["result"]) for item in read_results(results)):
        raise ValueError("Checks did not pass")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    if list(Draft202012Validator(schema).iter_errors(data)):
        raise ValueError("Publication does not match notify schema")
    files = []
    sizes: dict[str, int] = {}
    for item in artifact_records(data):
        key = item["key"]
        if key in sizes:
            if sizes[key] != item["size"]:
                raise ValueError("Conflicting artifact sizes")
            continue
        files.append(verified_file(item, artifact_dir))
        sizes[key] = item["size"]
    return files


def publish(
    transport: PublicationTransport, data: dict, files: list[tuple[Path, str]]
) -> dict:
    """
    Upload checked artifacts before notifying the catalog backend.

    Any failure prevents notify so incomplete publications cannot appear in the database.
    """
    transport.upload_files(files)
    return transport.notify(data)


def file_chunks(path: Path) -> Iterator[bytes]:
    """Stream a file in fixed chunks; it is opened again for every attempt."""
    with path.open("rb") as file:
        yield from iter(lambda: file.read(CHUNK), b"")


def check_upload_url(url: str) -> None:
    """A presigned URL is HTTPS, or plain HTTP to a loopback development bucket."""
    target = urlsplit(url)
    if target.scheme not in {"https", "http"} or not target.hostname:
        raise ValueError("Invalid presigned upload URL")
    if target.scheme == "http" and target.hostname not in LOOPBACK:
        raise ValueError("Presigned upload requires HTTPS")


def presign_batch(files: list[tuple[Path, str]]) -> bytes:
    """The upload-URL request for up to 50 files: type, hash and size of each."""
    return json.dumps(
        {
            "files": [
                {
                    "type": KIND[Path(filename).suffix],
                    "sha256": filename.split(".")[0],
                    "size": path.stat().st_size,
                }
                for path, filename in files
            ]
        }
    ).encode()


def check_plan_items(result: dict) -> list[dict]:
    """The per-file instructions of a presign answer; a malformed one is an error."""
    items = result.get("files")
    if not isinstance(items, list):
        raise TypeError("Invalid catalog API response")
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("status") not in {"exists", "upload"}
            or not isinstance(item.get("key"), str)
        ):
            raise TypeError("Invalid catalog API response")
    return items


class BackendTransport:
    """Stream uploads to a configured catalog API without following redirects."""

    def __init__(self, base_url: str, token: str) -> None:
        """
        Validate the backend URL and retain its token in memory only.

        Plain HTTP is allowed solely for loopback development endpoints.
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
        if parsed.scheme == "http" and parsed.hostname not in LOOPBACK:
            raise ValueError("Catalog API requires HTTPS")
        if not token:
            raise ValueError("Catalog CI token is required")
        self._url = parsed
        self._token = token

    def _post(self, endpoint: str, payload: bytes, retry_network: bool = True) -> dict:
        """
        POST a JSON body to a `ci/` endpoint, retrying 5xx (not 501) and, if allowed, network errors.

        Client errors and redirects are never retried or followed.
        """
        base = self._url.geturl().rstrip("/")
        try:
            reply = request(
                "POST",
                f"{base}/api/catalog/v1/ci/{endpoint}",
                headers={"Content-Type": "application/json", "Token": self._token},
                content=payload,
                timeout=60,
                retry=True,
                error_body=MAX_ERROR_BODY,
                retry_network=retry_network,
            )
        except HttpError as error:
            if error.status is None:
                raise RuntimeError("Catalog API is unreachable") from None
            raise CatalogApiError(
                error.status, error.body.decode(errors="replace").strip()
            ) from None
        result = json.loads(reply.content)
        if not isinstance(result, dict):
            raise TypeError("Invalid catalog API response")
        return result

    def upload_files(self, files: list[tuple[Path, str]]) -> None:
        """
        Upload straight to the bucket with presigned PUT requests.

        Any refusal of the catalog to issue upload URLs stops the publication.
        """
        try:
            plan = self._presign(files)
        except CatalogApiError as error:
            raise RuntimeError(
                f"The catalog did not issue upload URLs: {error}"
            ) from error
        for path, filename in files:
            item = plan[f"{KIND[Path(filename).suffix]}/{filename}"]
            if item["status"] == "upload":
                self._put(item["url"], item["headers"], path, filename)

    def _presign(self, files: list[tuple[Path, str]]) -> dict[str, dict]:
        """Ask the backend for upload instructions, at most 50 files per request."""
        plan: dict[str, dict] = {}
        for start in range(0, len(files), MAX_PRESIGN_FILES):
            payload = presign_batch(files[start : start + MAX_PRESIGN_FILES])
            for item in check_plan_items(self._post("uploads", payload)):
                plan[item["key"]] = item
        return plan

    def _put(
        self, url: str, headers: dict[str, str], path: Path, filename: str
    ) -> None:
        """
        Stream one file to its presigned URL with exactly the issued headers.

        412 means the object already exists; 5xx and network errors are retried.
        """
        check_upload_url(url)
        try:
            request(
                "PUT",
                url,
                headers={"Content-Length": str(path.stat().st_size), **headers},
                content=lambda: file_chunks(path),
                timeout=60,
                retry=True,
                accept={412},
                error_body=MAX_ERROR_BODY,
            )
        except HttpError as error:
            if error.status is None:
                raise RuntimeError(f"Storage is unreachable: {filename}") from None
            raise RuntimeError(
                f"Storage rejected the upload of {filename} (HTTP {error.status}): "
                + error.body.decode(errors="replace").strip()
            ) from None

    def notify(self, data: dict) -> dict:
        """
        Send validated metadata to the backend after uploads complete.

        Idempotency for repeat merge SHAs is enforced by the backend.
        """
        payload = json.dumps(data, separators=(",", ":")).encode()
        # Not idempotent: after a lost answer a repeat gets 409 for a publication
        # that went through, so only a 5xx answer is retried, never a lost connection.
        return self._post("notify", payload, retry_network=False)


def main() -> int:
    """
    Publish a validated approved build output or describe a dry run.

    The token is read only from the environment and never printed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--failure", type=Path, help="where to write why it failed")
    args = parser.parse_args()
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    files = validate_publication(data, args.results, args.artifacts)
    base_url = os.environ.get("CATALOG_API", "")
    if not base_url:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "files": [filename for _, filename in files],
                    "merge_sha": data["merge_sha"],
                }
            )
        )
        return 0
    try:
        result = publish(
            BackendTransport(base_url, os.environ.get("CATALOG_CI_TOKEN", "")),
            data,
            files,
        )
    except Exception as error:
        write_failure(args.failure, api_failure(error))
        raise
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
