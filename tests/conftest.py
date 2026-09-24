import os

import pytest

from helpers import write_tree

# In CI every test must run: a test skipped for a missing tool (BibTeX,
# pdflatex, Playwright/Chromium) would otherwise pass silently.
REQUIRE_ALL = os.environ.get("PAPERREADY_REQUIRE_ALL_TESTS") == "1"
_skipped = []


@pytest.fixture
def tree(tmp_path):
    """Writes {relative path: str | bytes} under a temp dir and returns it."""
    return lambda files: write_tree(tmp_path, files)


def pytest_runtest_logreport(report):
    if report.skipped:
        reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _skipped.append(f"{report.nodeid}: {reason}")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if REQUIRE_ALL and _skipped and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if REQUIRE_ALL and _skipped:
        terminalreporter.section("tests skipped but required", red=True)
        for line in _skipped:
            terminalreporter.write_line(line, red=True)
