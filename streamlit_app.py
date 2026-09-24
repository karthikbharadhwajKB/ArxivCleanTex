import streamlit as st

from cleaner import CleanerError, clean_zip

st.set_page_config(page_title="ArxivCleanTex", page_icon="🧹")

st.title("🧹 ArxivCleanTex")
st.write(
    "Upload your LaTeX project as a .zip and download a cleaned, "
    "arXiv-ready version with comments stripped and unused files removed."
)

uploaded = st.file_uploader("Upload your LaTeX project (.zip)", type=["zip"])

main_hint = st.text_input(
    "Main .tex file or folder (optional)",
    value="",
    placeholder="main.tex",
    help=(
        "Leave empty to auto-detect (main.tex is preferred, even inside nested "
        "folders). You can also type a file name like `paper.tex`, a path like "
        "`src/paper.tex`, or a folder name like `my-paper`."
    ),
)

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
            result = clean_zip(uploaded.getvalue(), extra, main_hint)
    except CleanerError as error:
        st.error(str(error).replace("\n", "  \n"))
    except Exception as error:
        st.error(f"Something went wrong while cleaning: {error}")
    else:
        st.success(f"Done! Main file: `{result.main_file}`")
        for warning in result.warnings:
            st.warning(warning)
        if result.missing_files:
            lines = "\n".join(
                f"- `{ref}` (referenced in `{source}`)"
                for source, ref in result.missing_files
            )
            st.warning(
                "These files are referenced but missing from your upload, so "
                "arXiv will fail to compile. Add them to the zip and re-upload:"
                f"\n\n{lines}"
            )
        out_name = uploaded.name.rsplit(".", 1)[0] + "_cleaned.zip"
        st.download_button(
            "Download cleaned .zip",
            data=result.zip_bytes,
            file_name=out_name,
            mime="application/zip",
        )
