from pathlib import Path

import streamlit as st

from views import acl_view, arxiv_view

ASSETS = Path(__file__).parent / "assets"

st.set_page_config(
    page_title="ArxivCleanTex", page_icon=str(ASSETS / "icon-192.png"), layout="centered"
)
st.logo(str(ASSETS / "icon.svg"), size="large")

ARXIV, ACL = "🧹 Prepare for arXiv", "📏 Check ACL format"

MODES = {
    ARXIV: {
        "pitch": "#### Your LaTeX project, ready for arXiv in one click\n"
        "Strip private comments, drop unused files and catch what would break "
        "arXiv's build, powered by Google's "
        "[arxiv_latex_cleaner](https://github.com/google-research/arxiv-latex-cleaner).",
        "steps": [
            ("📦", "1 · Upload", "Your project as a .zip, e.g. Overleaf's *Download Source*."),
            ("🧹", "2 · Clean", "Comments, todos and unused files are removed."),
            ("🚀", "3 · Submit", "Download the cleaned .zip and upload it to arXiv."),
        ],
        "view": arxiv_view,
    },
    ACL: {
        "pitch": "#### Camera-ready check for ACL venues\n"
        "Catch margin, font, page-limit and reference problems before the "
        "publication chairs do, with the official "
        "[aclpubcheck](https://github.com/acl-org/aclpubcheck).",
        "steps": [
            ("📄", "1 · Upload", "The camera-ready PDF of your paper."),
            ("📏", "2 · Check", "Page size, margins, page limit, fonts and references."),
            ("✅", "3 · Fix", "See every problem by page, with the areas marked in red."),
        ],
        "view": acl_view,
    },
}

st.title("🧹 ArxivCleanTex")
mode = st.segmented_control(
    "What do you want to do?", list(MODES), default=ARXIV, label_visibility="collapsed"
) or ARXIV
st.markdown(MODES[mode]["pitch"])

for column, (icon, title, text) in zip(st.columns(3), MODES[mode]["steps"]):
    with column.container(border=True):
        st.markdown(f"### {icon}\n**{title}**  \n{text}")

MODES[mode]["view"].render()
