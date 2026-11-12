"""Why a publication or removal failed after the merge, passed from its job to the report."""

import json
from pathlib import Path

MAX_DETAIL = 300
MAX_FILE = 4096
# How much of an error answer the jobs keep: room for a JSON detail of MAX_DETAIL.
MAX_ERROR_BODY = 1024


def normalized(status: object, detail: object) -> dict[str, object]:
    """The record's two fields with their types and the detail length enforced."""
    return {
        "status": status if type(status) is int else None,
        "detail": detail[:MAX_DETAIL] if isinstance(detail, str) else "",
    }


def failure_record(status: int | None, body: str) -> dict[str, object]:
    """
    The backend's status and its client-visible `detail`, if the answer had one.

    The backend answers errors as `{"detail": "..."}`; anything else (an HTML error
    page, a cut-off body, no answer at all) leaves the detail empty.
    """
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    return normalized(status, data.get("detail") if isinstance(data, dict) else None)


def write_failure(path: Path | None, record: dict[str, object]) -> None:
    """Write the record for the report job; without a path there is nothing to do."""
    if path is not None:
        path.write_text(json.dumps(record), encoding="utf-8")


def read_failure(path: Path) -> dict[str, object]:
    """
    Read a record written by `write_failure`; a missing or odd file is an empty record.

    The report must still say that the publication failed, with or without a reason.
    """
    empty: dict[str, object] = {"status": None, "detail": ""}
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE:
            return empty
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return normalized(data.get("status"), data.get("detail"))
