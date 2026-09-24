# 🧹 ArxivCleanTex

A simple web app to clean your LaTeX paper for arXiv submission.
**Upload your project as a `.zip` → get back a cleaned, submission-ready `.zip`.**

It strips comments, removes unused files, and (optionally) deletes helper
commands like `\todo{}` — powered by
[`arxiv_latex_cleaner`](https://github.com/google-research/arxiv-latex-cleaner).

## Use it

1. Zip your LaTeX project (the folder with `main.tex`, figures, `.bib`, `.sty`…).
   - On Overleaf: **Menu → Download → Source** gives you exactly this zip.
2. Open the app, drag the `.zip` in, click **Clean my paper**.
   - The main file is found automatically, even inside nested folders
     (`main.tex` is preferred; otherwise any file with `\documentclass` and
     `\begin{document}`). Junk like `__MACOSX/` is ignored.
   - If your zip holds several papers or the main file has another name, type
     it in **Main .tex file or folder**: a file (`paper.tex`), a path
     (`src/paper.tex`) or a folder (`my-paper`).
   - Files referenced via `\input`, `\include`, `\includegraphics` or
     `\bibliography` that are missing from the zip are listed by name, so you
     know exactly what to add before uploading to arXiv.
3. Download the cleaned `.zip` and upload it to arXiv.

> **Bibliography:** arXiv does not run BibTeX/Biber, and `.bib` files are removed
> unless you tick *Keep .bib files*. Include the compiled `main.bbl` (named like
> your main file) in the zip. On Overleaf it is under **Logs and output files →
> Other logs and files**. The app warns you when it is missing.

## Run it locally

```bash
uv sync
uv run streamlit run streamlit_app.py
```

Then open the URL it prints (usually http://localhost:8501).

## Run the tests

```bash
uv run pytest
```

`tests/test_cleaner.py` covers the cleaning pipeline (zip extraction, junk and
encoding handling, main-file detection, missing-file checks and end-to-end
cleaning); `tests/test_streamlit_app.py` drives the web UI with Streamlit's
`AppTest`.

## Deploy it live (free)

The app is designed for **Streamlit Community Cloud**, which hosts it from this
GitHub repo at a public URL and redeploys on every push:

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **Create app** → pick this repo → set the main file to `streamlit_app.py`.
3. **Deploy.** You get a permanent `https://…streamlit.app` link to share.

Dependencies are read from `requirements.txt` automatically.

## Project layout

```
streamlit_app.py   the web UI (upload → clean → download)
cleaner.py         the cleaning logic (unzip → arxiv_latex_cleaner → zip)
requirements.txt   dependencies for Streamlit Community Cloud
pyproject.toml     dependencies for local dev with uv
tests/             pytest suite (uv run pytest)
```
