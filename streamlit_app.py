import time
from pathlib import Path

import streamlit as st

from arxivcleantex.arxiv import (
    ARXIV_SIZE_LIMIT,
    CleanerError,
    CleanerOptions,
    build_cleaner_args,
    clean_zip,
)

ASSETS = Path(__file__).parent / "assets"

st.set_page_config(
    page_title="ArxivCleanTex", page_icon=str(ASSETS / "icon-192.png"), layout="centered"
)
st.logo(str(ASSETS / "icon.svg"), size="large")


def human_size(num_bytes):
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def file_table(files):
    return [{"File": name, "Size": human_size(size)} for name, size in files.items()]


# --- Header ---------------------------------------------------------------------

st.title("🧹 ArxivCleanTex")
st.markdown(
    "#### Your LaTeX project, ready for arXiv in one click\n"
    "Strip private comments, drop unused files and catch what would break "
    "arXiv's build, powered by Google's "
    "[arxiv_latex_cleaner](https://github.com/google-research/arxiv-latex-cleaner)."
)

for column, (icon, title, text) in zip(
    st.columns(3),
    [
        ("📦", "1 · Upload", "Your project as a .zip, e.g. Overleaf's *Download Source*."),
        ("🧹", "2 · Clean", "Comments, todos and unused files are removed."),
        ("🚀", "3 · Submit", "Download the cleaned .zip and upload it to arXiv."),
    ],
):
    with column.container(border=True):
        st.markdown(f"### {icon}\n**{title}**  \n{text}")

# --- Inputs ---------------------------------------------------------------------

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

with st.expander("⚙️ Cleaning options"):
    st.caption(
        "All options are passed to arxiv_latex_cleaner; defaults match its own. "
        "Folders and image paths are relative to the main file's folder."
    )
    content_tab, images_tab, other_tab = st.tabs(
        ["✂️ Remove content", "🖼️ Images", "🧩 Other"]
    )

    with content_tab:
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

    with images_tab:
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

    with other_tab:
        make_bbl = st.checkbox(
            "Generate a missing .bbl with BibTeX",
            value=True,
            help="arXiv needs the compiled bibliography (.bbl) and does not run BibTeX. "
            "If your zip has none, BibTeX builds it from your .bib and .bst files.",
        )
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

if uploaded is None:
    st.info(
        "👆 Drop your project's .zip above to get started. Nothing is stored: "
        "files are processed in a temporary folder and deleted right away."
    )
    st.stop()

# --- Cleaning -------------------------------------------------------------------

if not st.button("Clean my paper", type="primary", icon="🧹", width="stretch"):
    st.stop()

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

started = time.monotonic()
try:
    with st.status("Cleaning your paper…", expanded=True) as status:
        st.write("📦 Unpacking and finding your main .tex…")
        extra, notes = build_cleaner_args(options)
        for note in notes:
            st.warning(note)
        config = config_file.getvalue() if config_file is not None else None
        st.write("🧹 Running arxiv_latex_cleaner…")
        result = clean_zip(uploaded.getvalue(), extra, main_hint, config, make_bbl)
        st.write("🔎 Checking the result for arXiv…")
        status.update(
            label=f"Cleaned in {time.monotonic() - started:.1f} s",
            state="complete",
            expanded=False,
        )
except CleanerError as error:
    st.error(str(error).replace("\n", "  \n"))
    if getattr(error, "details", ""):
        with st.expander("Technical details"):
            st.code(error.details, language="text")
    st.stop()
except Exception as error:
    st.error(f"Something went wrong while cleaning: {error}")
    st.stop()

# --- Results --------------------------------------------------------------------

st.success(f"Done! Main file: `{result.main_file}`")
if result.generated_bbl:
    st.info(
        f"📚 Generated `{result.generated_bbl}` with BibTeX from your .bib and "
        "bibliography style, so your references will appear on arXiv."
    )

size_in = sum(result.input_files.values())
size_out = sum(result.output_files.values())
main_name = result.main_file.rsplit("/", 1)[-1]
main_in = result.input_files.get(main_name, 0)
main_out = result.output_files.get(main_name, 0)
removed = {n: s for n, s in result.input_files.items() if n not in result.output_files}

files_col, size_col, main_col = st.columns(3)
files_col.metric(
    "Files", len(result.output_files), delta=f"-{len(removed)} files" if removed else None,
    delta_color="off", border=True,
)
size_col.metric(
    "Size", human_size(size_out),
    delta=f"-{100 * (1 - size_out / size_in):.0f}%" if size_in else None,
    delta_color="inverse", border=True,
)
main_col.metric(
    main_name, human_size(main_out),
    delta=f"-{100 * (1 - main_out / main_in):.0f}% comments & drafts" if main_in else None,
    delta_color="off", border=True,
)

checks = [
    (True, f"Main file found: `{result.main_file}`"),
    (not result.missing_files, "Every referenced file is in the upload"),
    (not result.dropped_files, "Nothing your paper uses was dropped by the cleaner"),
    (
        not result.missing_bbl,
        "Bibliography compiled"
        + (f" (`{result.generated_bbl}` generated)" if result.generated_bbl else ""),
    ),
    (size_out <= ARXIV_SIZE_LIMIT, "Under arXiv's 50 MB limit"),
]
ready = all(ok for ok, _ in checks)
with st.container(border=True):
    st.markdown(
        "**🚀 Ready for arXiv!**" if ready else "**🛠️ Almost there: fix the items marked ⚠️ below**"
    )
    st.markdown("\n".join(f"- {'✅' if ok else '⚠️'} {text}" for ok, text in checks))

st.download_button(
    "Download cleaned .zip",
    data=result.zip_bytes,
    file_name=uploaded.name.rsplit(".", 1)[0] + "_cleaned.zip",
    mime="application/zip",
    type="primary",
    icon="⬇️",
    width="stretch",
)

for warning in result.warnings:
    st.warning(warning)
if result.missing_files:
    lines = "\n".join(
        f"- `{ref}` (referenced in `{source}`)" for source, ref in result.missing_files
    )
    st.warning(
        "These files are referenced but missing from your upload, so "
        "arXiv will fail to compile. Add them to the zip and re-upload:"
        f"\n\n{lines}"
    )

with st.expander(f"📂 What changed: {len(result.output_files)} kept, {len(removed)} removed"):
    kept_tab, removed_tab = st.tabs([f"✅ Kept ({len(result.output_files)})", f"🗑️ Removed ({len(removed)})"])
    kept_tab.dataframe(file_table(result.output_files), hide_index=True, width="stretch")
    removed_tab.dataframe(file_table(removed), hide_index=True, width="stretch")

if ready:
    st.balloons()
