"""Ways to report a problem: a button on every error and a section above the footer."""

import streamlit as st

from paperready import feedback


def report_button(error):
    """Offers to open a GitHub issue filled in with this error."""
    st.link_button(
        "Report this problem",
        feedback.bug_report_url(st.query_params.get("mode"), error),
        icon="🐞",
        help="Opens a GitHub issue with the error filled in. Your files are not attached.",
    )


def render():
    with st.container(border=True):
        st.markdown(
            "**💬 Found a problem or missing a feature?**  \n"
            "PaperReady gets better with every report. Tell us what "
            "went wrong and we'll fix it. Your files are never shared unless you "
            "attach them yourself."
        )
        bug_col, idea_col = st.columns(2)
        bug_col.link_button(
            "Report a problem",
            feedback.bug_report_url(st.query_params.get("mode")),
            icon="🐞",
            width="stretch",
        )
        idea_col.link_button("Suggest a feature", feedback.feature_request_url(), icon="💡", width="stretch")
