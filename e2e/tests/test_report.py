"""Offline checks for the E2E harness."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from e2e.report import render


def test_task_report_requires_ownership():
    """
    Count failures by task and refuse results with no task attribution.

    Missing features cannot disappear from the final readiness table.
    """
    result = render(
        [("10.26-26", "valid", "passed"), ("10.26-26", "invalid", "failed")]
    )
    assert "| 10.26-26 | 1 | 1 |" in result
    with pytest.raises(ValueError, match="Missing task"):
        render([("", "unowned", "passed")])


def test_scenarios_without_task_fail_collection():
    """
    Reject an unmarked scenario instead of dropping it from the task report.

    Collection happens before the session allocates any remote fixtures.
    """
    from e2e.conftest import pytest_collection_modifyitems

    item = Mock()
    item.path = Path("/e2e/scenarios/test_missing.py")
    item.nodeid = "missing"
    item.get_closest_marker.return_value = None
    with pytest.raises(pytest.UsageError, match="Missing task"):
        pytest_collection_modifyitems([item])


def test_causes_count_each_failed_scenario_once() -> None:
    """A scenario with two task markers is one failure; the cause comes from the fourth item."""
    text = render(
        [
            ("10.28-14", "nine", "failed", "network / harness"),
            ("10.26-27", "nine", "failed", "network / harness"),
            ("10.28-13", "cleanup", "failed", "not implemented"),
            ("10.26-26", "four", "passed", "not implemented"),
        ]
    )
    assert "| network / harness | 1 |" in text
    assert "| not implemented | 1 |" in text
