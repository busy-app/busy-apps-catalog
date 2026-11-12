"""Pytest representation for E2E configuration, lifecycle and task reporting."""

from pathlib import Path

import pytest

from e2e.application import Run
from e2e.infrastructure import HarnessNetworkError
from e2e.report import NETWORK, UNFINISHED, render


def pytest_addoption(parser):
    """
    Expose the same environment, retention and report options locally and in Actions.

    Environment-specific tokens remain outside command-line arguments.
    """
    parser.addoption("--env", default="dev", choices=["dev", "stage"])
    parser.addoption("--keep", action="store_true")
    parser.addoption("--report", default="e2e-report.md")


def pytest_configure(config):
    """
    Initialize a per-session result ledger and register the mandatory task marker.

    Unit tests for the harness do not require an E2E task marker.
    """
    config.addinivalue_line("markers", "task(id): task covered by an E2E scenario")
    config._catalog_results = []


def pytest_collection_modifyitems(items):
    """
    Refuse a scenario collection that would silently omit ownership in the report.

    The failure appears before any remote resource is created.
    """
    for item in items:
        if "/scenarios/" in str(item.path) and item.get_closest_marker("task") is None:
            raise pytest.UsageError(f"Missing task marker: {item.nodeid}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Record completed scenarios or setup failures once with their owning task.

    Skips count as failures because incomplete MVP features must remain visible.
    """
    result = yield
    report = result.get_result()
    marker = item.get_closest_marker("task")
    if marker and (report.when == "call" or report.when == "setup" and report.failed):
        network = (
            report.failed
            and call.excinfo is not None
            and issubclass(call.excinfo.type, HarnessNetworkError)
        )
        cause = NETWORK if network else UNFINISHED
        for task in marker.args:
            item.config._catalog_results.append(
                (task, item.name, report.outcome, cause)
            )


def pytest_sessionfinish(session):
    """
    Persist a task table even when scenarios fail on missing infrastructure.

    The report reflects real observed results from this exact execution.
    """
    if session.config._catalog_results:
        text = render(session.config._catalog_results)
        execution = getattr(session.config, "_catalog_execution", None)
        if execution:
            text += f"\nRun: `{execution.id}`\n\nRepository: `{execution.repo}`\n\nAPI: `{execution.api}`\n\n"
            text += "\n".join(f"- {url}" for url in execution.evidence) + "\n"
        Path(session.config.getoption("--report")).write_text(text)


@pytest.fixture(scope="session")
def run(request):
    """
    Allocate one isolated run and enforce cleanup after scenario completion.

    Retention is explicit and cleanup errors fail the session instead of disappearing.
    """
    execution = Run(
        request.config.getoption("--env"), request.config.getoption("--keep")
    )
    request.config._catalog_execution = execution
    yield execution
    errors = execution.cleanup()
    request.config._catalog_results.append(
        (
            "10.28-13",
            "cleanup",
            "failed" if errors else "passed",
            NETWORK if execution.cleanup_network else UNFINISHED,
        )
    )
    assert not errors, "E2E cleanup failed: " + "; ".join(errors)
