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
3. Download the cleaned `.zip` and upload it to arXiv.

## Run it locally

```bash
uv sync
uv run streamlit run streamlit_app.py
```

Then open the URL it prints (usually http://localhost:8501).

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
```
