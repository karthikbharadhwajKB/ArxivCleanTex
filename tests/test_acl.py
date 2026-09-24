import io
import json
import shutil
import subprocess

import pytest

from arxivcleantex import acl
from arxivcleantex.acl import (
    AclCheckFailedError,
    AclCheckUnavailableError,
    AclReport,
    Issue,
    InvalidPdfError,
    check_pdf,
)

needs_latex = pytest.mark.skipif(
    not (shutil.which("pdflatex") and shutil.which("kpsewhich")), reason="pdflatex not installed"
)


def latex_pdf(tmp_path, body, preamble=""):
    tex = tmp_path / "paper.tex"
    tex.write_text(
        f"\\documentclass{{article}}\n{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"
    )
    subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", tex.name], cwd=tmp_path, capture_output=True
    )
    return (tmp_path / "paper.pdf").read_bytes()


def image_pdf():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (595, 842), "white").save(buffer, "PDF")
    return buffer.getvalue()


# --- Grouping and review detection ------------------------------------------------


class TestGroup:
    def test_merges_repeats_and_splits_errors_from_warnings(self):
        logs = {
            "Error.MARGIN": [
                "Text on page 10 bleeds into the left margin.",
                "Text on page 2 bleeds into the left margin.",
                "Text on page 2 bleeds into the left margin.",
            ],
            "Error.FONT": ["Wrong font."],
            "Warn.BIB": ["Only 1 DOI."],
            "Error.PARSING": ["Error occurs when parsing page 3."],
        }
        errors, warnings = acl._group(logs)
        assert [(i.category, i.message, i.count) for i in errors] == [
            ("Margins", "Text on page 2 bleeds into the left margin.", 2),
            ("Margins", "Text on page 10 bleeds into the left margin.", 1),
            ("Fonts", "Wrong font.", 1),
        ]
        assert [(i.category, i.count) for i in warnings] == [("References", 1), ("Unreadable pages", 1)]

    def test_unknown_category(self):
        errors, _ = acl._group({"Error.NEWTHING": ["x"]})
        assert errors[0].category == "Newthing"

    def test_review_version_detection(self):
        lines = [f"Text on page {p} bleeds into the left margin." for p in range(1, 11)]
        assert acl._looks_like_review_version({"Error.MARGIN": lines * 20})
        assert not acl._looks_like_review_version({"Error.MARGIN": lines})
        assert not acl._looks_like_review_version({"Error.MARGIN": lines * 2 + [str(i) for i in range(90)]})
        assert not acl._looks_like_review_version({})

    def test_report_properties(self):
        report = AclReport(errors=[Issue("Margins", "a", 3), Issue("Fonts", "b")], warnings=[Issue("References", "c", 2)])
        assert (report.passed, report.error_count, report.warning_count) == (False, 4, 2)
        assert AclReport().passed


# --- check_pdf ----------------------------------------------------------------------


class TestCheckPdfValidation:
    def test_not_a_pdf(self):
        with pytest.raises(InvalidPdfError, match="not a PDF"):
            check_pdf(b"PK\x03\x04 a zip")

    def test_unknown_paper_type(self):
        with pytest.raises(InvalidPdfError, match="Unknown paper type"):
            check_pdf(b"%PDF-1.5", "novel")

    def test_aclpubcheck_missing(self, monkeypatch):
        monkeypatch.setattr(acl, "is_available", lambda: False)
        with pytest.raises(AclCheckUnavailableError):
            check_pdf(b"%PDF-1.5")


class FakeRun:
    """Stands in for the aclpubcheck subprocess, writing its output files."""

    def __init__(self, logs=None, images=(), returncode=0, stderr=""):
        self.logs, self.images, self.returncode, self.stderr = logs, images, returncode, stderr
        self.cmd = None

    def __call__(self, cmd, cwd, **kwargs):
        self.cmd = cmd
        if self.logs is not None:
            (cwd / "errors-paper.json").write_text(json.dumps(self.logs))
        for page in self.images:
            (cwd / f"errors-paper-page-{page}.png").write_bytes(f"png{page}".encode())
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


class TestCheckPdfRun:
    def test_passes_options_to_the_runner(self, monkeypatch):
        fake = FakeRun(logs={})
        monkeypatch.setattr(acl.subprocess, "run", fake)
        report = check_pdf(b"%PDF-1.5", "short", check_bottom=False, check_references=True, check_names=True)
        assert fake.cmd[-5:] == ["paper.pdf", "short", "0", "1", "1"]
        assert fake.cmd[1].endswith("acl_runner.py")
        assert report.passed and report.page_images == {}

    def test_reads_logs_and_images(self, monkeypatch):
        logs = {"Error.MARGIN": ["Text on page 3 bleeds into the right margin."] * 2}
        monkeypatch.setattr(acl.subprocess, "run", FakeRun(logs=logs, images=(10, 3)))
        report = check_pdf(b"%PDF-1.5")
        assert not report.passed and report.error_count == 2
        assert report.page_images == {3: b"png3", 10: b"png10"}
        assert list(report.page_images) == [3, 10]

    def test_crash(self, monkeypatch):
        monkeypatch.setattr(acl.subprocess, "run", FakeRun(returncode=1, stderr="Traceback\nKeyError: 'x'"))
        with pytest.raises(AclCheckFailedError, match="KeyError") as info:
            check_pdf(b"%PDF-1.5")
        assert "Traceback" in info.value.details

    def test_no_text_in_pdf(self, monkeypatch):
        stderr = "Traceback\nValueError: max() arg is an empty sequence"
        monkeypatch.setattr(acl.subprocess, "run", FakeRun(returncode=1, stderr=stderr))
        with pytest.raises(AclCheckFailedError, match="No text could be read"):
            check_pdf(b"%PDF-1.5")

    def test_timeout(self, monkeypatch):
        def slow(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(acl.subprocess, "run", slow)
        with pytest.raises(AclCheckFailedError, match="longer than"):
            check_pdf(b"%PDF-1.5", timeout=120)


# --- Real aclpubcheck -------------------------------------------------------------------


@needs_latex
class TestRealCheck:
    def test_plain_article_fails_font_and_page_limit(self, tmp_path):
        pdf = latex_pdf(tmp_path, "\\lipsum[1-60]\n\\section*{References}\nX", "\\usepackage{lipsum}")
        report = check_pdf(pdf, "short", check_references=False)
        categories = {i.category for i in report.errors}
        assert {"Fonts", "Page limit"} <= categories
        assert not report.likely_review_version

    def test_page_limit_depends_on_paper_type(self, tmp_path):
        pdf = latex_pdf(tmp_path, "\\lipsum[1-60]\n\\section*{References}\nX", "\\usepackage{lipsum}")
        short = {i.category for i in check_pdf(pdf, "short", check_references=False).errors}
        other = {i.category for i in check_pdf(pdf, "other", check_references=False).errors}
        assert "Page limit" in short and "Page limit" not in other

    def test_reference_warnings(self, tmp_path):
        pdf = latex_pdf(tmp_path, "Text\n\\section*{References}\nA reference.", "\\usepackage{times}")
        with_refs = check_pdf(pdf, "other", check_references=True)
        without = check_pdf(pdf, "other", check_references=False)
        assert any("DOI" in i.message for i in with_refs.warnings)
        assert without.warnings == []

    def test_image_only_pdf(self):
        with pytest.raises(AclCheckFailedError, match="No text could be read"):
            check_pdf(image_pdf(), "long")
