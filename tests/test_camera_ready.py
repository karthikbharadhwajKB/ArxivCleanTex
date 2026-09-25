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


@pytest.mark.parametrize(
    "tex, definitions, title",
    [
        # Self-Instruct (arXiv 2212.10560): a venue line and a macro in the title.
        (
            "\\newcommand{\\name}{\\textsc{Self-Instruct}}\n"
            "\\title{ \\vspace*{-0.5in} {{\\small \\hfill ACL 2023}\\\\ \\vspace*{.25in}}\n"
            "\\name{}: Aligning Language Models \\\\ with Self-Generated Instructions }",
            "",
            "ACL 2023 Self-Instruct: Aligning Language Models with Self-Generated Instructions",
        ),
        # Aya (arXiv 2402.07827): a logo in the title.
        (
            "\\title{\\includegraphics[scale=0.2]{./figures/logo2.png}Aya Model: An Instruction "
            "Finetuned \\\\Open-Access Multilingual Language Model}",
            "",
            "Aya Model: An Instruction Finetuned Open-Access Multilingual Language Model",
        ),
        # SimCSE (arXiv 2104.08821): the macro is defined in an \input file.
        (
            "\\title{\\ours: Simple Contrastive Learning of Sentence Embeddings}",
            "\\newcommand{\\ours}{SimCSE\\xspace}",
            "SimCSE: Simple Contrastive Learning of Sentence Embeddings",
        ),
        # FActScore (arXiv 2305.14251): only the active definition counts.
        (
            "\\title{\\ours: Fine-grained Atomic Evaluation}",
            "%\\newcommand{\\ours}{\\textsc{PreAF}}\n\\newcommand{\\ours}{\\textsc{FActScore}}",
            "FActScore: Fine-grained Atomic Evaluation",
        ),
        # The CVPR author kit.
        (
            "\\def\\confName{CVPR}\n\\title{\\LaTeX\\ Author Guidelines for \\confName~Proceedings}",
            "",
            "LaTeX Author Guidelines for CVPR Proceedings",
        ),
        (
            "\\title{Macros with arguments \\newterm{stay} out}",
            "\\newcommand{\\newterm}[1]{#1}",
            "Macros with arguments stay out",
        ),
    ],
)
def test_extract_title_from_real_papers(tex, definitions, title):
    assert extract_title(tex, definitions) == title


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

    # First pages as pdfplumber reads the real PDFs (ACL Anthology and arXiv).
    SELF_INSTRUCT_ANTHOLOGY = (
        "SELF-INSTRUCT: Aligning Language Models\nwith Self-Generated Instructions\n"
        "YizhongWang YeganehKordi SwaroopMishra AlisaLiu\nAbstract\nLarge instruction-tuned language models"
    )
    AYA_ANTHOLOGY = (
        "Aya Model: An Instruction Finetuned\nOpen-Access Multilingual Language Model\n"
        "AhmetÜstün ViraatAryabumi Zheng-XinYong\nAbstract\nRecent breakthroughs in large language models"
    )
    IMPOSSIBLE = (
        "Mission: Impossible Language Models\nJulieKallini1,IsabelPapadimitriou1,RichardFutrell2,\n"
        "1StanfordUniversity\nAbstract\nChomsky and others have very directly claimed"
    )

    def test_title_with_a_venue_line_matches_the_proceedings_pdf(self):
        title = "ACL 2023 Self-Instruct: Aligning Language Models with Self-Generated Instructions"
        assert same_paper(title, self.SELF_INSTRUCT_ANTHOLOGY).ok

    def test_different_papers_do_not_match(self):
        assert not same_paper(
            "An Embarrassingly Simple Approach for LLM with Strong ASR Capacity", self.IMPOSSIBLE
        ).ok
        assert not same_paper(
            "Aya Model: An Instruction Finetuned Open-Access Multilingual Language Model", self.IMPOSSIBLE
        ).ok

    def test_sibling_paper_with_shared_words_does_not_match(self):
        title = "Aya Dataset: An Open-Access Collection for Multilingual Instruction Tuning"
        assert not same_paper(title, self.AYA_ANTHOLOGY).ok

    def test_words_below_the_abstract_heading_do_not_count(self):
        page = (
            "A Different Title\nSomeAuthor\nAbstract\nWe study contrastive learning of sentence embeddings."
        )
        assert not same_paper("Simple Contrastive Learning of Sentence Embeddings", page).ok

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
