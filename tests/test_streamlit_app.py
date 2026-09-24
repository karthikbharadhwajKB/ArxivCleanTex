import pytest
import streamlit
from streamlit.testing.v1 import AppTest

from paperready import arxiv as cleaner
from helpers import doc, make_zip, unzip

APP = "streamlit_app.py"


class FakeUpload:
    def __init__(self, data, name="paper.zip"):
        self._data = data
        self.name = name

    def getvalue(self):
        return self._data


@pytest.fixture
def upload(monkeypatch):
    """Makes st.file_uploader return the given zip (AppTest cannot upload files)."""

    def set_upload(data, name="paper.zip", config=None):
        def file_uploader(label, *args, **kwargs):
            if label.startswith("cleaner_config"):
                return FakeUpload(config, "cleaner_config.yaml") if config else None
            return FakeUpload(data, name)

        monkeypatch.setattr(streamlit, "file_uploader", file_uploader)

    return set_upload


@pytest.fixture
def spy_clean_zip(monkeypatch):
    """Records the arguments the app passes to clean_zip."""
    calls = []
    real = cleaner.clean_zip

    def spy(zip_bytes, extra_args=None, main_hint=None, config_bytes=None, make_bbl=True):
        calls.append(
            {"extra_args": extra_args, "main_hint": main_hint, "config": config_bytes, "make_bbl": make_bbl}
        )
        return real(zip_bytes, extra_args, main_hint, config_bytes, make_bbl)

    monkeypatch.setattr(cleaner, "clean_zip", spy)
    return calls


def run_app():
    return AppTest.from_file(APP, default_timeout=60).run()


def button(at, label):
    return next(b for b in at.button if b.label == label)


def labels(elements):
    return [e.label for e in elements]


def click_clean(at):
    button(at, "Clean my paper").click().run()
    assert not at.exception
    return at


TEXT_INPUTS = [
    "Main .tex file or folder (optional)",
    "Commands to delete (e.g. todo note, or \\todo \\note)",
    "Commands to unwrap, keeping their text (e.g. hl)",
    "Environments to delete (e.g. note)",
    "\\if commands that are not conditionals (e.g. ifdraft)",
    "Folder with externalized TikZ PDFs (optional)",
    "Inkscape output folder",
]
CHECKBOXES = [
    "Resize images to reduce size",
    "Convert PNG images to JPG",
    "Compress PDF figures (Ghostscript)",
    "Generate a missing .bbl with BibTeX",
    "Keep .bib files",
    "Use Inkscape-exported SVGs (\\includesvg)",
]


def widget(elements, label):
    return next(e for e in elements if e.label == label)


def test_initial_page():
    at = run_app()
    assert not at.exception
    assert at.title[0].value == "PaperReady"
    assert [t.label for t in at.text_input] == TEXT_INPUTS
    assert [c.label for c in at.checkbox] == CHECKBOXES
    assert labels(at.button) == ["✓ Selected", "Choose"]  # only the mode cards until a zip is uploaded
    assert "Drop your project's .zip" in at.info[0].value


def test_upstream_defaults():
    at = run_app()
    assert widget(at.number_input, "Max image size (pixels, longest side)").value == 500
    assert widget(at.number_input, "PDF image resolution (dpi)").value == 500
    assert widget(at.number_input, "Only convert PNGs larger than (MB)").value == 0.5
    assert at.slider[0].value == 50
    assert [c.label for c in at.checkbox if c.value] == ["Generate a missing .bbl with BibTeX"]


@pytest.mark.parametrize(
    "checkbox, dependent",
    [
        ("Resize images to reduce size", ["Max image size (pixels, longest side)"]),
        ("Convert PNG images to JPG", ["Only convert PNGs larger than (MB)", "slider"]),
        ("Compress PDF figures (Ghostscript)", ["PDF image resolution (dpi)"]),
        ("Use Inkscape-exported SVGs (\\includesvg)", ["Inkscape output folder"]),
    ],
)
def test_dependent_inputs_enable_with_their_checkbox(checkbox, dependent):
    def elements(at):
        found = []
        for label in dependent:
            if label == "slider":
                found.append(at.slider[0])
            else:
                found += [e for e in [*at.number_input, *at.text_input] if e.label == label]
        return found

    at = run_app()
    assert all(e.disabled for e in elements(at))
    widget(at.checkbox, checkbox).check().run()
    assert not any(e.disabled for e in elements(at))


