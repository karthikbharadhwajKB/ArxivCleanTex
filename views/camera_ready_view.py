"""The “ACL camera-ready” mode: check the PDF and clean the source in one go."""

import streamlit as st

from paperready import acl, camera_ready
from views import acl_view, arxiv_view
from views.acl_view import plural


def render():
    zip_upload = st.file_uploader("Upload your LaTeX project (.zip)", type=["zip"])
    pdf_upload = st.file_uploader("Upload your camera-ready PDF", type=["pdf"])
    main_hint = arxiv_view.main_file_input()
    paper_type, check_options = acl_view.check_options()
    options, make_bbl, config = arxiv_view.cleaning_options()

    if zip_upload is None or pdf_upload is None:
        missing = " and ".join(
            name
            for name, upload in (("your project's .zip", zip_upload), ("its camera-ready PDF", pdf_upload))
            if upload is None
        )
        st.info(f"👆 Upload {missing} to get started.")
        st.stop()
    if not st.button("Check and clean my paper", type="primary", icon="🎓", width="stretch"):
        st.stop()

    pdf_bytes = pdf_upload.getvalue()
    report = acl_view.check(pdf_bytes, paper_type, check_options)
    result = arxiv_view.clean(zip_upload.getvalue(), main_hint, options, make_bbl, config)
    checks = camera_ready.cross_checks(result, report, acl.first_page_text(pdf_bytes))

    arxiv_problems = sum(not ok for ok, _ in arxiv_view.readiness_checks(result))
    summary = [
        (
            report.passed,
            "ACL format check passed"
            if report.passed
            else f"ACL format: {plural(report.error_count, 'error')} to fix (see the ACL tab)",
        ),
        (
            arxiv_problems == 0,
            "Source is ready for arXiv"
            if arxiv_problems == 0
            else f"arXiv: {plural(arxiv_problems, 'item')} to fix (see the arXiv tab)",
        ),
        *((check.ok, check.title) for check in checks),
    ]
    ready = all(ok for ok, _ in summary)

    with st.container(border=True):
        st.markdown(
            "**🎓 Camera-ready and arXiv-ready!**" if ready else "**🛠️ Almost there: fix the items marked ⚠️**"
        )
        st.markdown("\n".join(f"- {'✅' if ok else '⚠️'} {text}" for ok, text in summary))
    for check in checks:
        if check.detail:
            (st.info if check.ok else st.warning)(check.detail)

    acl_tab, arxiv_tab = st.tabs(["📏 ACL format (PDF)", "🧹 arXiv (source)"])
    with acl_tab:
        acl_view.show_report(report, pdf_upload.name, paper_type)
    with arxiv_tab:
        arxiv_view.show_results(result, zip_upload.name, celebrate=False)

    if ready:
        st.balloons()
