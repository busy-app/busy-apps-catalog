"""Offline checks for the E2E harness."""

import json

from e2e.domain import rewrite_manifest


def test_rewrite_preserves_negative_fixture():
    """
    Rewrite only identity and version without adding missing API declarations.

    The serialized app manifest must remain valid and below the device size limit.
    """
    source = {"id": "old", "version": "1.0.0", "name": "Clock", "format_version": 1}
    result = rewrite_manifest(json.dumps(source).encode(), "e2e.valid", "2.0.0")
    assert len(result) <= 512
    assert json.loads(result) == {**source, "id": "e2e.valid", "version": "2.0.0"}
