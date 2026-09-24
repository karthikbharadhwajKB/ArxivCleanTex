"""Links for reporting problems as GitHub issues.

The links open one of the issue forms in .github/ISSUE_TEMPLATE with fields
already filled in (GitHub fills a form field from the query parameter with the
field's id). Nothing is sent anywhere by the app: the user sees the issue
before submitting it, and their files are never attached automatically.
"""

import platform
from importlib import metadata
from urllib.parse import quote_plus, urlencode

REPO_URL = "https://github.com/karthikbharadhwajKB/PaperReady"
AUTHOR = "Karthik Bharadhwaj"
AUTHOR_URL = "https://github.com/karthikbharadhwajKB"

MODE_NAMES = {
    "arxiv": "Prepare for arXiv",
    "acl": "Check ACL format",
    "camera": "ACL camera-ready",
}

# Keeps the whole URL well below the ~8 KB browsers and GitHub accept.
MAX_ERROR_CHARS = 1500
MAX_ERROR_ENCODED = 4000


def _version(package):
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def environment():
    """Versions that help reproduce a problem."""
    return (
        f"arxiv_latex_cleaner {_version('arxiv_latex_cleaner')}, "
        f"aclpubcheck {_version('aclpubcheck')}, "
        f"Python {platform.python_version()}"
    )


def bug_report_url(mode=None, error=""):
    """A new bug report, filled in with the mode and the error message."""
    error = error.strip()
    if len(error) > MAX_ERROR_CHARS or len(quote_plus(error)) > MAX_ERROR_ENCODED:
        error = error[:MAX_ERROR_CHARS]
        while len(quote_plus(error)) > MAX_ERROR_ENCODED:
            error = error[: len(error) * 3 // 4]
        error += "\n… (shortened)"
    params = {"template": "bug_report.yml", "environment": environment()}
    if mode in MODE_NAMES:
        params["mode"] = MODE_NAMES[mode]
    if error:
        params["title"] = f"[Bug] {error.splitlines()[0][:80]}"
        params["error"] = error
    return f"{REPO_URL}/issues/new?{urlencode(params)}"


def feature_request_url():
    return f"{REPO_URL}/issues/new?{urlencode({'template': 'feature_request.yml'})}"
