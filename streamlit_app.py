import streamlit as st

from cleaner import clean_zip

st.set_page_config(page_title="ArxivCleanTex", page_icon="🧹")

st.title("🧹 ArxivCleanTex")
st.write(
    "Upload your LaTeX project as a .zip and download a cleaned, "
    "arXiv-ready version with comments stripped and unused files removed."
)

uploaded = st.file_uploader("Upload your LaTeX project (.zip)", type=["zip"])

with st.expander("Cleaning options"):
    keep_bib = st.checkbox("Keep .bib files", value=False)
    resize = st.checkbox("Resize images to reduce size", value=False)
    im_size = st.number_input(
        "Max image size (pixels, longest side)",
        min_value=100,
        value=1200,
        step=100,
        disabled=not resize,
    )
    commands = st.text_input(
        "Commands to delete (space-separated, e.g. todo note)", value=""
    )

if uploaded is not None and st.button("Clean my paper", type="primary"):
    extra = []
    if keep_bib:
        extra.append("--keep_bib")
    if resize:
        extra += ["--resize_images", "--im_size", str(int(im_size))]
    if commands.strip():
        extra += ["--commands_to_delete", *commands.split()]

    try:
        with st.spinner("Cleaning..."):
            cleaned = clean_zip(uploaded.getvalue(), extra)
    except ValueError as error:
        st.error(str(error))
    except Exception as error:
        st.error(f"Something went wrong while cleaning: {error}")
    else:
        st.success("Done! Your cleaned paper is ready.")
        out_name = uploaded.name.rsplit(".", 1)[0] + "_cleaned.zip"
        st.download_button(
            "Download cleaned .zip",
            data=cleaned,
            file_name=out_name,
            mime="application/zip",
        )
