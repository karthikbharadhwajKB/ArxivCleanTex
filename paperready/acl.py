"""Checking a paper's PDF against ACL formatting rules with aclpubcheck.

aclpubcheck (https://github.com/acl-org/aclpubcheck) is what ACL publication
chairs run on camera-ready papers. It runs in a separate process so a crash or
a very slow PDF cannot take the app down.
"""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .core import CleanerError

# Page limits aclpubcheck enforces for the main text (references, limitations,
# ethics and acknowledgments may follow).
PAPER_TYPES = {
    "long": "Long paper (9 pages)",
    "short": "Short paper (5 pages)",
    "demo": "Demo paper (7 pages)",
    "other": "Other (no page limit)",
}

# aclpubcheck's log keys, as written to its JSON file.
CATEGORIES = {
    "Error.SIZE": "Page size",
    "Error.MARGIN": "Margins",
    "Error.PAGELIMIT": "Page limit",
    "Error.FONT": "Fonts",
    "Error.SPELLING": "Spelling",
    "Error.PARSING": "Unreadable pages",
    "Warn.BIB": "References",
}
WARNING_KEYS = {"Warn.BIB", "Error.PARSING"}

# A review-version PDF has line numbers in both margins, which aclpubcheck
# reports once per line: hundreds of margin errors, each repeated many times.
_REVIEW_MIN_ERRORS = 100
_REVIEW_MIN_REPEATS = 8

_PAGE = re.compile(r"page (\d+)")
# aclpubcheck's crash on PDFs without text (e.g. scans), as worded by Python 3.10
# ("max() arg is an empty sequence") and 3.12+ ("max() iterable argument is empty").
_NO_TEXT = re.compile(r"max\(\) (?:arg is an empty sequence|iterable argument is empty)")
# The author line of anonymous ACL-style submissions, e.g. "Anonymous ACL submission".
# PDF text extraction often drops the spaces ("AnonymousACLsubmission").
_ANONYMOUS = re.compile(r"\bAnonymous\s*(?:[A-Z0-9][A-Za-z0-9-]*\s*){0,3}submission\b")


class InvalidPdfError(CleanerError):
    pass


class AclCheckUnavailableError(CleanerError):
    pass


class AclCheckFailedError(CleanerError):
    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details


@dataclass
class Issue:
    category: str
    message: str
    count: int = 1


@dataclass
class AclReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # {page number: annotated PNG} for pages with margin problems.
    page_images: dict = field(default_factory=dict)
    likely_review_version: bool = False

    @property
    def passed(self):
        return not self.errors

    @property
    def error_count(self):
        return sum(issue.count for issue in self.errors)

    @property
    def warning_count(self):
        return sum(issue.count for issue in self.warnings)


def is_available():
    return importlib.util.find_spec("aclpubcheck") is not None


def _group(logs):
    """Turns aclpubcheck's logs into (errors, warnings) of Issues, merging
    repeated messages and keeping the order they were found in."""
    errors, warnings = [], []
    for key, messages in logs.items():
        category = CATEGORIES.get(key, key.split(".")[-1].title())
        issues = [Issue(category, message, count) for message, count in Counter(messages).items()]
        issues.sort(key=lambda i: int(m.group(1)) if (m := _PAGE.search(i.message)) else 0)
        (warnings if key in WARNING_KEYS else errors).extend(issues)
    return errors, warnings


def _looks_like_review_version(logs):
    margin = logs.get("Error.MARGIN", [])
    return len(margin) >= _REVIEW_MIN_ERRORS and len(margin) >= _REVIEW_MIN_REPEATS * len(set(margin))


def _anonymous_first_page(pdf_path):
    """True if page 1 has an anonymous-submission author line."""
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        return False
    return bool(_ANONYMOUS.search(text))


def check_pdf(
    pdf_bytes, paper_type="long", check_bottom=True, check_references=True, check_names=False, timeout=300
):
    """Runs aclpubcheck on a PDF and returns an AclReport."""
    if paper_type not in PAPER_TYPES:
        raise InvalidPdfError(f"Unknown paper type “{paper_type}”.")
    if not pdf_bytes.lstrip()[:5] == b"%PDF-":
        raise InvalidPdfError("The uploaded file is not a PDF.")
    if not is_available():
        raise AclCheckUnavailableError("aclpubcheck is not installed on this server.")

    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        (work / "paper.pdf").write_bytes(pdf_bytes)
        flags = [str(int(bool(v))) for v in (check_bottom, check_references, check_names)]
        runner = Path(__file__).with_name("acl_runner.py")
        cmd = [sys.executable, str(runner), "paper.pdf", paper_type, *flags]
        try:
            run = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AclCheckFailedError(
                f"The check took longer than {timeout // 60} minutes and was stopped."
            ) from None
        log_file = work / "errors-paper.json"
        if run.returncode != 0 or not log_file.is_file():
            details = (run.stderr or run.stdout or "").strip()
            last = details.splitlines()[-1] if details else "unknown error"
            if _NO_TEXT.search(last):
                raise AclCheckFailedError(
                    "No text could be read from this PDF. Is it scanned or made of "
                    "images? Upload the PDF produced by LaTeX.",
                    details,
                )
            raise AclCheckFailedError(f"aclpubcheck could not check this PDF ({last}).", details)

        logs = json.loads(log_file.read_text())
        errors, warnings = _group(logs)
        anonymous = _anonymous_first_page(work / "paper.pdf")
        images = {int(p.stem.rsplit("-", 1)[1]): p.read_bytes() for p in work.glob("errors-paper-page-*.png")}
    return AclReport(
        errors=errors,
        warnings=warnings,
        page_images=dict(sorted(images.items())),
        likely_review_version=anonymous or _looks_like_review_version(logs),
    )
