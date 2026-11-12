"""Hide a removed application in the catalog after its removal PR was merged and approved."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from tools.lib_app_id import is_valid
from tools.lib_http import HttpError, request
from tools.lib_publication_failure import (
    MAX_ERROR_BODY,
    failure_record,
    write_failure,
)

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
SHA = r"^[0-9a-f]{40}$"


class RemovalError(RuntimeError):
    """The catalog refused the removal; the status and answer go to the report."""

    def __init__(self, status: int | None, body: str) -> None:
        """Keep the backend's answer for the PR comment."""
        super().__init__(
            f"Catalog API returned HTTP {status}" + (f": {body}" if body else "")
        )
        self.status = status
        self.body = body


def removal_url(base_url: str, app_id: str, pr_number: int, approved_by: str) -> str:
    """The catalog's DELETE address with the PR and the approver for the audit log."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.query:
        raise ValueError("Invalid catalog API URL")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK:
        raise ValueError("Catalog API requires HTTPS")
    if not is_valid(app_id) or pr_number < 1:
        raise ValueError("Invalid application or pull request")
    query = f"pr_number={pr_number}&approved_by={quote(approved_by, safe='')}"
    return f"{base_url.rstrip('/')}/api/catalog/v1/applications/{quote(app_id, safe='')}?{query}"


def remove_application(
    base_url: str, token: str, app_id: str, pr_number: int, approved_by: str
) -> dict[str, object]:
    """
    Hide the application (never purge it) and report whether it was still there.

    A 404 means it is already gone, which is a success. A 5xx answer is retried, a lost
    connection is not: the request may have been applied. The token is never printed.
    """
    if not token:
        raise ValueError("Catalog CI token is required")
    url = removal_url(base_url, app_id, pr_number, approved_by)
    try:
        reply = request(
            "DELETE",
            url,
            headers={"Token": token},
            timeout=60,
            retry=True,
            retry_network=False,
            accept={404},
            error_body=MAX_ERROR_BODY,
        )
    except HttpError as error:
        raise RemovalError(
            error.status, error.body.decode(errors="replace").strip()
        ) from None
    return {"removed": True, "app_id": app_id, "already_gone": reply.status == 404}


def main() -> int:
    """Remove the application, or describe a dry run when no catalog API is configured."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--failure", type=Path, help="where to write why it failed")
    args = parser.parse_args()
    base_url = os.environ.get("CATALOG_API", "")
    if not base_url:
        print(json.dumps({"dry_run": True, "removed": False, "app_id": args.app_id}))
        return 0
    try:
        result = remove_application(
            base_url,
            os.environ.get("CATALOG_CI_TOKEN", ""),
            args.app_id,
            args.pr,
            args.approved_by,
        )
    except RemovalError as error:
        write_failure(args.failure, failure_record(error.status, error.body))
        raise
    except Exception:
        write_failure(args.failure, failure_record(None, ""))
        raise
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