def test_successful_clean(upload):
    upload(make_zip({"paper/main.tex": doc("Hi % secret")}))
    at = click_clean(run_app())
    assert "paper/main.tex" in at.success[0].value
    assert not at.warning and not at.error
    # The download button is rendered with the cleaned zip.
    assert at.get("download_button")


def test_every_option_is_passed_to_the_cleaner(upload, spy_clean_zip, monkeypatch):
    monkeypatch.setattr(cleaner.shutil, "which", lambda name: None)  # no Ghostscript
    config = b"commands_to_delete: [draft]\n"
    upload(make_zip({"p1/main.tex": doc(), "p2/main.tex": doc()}), config=config)
    at = run_app()
    at.text_input[0].input("p2")
    for label in CHECKBOXES:
        widget(at.checkbox, label).check()
    widget(at.checkbox, "Generate a missing .bbl with BibTeX").uncheck()
    at.run()
    widget(at.number_input, "Max image size (pixels, longest side)").set_value(800)
    widget(at.number_input, "PDF image resolution (dpi)").set_value(300)
    widget(at.number_input, "Only convert PNGs larger than (MB)").set_value(1.5)
    at.slider[0].set_value(70)
    at.text_area[0].input('{"figs/a.png": 2000}')
    widget(at.text_input, TEXT_INPUTS[1]).input("\\todo, note bad*")
    widget(at.text_input, TEXT_INPUTS[2]).input("hl")
    widget(at.text_input, TEXT_INPUTS[3]).input("comment comment2")
    widget(at.text_input, TEXT_INPUTS[4]).input("ifdraft")
    widget(at.text_input, TEXT_INPUTS[5]).input("tikz")
    widget(at.text_input, TEXT_INPUTS[6]).input("svgs")
    button(at, "Clean my paper").click().run()
    assert "Ghostscript" in at.error[0].value
    assert spy_clean_zip[-1]["extra_args"][4:8] == [
        "--compress_pdf", "--pdf_im_resolution", "300", "--convert_png_to_jpg"
    ]

    widget(at.checkbox, "Compress PDF figures (Ghostscript)").uncheck()
    button(at, "Clean my paper").click().run()
    assert not at.exception
    assert spy_clean_zip[-1] == {
        "extra_args": [
            "--keep_bib",
            "--resize_images", "--im_size", "800",
            "--convert_png_to_jpg", "--png_quality", "70", "--png_size_threshold", "1.5",
            "--images_allowlist", '{"figs/a.png": 2000}',
            "--commands_to_delete", "todo", "note",
            "--commands_only_to_delete", "hl",
            "--environments_to_delete", "comment",
            "--if_exceptions", "ifdraft",
            "--use_external_tikz", "tikz",
            "--svg_inkscape", "svgs",
        ],
        "main_hint": "p2",
        "config": config,
        "make_bbl": False,
    }
    assert any("bad*" in w.value for w in at.warning)
    assert any("comment2" in w.value for w in at.warning)


def test_invalid_option_shows_error(upload, spy_clean_zip):
    upload(make_zip({"main.tex": doc()}))
    at = run_app()
    at.text_area[0].input("{not json")
    button(at, "Clean my paper").click().run()
    assert "not valid JSON" in at.error[0].value
    assert spy_clean_zip == []


def test_friendly_error(upload):
    upload(make_zip({"notes.txt": "x"}))
    at = click_clean(run_app())
    assert "No .tex files" in at.error[0].value
    assert not at.success


def test_multiline_error_keeps_line_breaks(upload):
    upload(make_zip({"p1/main.tex": doc(), "p2/main.tex": doc()}))
    at = click_clean(run_app())
    assert "  \n" in at.error[0].value


def test_cleaner_failure_shows_details(upload, monkeypatch):
    def fail(*args, **kwargs):
        raise cleaner.CleaningFailedError("could not process (ValueError: boom)", "Traceback...")

    monkeypatch.setattr(cleaner, "clean_zip", fail)
    upload(make_zip({"main.tex": doc()}))
    at = click_clean(run_app())
    assert "ValueError: boom" in at.error[0].value
    assert at.expander[-1].label == "Technical details"
    assert at.code[0].value == "Traceback..."


def test_unexpected_error(upload, monkeypatch):
    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cleaner, "clean_zip", crash)
    upload(make_zip({"main.tex": doc()}))
    at = click_clean(run_app())
    assert at.error[0].value == "Something went wrong while cleaning: disk full"


def test_warnings_and_missing_files(upload):
    upload(
        make_zip(
            {
                "main.tex": doc("\\includegraphics{figs/gone}\\bibliography{refs}"),
                "refs.bib": "@a{}",
            }
        )
    )
    at = click_clean(run_app())
    text = " ".join(w.value for w in at.warning)
    assert "main.bbl" in text
    assert "`figs/gone` (referenced in `main.tex`)" in text


