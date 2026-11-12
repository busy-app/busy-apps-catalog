"""Tests for the failure record passed from the publish job to the report."""

import json
from pathlib import Path

import pytest

from tools.lib_publication_failure import failure_record, read_failure, write_failure


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            409,
            '{"detail": "Application belongs to another repository"}',
            {"status": 409, "detail": "Application belongs to another repository"},
        ),
        (502, "<html>Bad gateway</html>", {"status": 502, "detail": ""}),
        (409, '{"detail": "cut off', {"status": 409, "detail": ""}),
        (422, '{"detail": [{"msg": "list"}]}', {"status": 422, "detail": ""}),
        (None, "", {"status": None, "detail": ""}),
    ],
)
def test_record_keeps_only_a_text_detail(status, body, expected) -> None:
    """Only the backend's `{"detail": "..."}` text becomes the reason."""
    assert failure_record(status, body) == expected


def test_detail_is_bounded(tmp_path: Path) -> None:
    """A long detail is cut on both sides of the file."""
    record = failure_record(409, json.dumps({"detail": "x" * 1000}))
    assert len(record["detail"]) == 300
    path = tmp_path / "failure.json"
    path.write_text(json.dumps({"status": 409, "detail": "y" * 1000}))
    assert len(read_failure(path)["detail"]) == 300


def test_round_trip_and_odd_files(tmp_path: Path) -> None:
    """What the job writes is read back; a missing, broken or linked file is empty."""
    path = tmp_path / "failure.json"
    write_failure(path, {"status": 409, "detail": "No"})
    assert read_failure(path) == {"status": 409, "detail": "No"}
    write_failure(None, {"status": 409, "detail": "ignored"})
    empty = {"status": None, "detail": ""}
    assert read_failure(tmp_path / "missing.json") == empty
    (tmp_path / "broken.json").write_text("{")
    assert read_failure(tmp_path / "broken.json") == empty
    (tmp_path / "list.json").write_text("[1]")
    assert read_failure(tmp_path / "list.json") == empty
    (tmp_path / "link.json").symlink_to(path)
    assert read_failure(tmp_path / "link.json") == empty
    (tmp_path / "typed.json").write_text('{"status": "409", "detail": 5}')
    assert read_failure(tmp_path / "typed.json") == empty
