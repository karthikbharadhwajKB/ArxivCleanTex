import time

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


@pytest.mark.parametrize(
    "tex, title",
    [
        # Only the last argument of these commands is printed.
        (
            "\\newcommand{\\sys}{\\textcolor{orange}{\\textbf{Sys}}}\\title{\\sys: Fast Inference}",
            "Sys: Fast Inference",
        ),
        (
            "\\newcommand{\\sys}{\\href{https://github.com/x/sys}{Sys}}\\title{\\sys: Fast Inference}",
            "Sys: Fast Inference",
        ),
        ("\\title{\\resizebox{2cm}{!}{Logo} \\fontsize{20}{24}\\selectfont Big Title}", "Logo Big Title"),
        # A macro whose body is a line break.
        ("\\newcommand{\\nl}{\\\\}\\title{Fast\\nl Inference}\\begin{document}Body", "Fast Inference"),
        # A word after a line break that is also a macro name stays a word.
        (
            "\\newcommand{\\model}{FooNet}\\title{Scaling Laws for\\\\model Merging}",
            "Scaling Laws for model Merging",
        ),
        # Math letters, sub- and superscripts, and escaped characters.
        ("\\title{Scaling $\\mu$P Transfer}", "Scaling μP Transfer"),
        ("\\title{Robust $\\ell_1$ Regression}", "Robust ℓ1 Regression"),
        (
            "\\title{The $\\beta$-Mixture Prior with $\\Gamma^2$ and $\\varepsilon$}",
            "The β-Mixture Prior with Γ2 and ε",
        ),
        ("\\title{Q\\&A for \\texttt{self\\_instruct}}", "Q&A for self_instruct"),
    ],
)
def test_extract_title_prints_what_latex_prints(tex, title):
    assert extract_title(tex) == title


@pytest.mark.parametrize(
    "main, macros",
    [
        # The \input file redefines the main file's macro …
        ("\\newcommand{\\x}{Old}\\input{macros}\\title{\\x Paper}", "\\renewcommand{\\x}{New}"),
        # … or the main file redefines the \input file's macro.
        ("\\input{macros}\\renewcommand{\\x}{New}\\title{\\x Paper}", "\\newcommand{\\x}{Old}"),
    ],
)
def test_the_last_definition_latex_reads_wins(main, macros):
    tex = f"\\documentclass{{article}}{main}\\begin{{document}}\\maketitle\\end{{document}}"
    assert clean_zip(make_zip({"main.tex": tex, "macros.tex": macros})).title == "New Paper"


def test_greek_letter_names_that_unicode_spells_differently():
    assert (
        extract_title("\\title{Fast $\\lambda$-Calculus and $\\Lambda$ Terms}")
        == "Fast λ-Calculus and Λ Terms"
    )


def test_self_referencing_macro_stays_cheap():
    # TeX itself would loop forever; the app must not run out of memory.
    tex = "\\def\\x{" + "\\x" * 1000 + "}\\title{\\x}"
    started = time.monotonic()
    title = extract_title(tex)
    assert time.monotonic() - started < 2 and len(title) < 50_000


def test_unbalanced_macros_stay_cheap():
    tex = "\\def\\a{" * 20_000 + "\\title{" + "\\vspace{" * 20_000
    started = time.monotonic()
    extract_title(tex)
    assert time.monotonic() - started < 5


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

    @pytest.mark.parametrize(
        "title, page",
        [
            # Short titles whose words all occur on another paper's first page.
            (
                "Visual Instruction Tuning",
                "Instruction Tuning for Visual Question Answering\nA. Author\nAbstract\n",
            ),
            ("Learning to Reason", "Planning with Language Models\nReasonable Learning Group\nAbstract\n"),
            # "Abstract" in the title must not let the abstract body count.
            (
                "Abstractive Summarization with Pretrained Transformers",
                "Faithful Summarization\nA. Author\nAbstract\nPretrained transformers with abstractive summarization",
            ),
            # Another paper whose own title starts with "Abstract…".
            (
                "Summarization with Pretrained Transformers for Long Documents",
                "Abstractive Methods\nA. Author\nAbstract\nSummarization with pretrained transformers for long documents",
            ),
            # The same words in a different order.
            (
                "Language Models for Instruction Tuning",
                "Tuning Instruction for Models Language\nA. Author\nAbstract\n",
            ),
        ],
    )
    def test_wrong_pdf_is_not_accepted(self, title, page):
        assert not same_paper(title, page).ok

    @pytest.mark.parametrize(
        "source, pdf",
        [
            ("Scaling μP Transfer", "Scaling µP Transfer\nA. Author\nAbstract\n"),  # micro sign in the PDF
            ("Robust ℓ1 Regression", "Robust ℓ1 Regression\nA. Author\nAbstract\n"),
            ("The β-Mixture Prior", "The β-Mixture Prior\nA. Author\nAbstract\n"),
            ("Scaling P Transfer", "Scaling μP Transfer\nA. Author\nAbstract\n"),  # source lost the symbol
        ],
    )
    def test_math_letters_in_short_titles_match(self, source, pdf):
        assert same_paper(source, pdf).ok

    @pytest.mark.parametrize(
        "banner",
        ["Extended Abstract\n", "Published at the Workshop on Efficient Systems 2024\nExtended Abstract\n"],
    )
    def test_extended_abstract_banner_above_the_title(self, banner):
        page = banner + "Scaling Laws for Sparse\nMixture of Experts\nA. Author\nAbstract\nWe study"
        assert same_paper("Scaling Laws for Sparse Mixture of Experts", page).ok

    def test_double_struck_and_bold_math_letters_match(self):
        # The PDF shows ℝ and 𝐀, which Unicode decomposes to capital R and A.
        assert same_paper("Q&A over Rn Data with A", "Q&A over ℝn Data with 𝐀\nA. Author\nAbstract\n").ok

    def test_mixed_script_title_needs_its_own_script(self):
        # Only "BERT" is ASCII: another Russian paper that mentions BERT is no match.
        page = "Другая статья о BERT\nА. Автор\nАннотация\n"
        assert not same_paper("Модели BERT для русского языка", page).ok
        assert same_paper("Модели BERT для русского языка", "Модели BERT для русского языка\nА. Автор\n").ok

    def test_accents_and_ligatures_match(self):
        # The source spells Schr\"{o}dinger and "Efficient"; the PDF has "ö" and the "ﬃ" ligature.
        assert same_paper(
            "Schrodinger Bridges for Eﬃcient Sampling", "Schrödinger Bridges for Eﬃcient Sampling"
        ).ok
        assert same_paper(
            "Schrodinger Bridges for Efficient Sampling", "Schrödinger Bridges for Eﬃcient Sampling"
        ).ok

    def test_titles_in_other_scripts_are_compared(self):
        assert same_paper("基于大模型的语音识别", "基于大模型的语音识别\n作者").ok
        assert not same_paper("基于大模型的语音识别", "一个完全不同的标题\n作者").ok

    def test_merged_column_line_ends_the_header(self):
        # pdfplumber merges the "Abstract" heading with the other column's line.
        page = (
            "A Different Title\nSomeAuthor\n001 Abstract Simple contrastive learning of sentence embeddings"
        )
        assert not same_paper("Simple Contrastive Learning of Sentence Embeddings", page).ok

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
