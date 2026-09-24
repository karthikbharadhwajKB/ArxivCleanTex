"""The “Check ACL format” mode: run aclpubcheck on a paper's PDF."""

import time

import streamlit as st

from paperready import acl
from paperready.core import CleanerError
from views import feedback_view


def report_markdown(report, file_name, paper_type):
    lines = [f"# ACL format check: {file_name}", "", f"Paper type: {acl.PAPER_TYPES[paper_type]}", ""]
    lines.append("**Passed**" if report.passed else f"**{report.error_count} errors**")
    for title, issues in (("Errors", report.errors), ("Warnings", report.warnings)):
        if issues:
            lines += ["", f"## {title}", ""]
            lines += [
                f"- {i.category}: {i.message}" + (f" (×{i.count})" if i.count > 1 else "") for i in issues
            ]
    return "\n".join(lines) + "\n"


def check_options():
    """Paper type and the “Check options” expander. Returns (paper_type, options dict)."""
    paper_type = (
        st.segmented_control(
            "Paper type",
            list(acl.PAPER_TYPES),
            format_func=acl.PAPER_TYPES.get,
            default="long",
            help="Sets the page limit for the main text. References, limitations, "
            "ethics and acknowledgments may follow it.",
        )
        or "long"
    )
    with st.expander("⚙️ Check options"):
        check_bottom = st.checkbox(
            "Check that the bottom margin is empty",
            value=True,
            help="The proceedings add page numbers there, so it should be blank.",
        )
        check_references = st.checkbox(
            "Check reference links (DOIs, arXiv links)",
            value=True,
            help="Warns when few references have ACL Anthology DOIs or too many point to arXiv.",
        )
        check_names = st.checkbox(
            "Check author names online (slow)",
            value=False,
            help="Sends the PDF's references to the Scholarcy API and compares author "
            "names with ACL Anthology, DBLP and arXiv to catch outdated names.",
        )
    return paper_type, {
        "check_bottom": check_bottom,
        "check_references": check_references,
        "check_names": check_names,
    }


def check(pdf_bytes, paper_type, options):
    """Runs aclpubcheck with a live status; shows errors and stops on failure."""
    started = time.monotonic()
    try:
        with st.status("Checking your paper…", expanded=True) as status:
            st.write("📏 Running aclpubcheck: page size, margins, page limit, fonts…")
            report = acl.check_pdf(
                pdf_bytes,
                paper_type,
                options["check_bottom"],
                options["check_references"],
                options["check_names"],
            )
            status.update(
                label=f"Checked in {time.monotonic() - started:.1f} s",
                state="complete",
                expanded=False,
            )
    except CleanerError as error:
        st.error(str(error))
        if getattr(error, "details", ""):
            with st.expander("Technical details"):
                st.code(error.details, language="text")
        feedback_view.report_button(str(error))
        st.stop()
    except Exception as error:
        st.error(f"Something went wrong while checking: {error}")
        feedback_view.report_button(f"Something went wrong while checking: {error}")
        st.stop()
    return report


def show_report(report, upload_name, paper_type):
    """Verdict, metrics, grouped issues, flagged pages and the report download."""
    if report.likely_review_version:
        st.warning(
            "This looks like a **review version**: it has an anonymous author line or "
            "line numbers in the margins (which show up as hundreds of margin errors). "
            "Switch your template to the final version (e.g. `\\usepackage[final]{acl}`) "
            "and check again."
        )
    if report.passed:
        st.success("All clear! No formatting errors found.")
    else:
        st.error(
            f"Found {report.error_count} formatting errors in {len(report.errors)} places. "
            "Errors must be fixed before publication."
        )

    errors_col, warnings_col, pages_col = st.columns(3)
    errors_col.metric("Errors", report.error_count, border=True)
    warnings_col.metric("Warnings", report.warning_count, border=True)
    pages_col.metric("Pages flagged", len(report.page_images), border=True)

    for title, issues, icon in (("Errors", report.errors, "❌"), ("Warnings", report.warnings, "⚠️")):
        by_category = {}
        for issue in issues:
            by_category.setdefault(issue.category, []).append(issue)
        for category, items in by_category.items():
            total = sum(i.count for i in items)
            expanded = title == "Errors" and not report.likely_review_version
            with st.expander(f"{icon} {category}: {total} {title.lower()}", expanded=expanded):
                st.markdown(
                    "\n".join(f"- {i.message}" + (f" **×{i.count}**" if i.count > 1 else "") for i in items)
                )

    if report.page_images:
        with st.expander(f"🔎 Flagged pages ({len(report.page_images)}): problem areas in red"):
            page = st.select_slider("Page", options=list(report.page_images))
            st.image(report.page_images[page], caption=f"Page {page}", width="stretch")

    if report.warnings:
        st.caption(
            "Warnings are recommendations; margin errors next to figures can be spurious "
            "when an image has a white border."
        )

    st.download_button(
        "Download report (.md)",
        data=report_markdown(report, upload_name, paper_type),
        file_name=upload_name.rsplit(".", 1)[0] + "_aclpubcheck.md",
        mime="text/markdown",
        icon="⬇️",
        width="stretch",
    )


def render():
    uploaded = st.file_uploader("Upload your paper (PDF)", type=["pdf"])
    paper_type, options = check_options()

    if uploaded is None:
        st.info(
            "👆 Upload the **camera-ready** PDF of an ACL-style paper. Review versions "
            "(with line numbers) trigger many false margin errors."
        )
        st.stop()
    if not st.button("Check my paper", type="primary", icon="📏", width="stretch"):
        st.stop()

    report = check(uploaded.getvalue(), paper_type, options)
    show_report(report, uploaded.name, paper_type)
