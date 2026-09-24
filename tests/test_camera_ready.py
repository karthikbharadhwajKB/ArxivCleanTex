import pytest

from helpers import doc, make_zip
from paperready import acl
from paperready.acl import AclReport
from paperready.arxiv import CleanResult, clean_zip, extract_title
from paperready.camera_ready import _compact, cross_checks, same_paper, same_version


@pytest.mark.parametrize(
    "tex, title",
    [
        (
            "\\title{Pruning for Efficiency: Fairness in\\\\ Pruned Speech-LLMs}",
            "Pruning for Efficiency: Fairness in Pruned Speech-LLMs",
        ),
        ("% \\title{Old}\n\\title[Short]{A \\textbf{Bold} Idea\\thanks{Funded by X.}}", "A Bold Idea"),
        ("\\title{The {BERT}~Model}", "The BERT Model"),
        ("\\title{Broken {nesting", "Broken nesting"),
        ("no title here", ""),
    ],
)
def test_extract_title(tex, title):
    assert extract_title(tex) == title


def test_clean_result_has_title():
    result = clean_zip(make_zip({"paper/main.tex": doc("\\title{My Paper}\\maketitle")}))
    assert result.title == "My Paper"


def test_compact():
    assert _compact("Demo-\ngraphic Dis parities!") == "demographicdisparities"


class TestSameVersion:
    def test_both_final(self):
        assert same_version("", False).ok

    def test_source_review(self):
        check = same_version("it uses the ACL review option", False)
        assert not check.ok and "source is still the review" in check.title
        assert "ACL review option" in check.detail

    def test_pdf_review(self):
        check = same_version("", True)
        assert not check.ok and check.title == "The PDF is a review version"

    def test_both_review(self):
        assert same_version("x", True).title == "Both are review versions"


class TestSamePaper:
    def test_match_despite_lost_spaces_and_hyphenation(self):
        pdf = "PruningforEfficiency,PayinginFairness:Demo-\ngraphicDisparities\nAnonymousACLsubmission"
        assert same_paper("Pruning for Efficiency, Paying in Fairness: Demographic Disparities", pdf).ok

    def test_mismatch(self):
        check = same_paper("Great Results", "Another Paper Entirely")
        assert not check.ok and "“Great Results”" in check.detail

    def test_no_title(self):
        check = same_paper("", "anything")
        assert check.ok and "not checked" in check.title and check.detail


def test_cross_checks():
    result = CleanResult(zip_bytes=b"", main_file="main.tex", project_root=".", title="T")
    checks = cross_checks(result, AclReport(likely_review_version=True), "T")
    assert [c.ok for c in checks] == [False, True]


def test_first_page_text_of_bytes_and_bad_input(tmp_path):
    assert acl.first_page_text(b"not a pdf") == ""
    assert acl.first_page_text(tmp_path / "missing.pdf") == ""
