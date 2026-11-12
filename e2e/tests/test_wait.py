"""Offline checks for the E2E harness."""

from unittest.mock import Mock

import pytest

from e2e.infrastructure import wait_for


def test_wait_failure_names_evidence_and_duration():
    """
    Fail an expired poll with a precise awaited state and configured timeout.

    A fake clock removes real sleeps and networking from the regression test.
    """
    with pytest.raises(AssertionError, match="catalog-checks.*5 seconds"):
        wait_for(
            list,
            bool,
            "catalog-checks",
            5,
            clock=Mock(side_effect=[0, 5]),
            sleep=Mock(),
        )