def test_download_contains_cleaned_zip(upload, monkeypatch):
    captured = {}
    real = streamlit.download_button

    def spy(label, data, file_name, **kwargs):
        captured.update(data=data, file_name=file_name)
        return real(label, data, file_name, **kwargs)

    monkeypatch.setattr(streamlit, "download_button", spy)
    upload(make_zip({"main.tex": doc("Hi % secret")}), name="my paper.zip")
    click_clean(run_app())
    assert captured["file_name"] == "my paper_cleaned.zip"
    assert b"secret" not in unzip(captured["data"])["main.tex"]


def test_results_dashboard_when_ready(upload):
    upload(
        make_zip(
            {
                "paper/main.tex": doc("Hi % a long private comment\n\\bibliography{refs}"),
                "paper/main.bbl": "bbl",
                "paper/refs.bib": "@a{}",
                "paper/main.log": "log",
            }
        )
    )
    at = click_clean(run_app())
    files, size, main = at.metric
    assert (files.label, files.value, files.delta) == ("Files", "2", "-2 files")
    assert size.label == "Size" and size.delta.startswith("-")
    assert main.label == "main.tex" and "comments & drafts" in main.delta
    assert any("Ready for arXiv!" in m for m in at.markdown.values)
    assert not any("⚠️" in m for m in at.markdown.values)
    assert at.status[0].label.startswith("Cleaned in")


def test_results_dashboard_lists_problems(upload):
    upload(make_zip({"main.tex": doc("\\includegraphics{gone}\\includegraphics{figs/d}\\bibliography{refs}"), "figs/d.eps": "x"}))
    at = click_clean(run_app())
    checklist = next(m for m in at.markdown.values if "Every referenced file" in m)
    assert "⚠️ Every referenced file is in the upload" in checklist
    assert "⚠️ Nothing your paper uses was dropped" in checklist
    assert "⚠️ Bibliography compiled" in checklist
    assert "✅ Under arXiv's 50 MB limit" in checklist
    assert "✅ Final version, not the anonymous submission" in checklist
    assert any("Almost there" in m for m in at.markdown.values)


def test_what_changed_lists_files(upload):
    upload(make_zip({"main.tex": doc(), "unused.png": "x", "main.aux": "x"}))
    at = click_clean(run_app())
    assert at.expander[-1].label == "📂 What changed: 1 kept, 2 removed"
    kept, removed = at.tabs[-2:]
    assert kept.label == "✅ Kept (1)" and removed.label == "🗑️ Removed (2)"


@pytest.mark.skipif(not cleaner.shutil.which("bibtex"), reason="BibTeX not installed")
def test_generated_bbl_is_announced(upload):
    bib = "@article{a, author = {Ann A}, title = {T}, journal = {J}, year = {2020}}"
    upload(make_zip({"main.tex": doc("\\cite{a}\\bibliographystyle{plain}\\bibliography{refs}"), "refs.bib": bib}))
    at = click_clean(run_app())
    assert "Generated `main.bbl` with BibTeX" in at.info[0].value
    checklist = next(m for m in at.markdown.values if "Bibliography compiled" in m)
    assert "✅ Bibliography compiled (`main.bbl` generated)" in checklist
    assert any("Ready for arXiv!" in m for m in at.markdown.values)


# --- ACL mode ---------------------------------------------------------------------

from paperready import acl  # noqa: E402
from paperready.acl import AclReport, Issue  # noqa: E402



def run_acl_mode():
    at = run_app()
    at.button(key="mode_acl").click().run()
    assert not at.exception
    return at


@pytest.fixture
def fake_check(monkeypatch):
    """Replaces aclpubcheck with a canned report and records the call."""
    calls = []

    def set_report(report):
        def check(pdf_bytes, paper_type, check_bottom, check_references, check_names):
            calls.append((pdf_bytes, paper_type, check_bottom, check_references, check_names))
            return report

        monkeypatch.setattr(acl, "check_pdf", check)
        return calls

    return set_report


def test_mode_switch():
    at = run_app()
    assert at.query_params["mode"] == ["arxiv"]
    assert at.get("file_uploader")[0].label == "Upload your LaTeX project (.zip)"
    assert at.button(key="mode_arxiv").label == "✓ Selected"
    at.button(key="mode_acl").click().run()
    assert at.query_params["mode"] == ["acl"]
    assert at.get("file_uploader")[0].label == "Upload your paper (PDF)"
    assert at.button(key="mode_acl").label == "✓ Selected"
    assert at.button(key="mode_arxiv").label == "Choose"


