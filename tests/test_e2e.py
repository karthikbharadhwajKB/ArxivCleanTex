"""End-to-end tests: the real app in a real browser (Chromium via Playwright).

They start `streamlit run streamlit_app.py` on a free port, upload files and
download results like a user would. Skipped when Playwright or a browser is
not available (CI requires them; see tests/conftest.py).
"""

import glob
import io
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from helpers import doc, make_zip

ROOT = Path(__file__).resolve().parent.parent

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_url():
    port = _free_port()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "streamlit_app.py",
            "--server.headless",
            "true",
            "--server.port",
            str(port),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{url}/_stcore/health", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("the app did not start")
        yield url
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception:
            # Fall back to a preinstalled Chromium (e.g. CHROMIUM_EXECUTABLE or /opt/pw-browsers).
            candidates = [os.environ.get("CHROMIUM_EXECUTABLE", "")]
            candidates += glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
            path = next((c for c in candidates if c and os.path.exists(c)), None)
            if not path:
                pytest.skip("no Chromium available for Playwright")
            chromium = p.chromium.launch(executable_path=path)
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser, app_url):
    page = browser.new_page(viewport={"width": 1100, "height": 1400})
    page.set_default_timeout(60_000)
    yield page
    page.close()


def open_mode(page, app_url, mode):
    page.goto(f"{app_url}/?mode={mode}")
    page.wait_for_selector("text=Get your paper ready to submit")


def upload(page, tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    page.locator("input[type=file]").first.set_input_files(str(path))


def test_home_page_and_mode_switch(page, app_url):
    open_mode(page, app_url, "arxiv")
    page.wait_for_selector("text=Upload your LaTeX project (.zip)")
    assert page.title() == "PaperReady"
    page.get_by_role("button", name="Choose", exact=True).click()
    page.wait_for_selector("text=Upload your paper (PDF)")
    assert page.url.endswith("?mode=acl")
    assert page.get_by_text("Nothing is stored").is_visible()


def test_arxiv_cleaning_end_to_end(page, app_url, tmp_path):
    project = make_zip(
        {
            "paper/main.tex": doc("Hello % private\n\\includegraphics{figs/plot}"),
            "paper/figs/plot.png": "png",
            "paper/figs/unused.png": "png",
            "__MACOSX/paper/._main.tex": b"\x00\x05\x16\x07",
        }
    )
    open_mode(page, app_url, "arxiv")
    upload(page, tmp_path, "paper.zip", project)
    page.get_by_role("button", name="Clean my paper").click()
    page.wait_for_selector("text=Download cleaned .zip")
    assert page.get_by_text("Done! Main file: paper/main.tex").is_visible()
    assert page.get_by_text("Ready for arXiv!").is_visible()

    with page.expect_download() as download:
        page.get_by_role("button", name="Download cleaned .zip").click()
    assert download.value.suggested_filename == "paper_cleaned.zip"
    cleaned = zipfile.ZipFile(io.BytesIO(Path(download.value.path()).read_bytes()))
    assert sorted(cleaned.namelist()) == ["figs/plot.png", "main.tex"]
    assert b"private" not in cleaned.read("main.tex")


def test_arxiv_error_is_shown(page, app_url, tmp_path):
    open_mode(page, app_url, "arxiv")
    upload(page, tmp_path, "broken.zip", b"this is not a zip")
    page.get_by_role("button", name="Clean my paper").click()
    page.wait_for_selector("text=not a valid .zip archive")


def test_arxiv_options_reach_the_cleaner(page, app_url, tmp_path):
    open_mode(page, app_url, "arxiv")
    upload(page, tmp_path, "paper.zip", make_zip({"main.tex": doc("A\\todo{secret}B")}))
    page.get_by_text("Cleaning options").click()
    page.get_by_role(
        "textbox", name="Commands to delete (e.g. todo note, or \\todo \\note)", exact=True
    ).fill("\\todo")
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Clean my paper").click()
    page.wait_for_selector("text=Download cleaned .zip")
    with page.expect_download() as download:
        page.get_by_role("button", name="Download cleaned .zip").click()
    main = zipfile.ZipFile(download.value.path()).read("main.tex")
    assert b"secret" not in main and b"AB" in main


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex not installed")
def test_acl_check_end_to_end(page, app_url, tmp_path):
    tex = tmp_path / "paper.tex"
    tex.write_text(
        "\\documentclass{article}\\usepackage{lipsum}\\begin{document}"
        "\\lipsum[1-60]\\section*{References}X\\end{document}"
    )
    subprocess.run(["pdflatex", "-interaction=nonstopmode", tex.name], cwd=tmp_path, capture_output=True)

    open_mode(page, app_url, "acl")
    page.locator("input[type=file]").first.set_input_files(str(tmp_path / "paper.pdf"))
    page.get_by_role("button", name="Short paper (5 pages)").click()
    page.get_by_role("button", name="Check my paper").click()
    page.wait_for_selector("text=Download report (.md)", timeout=180_000)
    assert page.get_by_text("formatting errors").first.is_visible()
    assert page.get_by_text("Page limit").first.is_visible()
    assert page.get_by_text("Fonts").first.is_visible()
