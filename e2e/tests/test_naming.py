"""Offline checks for the E2E harness."""

from e2e.domain import app_id


def test_longest_identity_stays_valid():
    """
    Bound every fixture ID while preserving a distinct hashed run suffix.

    Long names and runs must not exceed the 63-character application ID limit.
    """
    import re

    identifier = app_id("no-api-version-with-a-long-name", "x" * 500)
    assert len(identifier) <= 63 and re.fullmatch(
        r"[a-z0-9_-]+(\.[a-z0-9_-]+){2,}", identifier
    )
    assert identifier != app_id("no-api-version-with-a-long-name", "y" * 500)
