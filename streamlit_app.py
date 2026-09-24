import streamlit as st

from cleaner import CleanerError, CleanerOptions, build_cleaner_args, clean_zip

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
    st.caption(
        "All options are passed to arxiv_latex_cleaner; defaults match its own. "
        "Folders and image paths are relative to the main file's folder."
    )

    st.markdown("**Remove content**")
    commands = st.text_input(
        "Commands to delete (e.g. todo note, or \\todo \\note)",
        help="Deletes \\todo{...} together with its argument.",
    )
    commands_only = st.text_input(
        "Commands to unwrap, keeping their text (e.g. hl)",
        help="\\hl{text} becomes text. Only for one-argument commands: "
        "\\textcolor{red}{text} would keep “red”.",
    )
    environments = st.text_input(
        "Environments to delete (e.g. note)",
        help="Deletes \\begin{note} … \\end{note}.",
    )
    if_exceptions = st.text_input(
        "\\if commands that are not conditionals (e.g. ifdraft)",
        help="The cleaner treats every \\if… as a TeX conditional; list exceptions here.",
    )

    st.markdown("**Images**")
    resize = st.checkbox("Resize images to reduce size")
    im_size = st.number_input(
        "Max image size (pixels, longest side)",
        min_value=100, value=500, step=100, disabled=not resize,
    )
    convert_png = st.checkbox("Convert PNG images to JPG")
    png_quality = st.slider("JPG quality", 0, 100, 50, disabled=not convert_png)
    png_threshold = st.number_input(
        "Only convert PNGs larger than (MB)",
        min_value=0.0, value=0.5, step=0.1, disabled=not convert_png,
    )
    compress_pdf = st.checkbox("Compress PDF figures (Ghostscript)")
    pdf_resolution = st.number_input(
        "PDF image resolution (dpi)",
        min_value=50, value=500, step=50, disabled=not compress_pdf,
    )
    images_allowlist = st.text_area(
        "Image allowlist (JSON: path → size in pixels, or dpi for PDFs)",
        placeholder='{"figs/teaser.png": 2000}',
        help="These images are resized to their own size instead of the global one.",
    )

    st.markdown("**Other**")
    keep_bib = st.checkbox("Keep .bib files")
    external_tikz = st.text_input(
        "Folder with externalized TikZ PDFs (optional)",
        help="Replaces \\tikzsetnextfilename{x} + tikzpicture with \\includegraphics{folder/x.pdf}.",
    )
    svg_inkscape = st.checkbox("Use Inkscape-exported SVGs (\\includesvg)")
    svg_path = st.text_input(
        "Inkscape output folder", placeholder="svg-inkscape", disabled=not svg_inkscape
    )
    config_file = st.file_uploader(
        "cleaner_config.yaml (optional)",
        type=["yaml", "yml"],
        help="An arxiv_latex_cleaner config, e.g. with patterns_and_insertions. "
        "Options set above take precedence.",
    )

if uploaded is not None and st.button("Clean my paper", type="primary"):
    options = CleanerOptions(
        keep_bib=keep_bib,
        resize_images=resize,
        im_size=int(im_size),
        compress_pdf=compress_pdf,
        pdf_im_resolution=int(pdf_resolution),
        images_allowlist=images_allowlist,
        convert_png_to_jpg=convert_png,
        png_quality=int(png_quality),
        png_size_threshold=float(png_threshold),
        commands_to_delete=commands,
        commands_only_to_delete=commands_only,
        environments_to_delete=environments,
        if_exceptions=if_exceptions,
        use_external_tikz=external_tikz,
        svg_inkscape=svg_inkscape,
        svg_inkscape_path=svg_path,
    )

    try:
        with st.spinner("Cleaning..."):
            extra, notes = build_cleaner_args(options)
            for note in notes:
                st.warning(note)
            config = config_file.getvalue() if config_file is not None else None
            result = clean_zip(uploaded.getvalue(), extra, main_hint, config)
    except CleanerError as error:
        st.error(str(error).replace("\n", "  \n"))
        if getattr(error, "details", ""):
            with st.expander("Technical details"):
                st.code(error.details, language="text")
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
