import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import cleaner
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

    def set_upload(data, name="paper.zip"):
        monkeypatch.setattr(streamlit, "file_uploader", lambda *a, **k: FakeUpload(data, name))

    return set_upload


@pytest.fixture
def spy_clean_zip(monkeypatch):
    """Records the arguments the app passes to clean_zip."""
    calls = []
    real = cleaner.clean_zip

    def spy(zip_bytes, extra_args=None, main_hint=None):
        calls.append({"extra_args": extra_args, "main_hint": main_hint})
        return real(zip_bytes, extra_args, main_hint)

    monkeypatch.setattr(cleaner, "clean_zip", spy)
    return calls


def run_app():
    return AppTest.from_file(APP, default_timeout=60).run()


def click_clean(at):
    at.button[0].click().run()
    assert not at.exception
    return at


def test_initial_page():
    at = run_app()
    assert not at.exception
    assert at.title[0].value == "🧹 ArxivCleanTex"
    assert [t.label for t in at.text_input] == [
        "Main .tex file or folder (optional)",
        "Commands to delete (e.g. todo note, or \\todo \\note)",
    ]
    assert [c.label for c in at.checkbox] == ["Keep .bib files", "Resize images to reduce size"]
    assert len(at.button) == 0  # no button until a zip is uploaded


def test_image_size_only_enabled_when_resizing():
    at = run_app()
    assert at.number_input[0].disabled
    at.checkbox[1].check().run()
    assert not at.number_input[0].disabled


def test_successful_clean(upload):
    upload(make_zip({"paper/main.tex": doc("Hi % secret")}))
    at = click_clean(run_app())
    assert "paper/main.tex" in at.success[0].value
    assert not at.warning and not at.error
    # The download button is rendered with the cleaned zip.
    assert at.get("download_button")


def test_options_are_passed_to_the_cleaner(upload, spy_clean_zip):
    upload(make_zip({"p1/main.tex": doc(), "p2/main.tex": doc()}))
    at = run_app()
    at.text_input[0].input("p2")
    at.checkbox[0].check()
    at.checkbox[1].check()
    at.run()
    at.number_input[0].set_value(800)
    at.text_input[1].input("\\todo, note bad*")
    click_clean(at)
    assert spy_clean_zip[-1] == {
        "extra_args": [
            "--keep_bib",
            "--resize_images",
            "--im_size",
            "800",
            "--commands_to_delete",
            "todo",
            "note",
        ],
        "main_hint": "p2",
    }
    assert any("bad*" in w.value for w in at.warning)


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