def test_mode_from_url():
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["mode"] = "acl"
    at.run()
    assert at.get("file_uploader")[0].label == "Upload your paper (PDF)"


def test_unknown_mode_falls_back_to_arxiv():
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["mode"] = "nope"
    at.run()
    assert at.query_params["mode"] == ["arxiv"]
    assert at.get("file_uploader")[0].label == "Upload your LaTeX project (.zip)"


def test_footer_is_always_shown():
    for at in (run_app(), run_acl_mode()):
        assert any("Nothing is stored" in c for c in at.caption.values)


def test_acl_mode_defaults():
    at = run_acl_mode()
    assert at.button_group[0].label == "Paper type" and at.button_group[0].value == "long"
    assert [(c.label, c.value) for c in at.checkbox] == [
        ("Check that the bottom margin is empty", True),
        ("Check reference links (DOIs, arXiv links)", True),
        ("Check author names online (slow)", False),
    ]
    assert "camera-ready" in at.info[0].value
    assert "Check my paper" not in labels(at.button)


def test_acl_options_are_passed(upload, fake_check):
    calls = fake_check(AclReport())
    upload(b"%PDF-1.5 paper", name="paper.pdf")
    at = run_acl_mode()
    at.button_group[0].set_value("short")
    at.checkbox[0].uncheck()
    at.checkbox[2].check()
    at.run()
    button(at, "Check my paper").click().run()
    assert calls == [(b"%PDF-1.5 paper", "short", False, True, True)]
    assert at.success[0].value == "All clear! No formatting errors found."
    assert [m.value for m in at.metric] == ["0", "0", "0"]


def test_acl_report_rendering(upload, fake_check):
    fake_check(
        AclReport(
            errors=[
                Issue("Margins", "Text on page 2 bleeds into the left margin.", 3),
                Issue("Fonts", "Wrong font."),
            ],
            warnings=[Issue("References", "Only 1 DOI.")],
            page_images={2: _png()},
        )
    )
    upload(b"%PDF-1.5", name="paper.pdf")
    at = run_acl_mode()
    button(at, "Check my paper").click().run()
    assert not at.exception
    assert "Found 4 formatting errors in 2 places" in at.error[0].value
    assert [m.value for m in at.metric] == ["4", "1", "1"]
    labels = [e.label for e in at.expander]
    assert "❌ Margins: 3 errors" in labels and "❌ Fonts: 1 errors" in labels
    assert "⚠️ References: 1 warnings" in labels
    assert "🔎 Flagged pages (1): problem areas in red" in labels
    assert any("×3" in m for m in at.markdown.values)
    assert not at.warning  # not a review version


def test_acl_review_version_warning(upload, fake_check):
    fake_check(AclReport(errors=[Issue("Margins", "x", 900)], likely_review_version=True))
    upload(b"%PDF-1.5", name="paper.pdf")
    at = run_acl_mode()
    button(at, "Check my paper").click().run()
    assert "review version" in at.warning[0].value


def test_acl_error(upload, monkeypatch):
    def fail(*args, **kwargs):
        raise acl.AclCheckFailedError("aclpubcheck could not check this PDF (boom).", "Traceback")

    monkeypatch.setattr(acl, "check_pdf", fail)
    upload(b"%PDF-1.5", name="paper.pdf")
    at = run_acl_mode()
    button(at, "Check my paper").click().run()
    assert "boom" in at.error[0].value and at.code[0].value == "Traceback"


def test_acl_report_download():
    report = AclReport(errors=[Issue("Margins", "m", 2)], warnings=[Issue("References", "r")])
    from views.acl_view import report_markdown

    text = report_markdown(report, "paper.pdf", "long")
    assert "# ACL format check: paper.pdf" in text and "Long paper (9 pages)" in text
    assert "- Margins: m (×2)" in text and "- References: r" in text
    assert "**Passed**" in report_markdown(AclReport(), "p.pdf", "demo")


def _png():
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
    return buffer.getvalue()


def test_review_version_in_checklist(upload):
    upload(make_zip({"main.tex": doc("Hi", "\\usepackage[review]{acl}"), "acl.sty": "%"}))
    at = click_clean(run_app())
    checklist = next(m for m in at.markdown.values if "Final version" in m)
    assert "⚠️ Final version, not the anonymous submission" in checklist
    assert any("submission version, not a final one" in w.value for w in at.warning)
