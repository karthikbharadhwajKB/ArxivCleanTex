from pathlib import Path

import streamlit as st

from paperready import feedback
from views import acl_view, arxiv_view, camera_ready_view, feedback_view

ASSETS = Path(__file__).parent / "assets"

st.set_page_config(page_title="PaperReady", page_icon=str(ASSETS / "icon-192.png"), layout="centered")
st.logo(str(ASSETS / "icon.svg"), size="large")

MODES = {
    "arxiv": {
        "logo": ASSETS / "arxiv.svg",
        "name": "Prepare for arXiv",
        "summary": "Strip comments and unused files, add the bibliography.",
        "upload": "LaTeX project (.zip)",
        "steps": [
            ("📦", "Upload", "Your project as a .zip, e.g. Overleaf's *Download Source*."),
            ("🧹", "Clean", "Comments, todos and unused files are removed."),
            ("🚀", "Submit", "Download the cleaned .zip and upload it to arXiv."),
        ],
        "view": arxiv_view,
    },
    "acl": {
        "logo": ASSETS / "acl.svg",
        "name": "Check ACL format",
        "summary": "Check margins, fonts, page limit and references.",
        "upload": "Camera-ready PDF",
        "steps": [
            ("📄", "Upload", "The camera-ready PDF of your paper."),
            ("📏", "Check", "Page size, margins, page limit, fonts and references."),
            ("✅", "Fix", "Every problem by page, marked in red."),
        ],
        "view": acl_view,
    },
    "camera": {
        "logo": ASSETS / "icon.svg",
        "name": "ACL camera-ready",
        "summary": "Check the PDF and clean the source, and match the two.",
        "upload": "Zip + PDF",
        "steps": [
            ("📦", "Upload", "Your project .zip and its camera-ready PDF."),
            ("🔎", "Check", "ACL format, arXiv cleaning, and whether the two match."),
            ("🎓", "Submit", "The PDF to the proceedings, the zip to arXiv."),
        ],
        "view": camera_ready_view,
    },
}

# The mode lives in the URL (?mode=acl), so each mode can be linked to directly.
if st.query_params.get("mode") not in MODES:
    st.query_params["mode"] = "arxiv"
mode = st.query_params["mode"]

# --- Hero -----------------------------------------------------------------------

logo_col, title_col = st.columns([1, 6], vertical_alignment="center")
logo_col.image(str(ASSETS / "icon.svg"), width=84)
with title_col:
    st.title("PaperReady")
    st.markdown("**Get your paper ready to submit**, for arXiv and ACL venues.")

# --- Mode cards -----------------------------------------------------------------

for column, (key, info) in zip(st.columns(len(MODES)), MODES.items(), strict=True):
    selected = key == mode
    with column.container(border=True):
        st.image(str(info["logo"]), width=56)
        st.markdown(f"##### {info['name']}\n{info['summary']}")
        st.caption(f"You upload: {info['upload']}")
        if (
            st.button(
                "✓ Selected" if selected else "Choose",
                key=f"mode_{key}",
                type="primary" if selected else "secondary",
                width="stretch",
            )
            and not selected
        ):
            st.query_params["mode"] = key
            st.rerun()

current = MODES[mode]
st.caption("HOW IT WORKS")
for column, (number, (icon, title, text)) in zip(st.columns(3), enumerate(current["steps"], 1), strict=True):
    with column.container(border=True):
        st.markdown(f"#### {icon}\n**{number} · {title}**  \n{text}")

# The views stop the script early (e.g. while waiting for an upload), so the
# footer's place is reserved first and filled before the view runs.
body, footer = st.container(), st.container()
with footer:
    feedback_view.render()
    st.divider()
    st.caption(
        "PaperReady runs Google's "
        "[arxiv_latex_cleaner](https://github.com/google-research/arxiv-latex-cleaner) "
        "and ACL's [aclpubcheck](https://github.com/acl-org/aclpubcheck). "
        "Nothing is stored: uploads are processed in a temporary folder and deleted "
        "right away."
    )
    st.markdown(
        f"Made by **[{feedback.AUTHOR}]({feedback.AUTHOR_URL})** · © 2026 · "
        f"⭐ [Source on GitHub]({feedback.REPO_URL}) · "
        f"[Report an issue]({feedback.REPO_URL}/issues)"
    )
with body:
    current["view"].render()
