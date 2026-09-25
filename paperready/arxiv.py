"""Preparing a LaTeX project for arXiv around arxiv_latex_cleaner."""

import codecs
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from .core import CleanerError, _file_sizes, _is_ignored, _remove_junk, _safe_extract

DEFAULT_MAIN = "main.tex"

# arXiv's upload limit (see the arxiv_latex_cleaner README).
ARXIV_SIZE_LIMIT = 50 * 1024 * 1024

# arxiv_latex_cleaner reads its input path as a zip when it ends in ".zip" and
# erases an existing "<input>_arXiv" folder, so it always runs on a copy of the
# main file's folder under this fixed name.
STAGING_NAME = "paper"

# arxiv_latex_cleaner pastes command names into a regex unescaped.
_COMMAND_NAME = re.compile(r"[A-Za-z@]+")

# Files arxiv_latex_cleaner reads as (strict UTF-8) text; mirrors its own patterns.
CLEANER_TEXT_FILES = re.compile(r".tex$|.tikz$")

# Bytes that are not valid UTF-8 are mapped one-to-one onto this private-use
# range while the cleaner runs, then restored. Unlike a Latin-1 round trip, these
# characters are never treated as whitespace or line breaks by the cleaner.
_PUA_BASE = 0xF700
_PUA_CHARS = re.compile("([\uf780-\uf7ff]+)")
# Ways a source can declare a non-UTF-8 input encoding.
_INPUTENC = re.compile(
    r"\\(?:usepackage|RequirePackage)\s*\[([^\]]*)\]\s*\{[^}]*\binputenc\b[^}]*\}"
    r"|\\inputencoding\s*\{([^}]*)\}"
)
_OTHER_ENCODING_DECL = re.compile(r"\\begin\s*\{CJK\*?\}|\\XeTeXinputencoding")


def _pua_errors(error):
    return chr(_PUA_BASE + error.object[error.start]), error.start + 1


codecs.register_error("paperready_pua", _pua_errors)

GRAPHICS_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg", ".tikz"]

_COMMENT = re.compile(r"(?<!\\)%.*")
_INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_GRAPHICS = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_BIB = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
# Local style/class/bibliography-style files: \usepackage{styles/mine}, etc.
_SUPPORT = {
    ".sty": re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}"),
    ".cls": re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}"),
    ".bst": re.compile(r"\\bibliographystyle\s*\{([^}]+)\}"),
}
# Images that ship with TeX distributions (the mwe package), never in a project.
_TEX_DISTRIBUTION_IMAGE = re.compile(r"example-(?:image|grid)(?:-[A-Za-z0-9]+)*")

# Figure types arxiv_latex_cleaner keeps when referenced without an extension.
_LOOSE_FIGURE_EXTS = {".png", ".jpg", ".jpeg", ".pdf"}
_GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^}]*\}\s*)+)\}")


class NoTexFilesError(CleanerError):
    pass


class MainFileNotFoundError(CleanerError):
    pass


class AmbiguousMainFileError(CleanerError):
    pass


class InvalidOptionsError(CleanerError):
    pass


class CleaningFailedError(CleanerError):
    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details


@dataclass
class CleanResult:
    zip_bytes: bytes
    main_file: str
    project_root: str
    missing_files: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # {relative path: size in bytes} of the paper folder before and after cleaning.
    input_files: dict = field(default_factory=dict)
    output_files: dict = field(default_factory=dict)
    # References the cleaner left out although they were uploaded.
    dropped_files: list = field(default_factory=list)
    missing_bbl: bool = False
    # Name of the .bbl the app generated with BibTeX, if any.
    generated_bbl: str = ""
    # Why the paper looks like an anonymous/line-numbered submission, if it does.
    review_version: str = ""
    # The paper's \\title, as plain text ("" if none was found).
    title: str = ""


def _encode_back(text):
    out = bytearray()
    for chunk in _PUA_CHARS.split(text):
        if chunk and _PUA_CHARS.fullmatch(chunk):
            out += bytes(ord(c) - _PUA_BASE for c in chunk)
        else:
            out += chunk.encode("utf-8")
    return bytes(out)


def _normalize_encodings(base):
    """Makes every file the cleaner reads valid UTF-8.

    Returns (restore, converted): `restore` lists files in an 8-bit encoding
    (Latin-1, Windows-1252, ...) whose original bytes must be put back after
    cleaning; `converted` lists UTF-16/32 files that were re-saved as UTF-8,
    since LaTeX cannot read those anyway.
    """
    restore, converted = [], []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or not CLEANER_TEXT_FILES.search(path.name):
            continue
        data = path.read_bytes()
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            path.write_text(data.decode("utf-32"), encoding="utf-8")
            converted.append(path)
            continue
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            path.write_text(data.decode("utf-16"), encoding="utf-8")
            converted.append(path)
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="paperready_pua")
            path.write_bytes(text.encode("utf-8"))
            restore.append(path)
    return restore, converted


def _restore_encodings(rel_paths, cleaned):
    for rel in rel_paths:
        out = cleaned / rel
        if out.is_file():
            out.write_bytes(_encode_back(out.read_text(encoding="utf-8")))


def _declares_encoding(text):
    """True if the source declares a non-UTF-8 input encoding."""
    if _OTHER_ENCODING_DECL.search(text):
        return True
    for m in _INPUTENC.finditer(text):
        options = (m.group(1) or m.group(2) or "").replace(" ", "").lower()
        if any(o and o not in ("utf8", "utf8x", "utf-8") for o in options.split(",")):
            return True
    return False


def _read_tex(path):
    return path.read_text(encoding="utf-8", errors="ignore")


def _strip_comments(text):
    return "\n".join(_COMMENT.sub("", line) for line in text.splitlines())


# Text LaTeX prints literally instead of running, e.g. usage examples in
# templates: \begin{verbatim}\bibliography{anthology,custom}\end{verbatim}.
_VERBATIM_ENV = re.compile(
    r"\\begin\s*\{(verbatim\*?|Verbatim\*?|lstlisting|minted|comment)\}.*?\\end\s*\{\1\}", re.DOTALL
)
_VERBATIM_INLINE = re.compile(r"\\(?:verb\*?|lstinline)([^\sA-Za-z{])(?:(?!\1).)*\1")


def _active_latex(text):
    """The LaTeX that actually runs: without comments and verbatim text."""
    text = _VERBATIM_ENV.sub("", _strip_comments(text))
    return _VERBATIM_INLINE.sub("", text)


def _is_main_candidate(path):
    text = _active_latex(_read_tex(path))
    return "\\documentclass" in text and "\\begin{document}" in text


def _rel(path, base):
    return path.relative_to(base).as_posix()


def _all_tex_files(base):
    return sorted(p for p in base.rglob("*.tex") if p.is_file() and not _is_ignored(p, base))


def _match_user_path(base, query):
    """Finds files/folders in `base` matching a user-supplied name or path."""
    query = query.strip().strip("/\\").replace("\\", "/")
    q_parts = PurePosixPath(query).parts
    matches = []
    for p in sorted(base.rglob("*")):
        if _is_ignored(p, base):
            continue
        parts = p.relative_to(base).parts
        if tuple(x.lower() for x in parts[-len(q_parts) :]) == tuple(x.lower() for x in q_parts):
            matches.append(p)
    return matches


def _in_previous_output(path, base):
    return any(p.endswith("_arXiv") for p in path.relative_to(base).parent.parts)


def _pick_main(candidates, base, scope_desc):
    # Ignore copies left over from an earlier arxiv_latex_cleaner run.
    fresh = [c for c in candidates if not _in_previous_output(c, base)]
    if fresh:
        candidates = fresh

    if len(candidates) == 1:
        return candidates[0]

    named_main = [c for c in candidates if c.name.lower() == DEFAULT_MAIN]
    if len(named_main) == 1:
        return named_main[0]
    if named_main:
        candidates = named_main

    # Prefer the shallowest file if it is unique at that depth.
    depth = min(len(c.relative_to(base).parts) for c in candidates)
    shallowest = [c for c in candidates if len(c.relative_to(base).parts) == depth]
    if len(shallowest) == 1:
        return shallowest[0]

    listing = "\n".join(f"  • {_rel(c, base)}" for c in candidates)
    raise AmbiguousMainFileError(
        f"Found several possible main .tex files {scope_desc}:\n{listing}\n"
        "Please type the one you want in “Main .tex file or folder”."
    )


def find_main_tex(base, main_hint=None):
    """Locates the main .tex file anywhere inside `base`.

    `main_hint` may be a file name (`paper.tex`, `paper`), a relative path
    (`src/paper.tex`) or a folder name (`my-paper`). Without a hint, the file
    called main.tex is preferred, then any file with \\documentclass and
    \\begin{document}.
    """
    all_tex = _all_tex_files(base)
    if not all_tex:
        upper = [p for p in base.rglob("*") if p.suffix.lower() == ".tex"]
        if upper:
            raise NoTexFilesError(
                f"“{_rel(upper[0], base)}” has an upper-case extension. "
                "arxiv_latex_cleaner only processes files ending in lower-case "
                "“.tex”; please rename your files and upload again."
            )
        raise NoTexFilesError(
            "No .tex files were found in the uploaded zip. "
            "Make sure you zipped the LaTeX source folder, not the compiled PDF."
        )

    if main_hint and main_hint.strip():
        hint = main_hint.strip()
        matches = _match_user_path(base, hint)
        if not matches and not hint.lower().endswith(".tex"):
            matches = _match_user_path(base, hint + ".tex")

        files = [m for m in matches if m.is_file() and m.suffix.lower() == ".tex"]
        folders = [m for m in matches if m.is_dir()]

        if files:
            if len(files) > 1:
                listing = "\n".join(f"  • {_rel(f, base)}" for f in files)
                raise AmbiguousMainFileError(
                    f"“{hint}” matches several files:\n{listing}\n"
                    "Please type the full path of the one you want."
                )
            return files[0]

        if folders:
            if len(folders) > 1:
                listing = "\n".join(f"  • {_rel(f, base)}/" for f in folders)
                raise AmbiguousMainFileError(
                    f"“{hint}” matches several folders:\n{listing}\n"
                    "Please type the full path of the one you want."
                )
            folder = folders[0]
            in_folder = _all_tex_files(folder)
            if not in_folder:
                raise NoTexFilesError(f"The folder “{_rel(folder, base)}” contains no .tex files.")
            candidates = [p for p in in_folder if _is_main_candidate(p)]
            if not candidates:
                raise MainFileNotFoundError(
                    f"No main .tex file (one with \\documentclass and "
                    f"\\begin{{document}}) was found in “{_rel(folder, base)}”."
                )
            return _pick_main(candidates, base, f"in “{_rel(folder, base)}”")

        available = "\n".join(f"  • {_rel(p, base)}" for p in all_tex[:20])
        more = "\n  …" if len(all_tex) > 20 else ""
        raise MainFileNotFoundError(
            f"Could not find a file or folder named “{hint}” in the zip.\n"
            f".tex files in your upload:\n{available}{more}"
        )

    candidates = [p for p in all_tex if _is_main_candidate(p)]
    if not candidates:
        named = [p for p in all_tex if p.name.lower() == DEFAULT_MAIN]
        if len(named) == 1:
            return named[0]
        available = "\n".join(f"  • {_rel(p, base)}" for p in all_tex[:20])
        more = "\n  …" if len(all_tex) > 20 else ""
        raise MainFileNotFoundError(
            "Could not find a main .tex file: no file contains both "
            "\\documentclass and \\begin{document}, and there is no main.tex.\n"
            f".tex files in your upload:\n{available}{more}\n"
            "Type the name of your main file in “Main .tex file or folder”."
        )
    return _pick_main(candidates, base, "in the zip")


def _split_args(raw):
    return [a.strip() for a in raw.split(",") if a.strip()]


@dataclass
class CleanerOptions:
    """Every arxiv_latex_cleaner option the app exposes, with upstream defaults.

    Name lists are free text ("todo, \\note"); see build_cleaner_args.
    """

    keep_bib: bool = False
    resize_images: bool = False
    im_size: int = 500
    compress_pdf: bool = False
    pdf_im_resolution: int = 500
    images_allowlist: str = ""
    convert_png_to_jpg: bool = False
    png_quality: int = 50
    png_size_threshold: float = 0.5
    commands_to_delete: str = ""
    commands_only_to_delete: str = ""
    environments_to_delete: str = ""
    if_exceptions: str = ""
    use_external_tikz: str = ""
    svg_inkscape: bool = False
    svg_inkscape_path: str = ""


# Options whose values upstream's --config merge would overwrite with argparse
# defaults, so values from an uploaded config are passed as flags instead.
_SCALAR_OPTIONS = {
    "keep_bib": bool,
    "resize_images": bool,
    "im_size": int,
    "compress_pdf": bool,
    "pdf_im_resolution": int,
    "convert_png_to_jpg": bool,
    "png_quality": int,
    "png_size_threshold": float,
    "use_external_tikz": str,
    "svg_inkscape": str,
}


def _relative_folder(raw, label):
    folder = raw.strip().replace("\\", "/").strip("/")
    if not folder:
        return ""
    if ".." in PurePosixPath(folder).parts:
        raise InvalidOptionsError(f"{label} must be a folder inside your project.")
    return folder


def build_cleaner_args(options):
    """Turns CleanerOptions into arxiv_latex_cleaner arguments.

    Returns (args, notes): notes are user-facing messages about input that was
    ignored. Raises InvalidOptionsError for input that cannot be used.
    """
    args, notes = [], []
    if options.keep_bib:
        args.append("--keep_bib")
    if options.resize_images:
        args += ["--resize_images", "--im_size", str(int(options.im_size))]
    if options.compress_pdf:
        args += ["--compress_pdf", "--pdf_im_resolution", str(int(options.pdf_im_resolution))]
    if options.convert_png_to_jpg:
        if not 0 <= options.png_quality <= 100:
            raise InvalidOptionsError("PNG → JPG quality must be between 0 and 100.")
        args += [
            "--convert_png_to_jpg",
            "--png_quality",
            str(int(options.png_quality)),
            "--png_size_threshold",
            str(float(options.png_size_threshold)),
        ]

    if options.images_allowlist.strip():
        try:
            allowlist = json.loads(options.images_allowlist)
        except json.JSONDecodeError as error:
            raise InvalidOptionsError(f"Image allowlist is not valid JSON: {error}.") from None
        if not isinstance(allowlist, dict) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in allowlist.values()
        ):
            raise InvalidOptionsError(
                'Image allowlist must map image paths to numbers, e.g. {"figs/a.png": 2000}.'
            )
        args += ["--images_allowlist", json.dumps(allowlist)]

    for flag, raw, label, example in (
        ("--commands_to_delete", options.commands_to_delete, "commands to delete", "`todo` for \\todo{...}"),
        (
            "--commands_only_to_delete",
            options.commands_only_to_delete,
            "commands to unwrap",
            "`hl` for \\hl{...}",
        ),
        (
            "--environments_to_delete",
            options.environments_to_delete,
            "environments to delete",
            "`note` for \\begin{note}",
        ),
    ):
        names, rejected = parse_commands(raw)
        if rejected:
            notes.append(
                f"Ignored invalid {label}: "
                + ", ".join(f"`{r}`" for r in rejected)
                + f". Use letters only, e.g. {example}."
            )
        if names:
            args += [flag, *names]

    names, rejected = parse_commands(options.if_exceptions)
    rejected += [n for n in names if not n.startswith("if")]
    names = [n for n in names if n.startswith("if")]
    if rejected:
        notes.append(
            "Ignored invalid \\if exceptions: "
            + ", ".join(f"`{r}`" for r in rejected)
            + ". They must start with “if”, e.g. `ifdraft`."
        )
    if names:
        args += ["--if_exceptions", *names]

    tikz = _relative_folder(options.use_external_tikz, "External TikZ folder")
    if tikz:
        args += ["--use_external_tikz", tikz]
    if options.svg_inkscape:
        args.append("--svg_inkscape")
        path = _relative_folder(options.svg_inkscape_path, "Inkscape SVG folder")
        if path:
            args.append(path)
    return args, notes


def _load_config(config_bytes):
    try:
        config = yaml.safe_load(config_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise InvalidOptionsError(f"The config file is not valid YAML: {error}") from None
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise InvalidOptionsError("The config file must be a YAML mapping of option names.")
    patterns = config.get("patterns_and_insertions") or []
    required = ("pattern", "insertion", "description")
    if not isinstance(patterns, list) or not all(
        isinstance(p, dict) and all(isinstance(p.get(k), str) for k in required) for p in patterns
    ):
        raise InvalidOptionsError(
            "Each entry in patterns_and_insertions needs “pattern”, “insertion” "
            "and “description” (see arxiv_latex_cleaner's cleaner_config.yaml)."
        )
    return config


def _config_scalar_args(config, args):
    """Flags for scalar config values the UI did not set (see _SCALAR_OPTIONS)."""
    extra = []
    for key, kind in _SCALAR_OPTIONS.items():
        value = config.get(key)
        if value is None or f"--{key}" in args or value is False:
            continue
        if kind is bool or (key == "svg_inkscape" and value is True):
            extra.append(f"--{key}")
        elif kind is str:
            extra += [f"--{key}", _relative_folder(str(value), f"Config “{key}”")]
        else:
            extra += [f"--{key}", str(value)]
    return extra


def parse_commands(raw):
    """Turns user input like "\\todo, note{}" into (["todo", "note"], rejected)."""
    valid, rejected = [], []
    for token in raw.replace(",", " ").split():
        name = token.lstrip("\\").rstrip("{}")
        if _COMMAND_NAME.fullmatch(name):
            valid.append(name)
        else:
            rejected.append(token)
    return valid, rejected


def _is_file(path):
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _inside(path, root):
    """True if `path` really is under `root`, after "..", links and absolute
    paths are resolved: references like \\input{/etc/hostname} or
    \\input{../../x} must not reach files outside the upload."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _is_literal(name):
    # Skip references built from macros (e.g. \input{\dir/intro}); we can't resolve them.
    return name and "\\" not in name and "#" not in name


def _resolve(root, kind, name, graphic_dirs=("",)):
    """Returns the file a reference points to under `root`, or None (also for
    files outside `root`, which arXiv never gets)."""
    if kind == "input":
        candidates = [name] if PurePosixPath(name).suffix else [name + ".tex"]
        candidates.append(name)
    elif kind == "graphics":
        candidates = [d + name + ext for d in graphic_dirs for ext in ["", *GRAPHICS_EXTS]]
    else:  # ".bib", ".sty", ".cls", ".bst"
        candidates = [name, name + kind]
    for candidate in candidates:
        # Plain relative names can't leave `root` (symlinks are removed on upload);
        # only absolute ones and "../" need the slower check.
        plain = ":" not in candidate and not candidate.startswith("/") and ".." not in candidate
        if _is_file(root / candidate) and (plain or _inside(root / candidate, root)):
            return root / candidate
    return None


def _scan_references(root, main_tex):
    """Follows the paper from `main_tex` and lists what it references.

    Returns (refs, uses_bib), where refs are (source file, kind, name,
    graphic dirs) tuples and kind is "input", "graphics", ".bib", ".sty",
    ".cls" or ".bst". Paths resolve relative to `root` (the main file's
    folder), as in LaTeX and on arXiv.
    """
    refs = []
    uses_bib = False
    seen = set()
    graphic_dirs = [""]
    queue = [main_tex]

    while queue:
        tex = queue.pop(0)
        if tex in seen:
            continue
        seen.add(tex)
        text = _active_latex(_read_tex(tex))
        where = _rel(tex, root)

        for m in _GRAPHICSPATH.finditer(text):
            graphic_dirs += re.findall(r"\{([^}]*)\}", m.group(1))

        for m in _INPUT.finditer(text):
            name = m.group(1).strip()
            if _is_literal(name):
                refs.append((where, "input", name, ()))
                target = _resolve(root, "input", name)
                if target:
                    queue.append(target)

        for m in _GRAPHICS.finditer(text):
            name = m.group(1).strip()
            if _is_literal(name):
                refs.append((where, "graphics", name, tuple(graphic_dirs)))

        for m in _BIB.finditer(text):
            uses_bib = True
            refs += [(where, ".bib", n, ()) for n in _split_args(m.group(1))]

        for ext, pattern in _SUPPORT.items():
            for m in pattern.finditer(text):
                refs += [(where, ext, n, ()) for n in _split_args(m.group(1)) if _is_literal(n)]

    return refs, uses_bib


# Every way a paper loads its files: \input{…}, \include{…}, \subfile{…}, TeX's
# brace-less \input, and the import package, whose \import{dir}{file} is
# relative to the main file and whose \subimport{dir}{file} is relative to the
# file that uses it.
_LOAD = re.compile(
    r"\\(?:input|include|subfile)\s*\{(?P<braced>[^}]+)\}"
    r"|\\input\s+(?P<bare>[^\s{}\\%]+)"
    r"|\\(?P<sub>sub)?(?:import|includefrom|inputfrom)\*?\s*\{(?P<dir>[^}]*)\}\s*\{(?P<file>[^}]+)\}"
)
# Deeper nesting than this is not followed (real papers nest a few levels).
_LOAD_DEPTH = 20


def _paper_text(root, main_tex):
    """The active LaTeX of the paper rooted at `main_tex`, with each file it
    loads inlined where it is loaded (once), i.e. in the order LaTeX reads it.
    Files are told apart by their resolved path: "s1/../a.tex" is "a.tex"."""
    base = root.resolve()
    seen = set()
    targets = {}  # reference -> resolved file or None, as a paper may load a file many times

    def target_of(name):
        if name not in targets:
            target = _resolve(root, "input", name) if _is_literal(name) else None
            targets[name] = target.resolve() if target is not None else None
        return targets[name]

    def read(tex, depth):
        tex = tex.resolve()
        seen.add(tex)
        here = PurePosixPath(tex.parent.relative_to(base).as_posix())

        def load(m):
            if m.group("file") is not None:
                folder = PurePosixPath(m.group("dir").strip())
                name = ((here / folder) if m.group("sub") else folder) / m.group("file").strip()
                name = name.as_posix()
            else:
                name = (m.group("braced") or m.group("bare")).strip()
            target = target_of(name)
            if target is None or target in seen or depth >= _LOAD_DEPTH:
                return m.group(0)
            return f"{m.group(0)}\n{read(target, depth + 1)}\n"

        return _LOAD.sub(load, _active_latex(_read_tex(tex)))

    return read(main_tex, 0)


def find_missing_files(root, main_tex, check_bib=True):
    """Returns (missing, uses_bib) for the paper rooted at `main_tex`.

    `missing` lists (source file, reference) pairs for inputs, figures and
    (with `check_bib`) .bib files the paper needs that do not exist. Style and
    class references are not checked: most come from the TeX distribution.
    """
    kinds = {"input", "graphics"} | ({".bib"} if check_bib else set())
    refs, uses_bib = _scan_references(root, main_tex)
    missing = [
        (where, name)
        for where, kind, name, dirs in refs
        if kind in kinds
        and not _resolve(root, kind, name, dirs)
        and not (kind == "graphics" and _TEX_DISTRIBUTION_IMAGE.fullmatch(PurePosixPath(name).stem))
    ]
    return missing, uses_bib


def _dropped_reason(kind, name, original):
    if (
        kind == "graphics"
        and not PurePosixPath(name).suffix
        and original.suffix.lower() not in _LOOSE_FIGURE_EXTS
    ):
        return f"it is referenced without its extension; write “{name}{original.suffix}”"
    if kind in _SUPPORT and "/" in name and not PurePosixPath(name).suffix:
        return (
            f"files in subfolders need the extension in the reference; write "
            f"“{name}{kind}” or move the file next to the main file"
        )
    return (
        "arxiv_latex_cleaner mishandles names with brackets, spaces or other "
        "special characters and folders ending in “git”; rename it"
    )


def find_dropped_files(original_root, cleaned_root, main_name):
    """Lists files the cleaned paper still references that the cleaner left
    out although they were uploaded, as (reference, reason) pairs."""
    refs, _ = _scan_references(cleaned_root, cleaned_root / main_name)
    dropped = {}
    for _, kind, name, dirs in refs:
        if kind == ".bib" or _resolve(cleaned_root, kind, name, dirs):
            continue
        original = _resolve(original_root, kind, name, dirs)
        if original and name not in dropped:
            dropped[name] = _dropped_reason(kind, name, original)
    return sorted(dropped.items())


def _package_options(text, package_pattern):
    """Yields (options, name) for each package matching package_pattern that is
    loaded with \\usepackage, including lists like \\usepackage{a,b}."""
    package = re.compile(package_pattern)
    for m in re.finditer(r"\\usepackage\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}", text):
        options = [o.strip() for o in (m.group(1) or "").split(",") if o.strip()]
        for name in (n.strip() for n in m.group(2).split(",")):
            if package.fullmatch(name):
                yield options, name


# Braces and escapes: "\}" is an escaped brace, but the "}" in "\\}" (a line
# break, then a brace) is not, so escapes are read as two-character tokens.
_BRACE_TOKEN = re.compile(r"\\.|[{}]", re.DOTALL)


def _braced(text, start):
    """Returns the contents of the {...} group opening at text[start]."""
    depth = 0
    for m in _BRACE_TOKEN.finditer(text, start):
        if m.group() == "{":
            depth += 1
        elif m.group() == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : m.start()]
    return text[start + 1 :]


# Zero-argument macros, e.g. \newcommand{\name}{\textsc{Self-Instruct}} or
# \def\confName{CVPR}, which papers often use in their \title.
_MACRO_DEF = re.compile(
    r"\\(?:(?:re)?newcommand|providecommand)\*?\s*\{?\s*\\([A-Za-z@]+)\s*\}?\s*(?=\{)"
    r"|\\def\s*\\([A-Za-z@]+)\s*(?=\{)"
)
_LOGOS = {"LaTeX": "LaTeX", "LaTeXe": "LaTeX2e", "TeX": "TeX", "BibTeX": "BibTeX"}
# Commands in a title whose arguments are layout, not title text, with how many
# {...} arguments they take, e.g. \vspace*{-0.5in} or \includegraphics[...]{logo}.
_TITLE_LAYOUT = {
    "includegraphics": 1,
    "vspace": 1,
    "hspace": 1,
    "raisebox": 1,
    "label": 1,
    "thanks": 1,
    "footnote": 1,
    "footnotemark": 0,
    "rule": 2,
    "phantom": 1,
    "hphantom": 1,
    "vphantom": 1,
    # Only the last argument is printed: \textcolor{orange}{Sys}, \href{url}{Sys}.
    "textcolor": 1,
    "color": 1,
    "colorbox": 1,
    "href": 1,
    "hyperlink": 1,
    "scalebox": 1,
    "resizebox": 2,
    "fontsize": 2,
}
# Limits that keep hostile uploads cheap; real titles and title macros are far
# shorter. Expansion may add _TITLE_EXPANSION characters per title character (plus
# a fixed allowance), so a self-referencing \def cannot run away.
_TITLE_MAX = 2000
_MACRO_BODY_MAX = 1000
_TITLE_EXPANSION = 10


class _Macros:
    """Zero-argument macros defined in some LaTeX sources (later definitions
    win); a body is only read when the title uses the macro."""

    def __init__(self, *sources):
        self.defined = {
            m.group(1) or m.group(2): (text, m.end()) for text in sources for m in _MACRO_DEF.finditer(text)
        }
        self.bodies = {}

    def get(self, name):
        if name not in self.bodies:
            text, start = self.defined.get(name, (None, 0))
            self.bodies[name] = (
                _braced(text[start : start + _MACRO_BODY_MAX], 0)
                if text is not None
                else _LOGOS.get(name) or _greek_letter(name)
            )
        return self.bodies[name]


def _greek_letter(name):
    """The letter a PDF shows for \\mu, \\Gamma, \\varepsilon or \\ell (as in
    "Scaling μP Transfer"), or None for other commands."""
    if name == "ell":
        return "ℓ"
    letter = name[3:] if name.startswith("var") else name
    case = "CAPITAL" if letter[:1].isupper() else "SMALL"
    unicode_name = {"LAMBDA": "LAMDA"}.get(letter.upper(), letter.upper())  # Unicode's spelling
    try:
        return unicodedata.lookup(f"GREEK {case} LETTER {unicode_name}")
    except KeyError:
        return None


def _drop_commands(text, commands):
    """Removes each \\command[...]{...} in `commands` ({name: number of braced
    arguments}) together with its arguments, which may contain braces."""
    pattern = re.compile(r"\\(" + "|".join(commands) + r")(?![A-Za-z@])\*?\s*(?:\[[^\]]*\]\s*)?")
    kept, pos = [], 0
    while m := pattern.search(text, pos):
        kept.append(text[pos : m.start()])
        pos = m.end()
        for _ in range(commands[m.group(1)]):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] == "{":
                pos += len(_braced(text, pos)) + 2
    return "".join(kept) + text[pos:]


def extract_title(text, definitions=""):
    """The paper's \\title{...} as plain text, e.g. for comparing with a PDF.

    `definitions` is more LaTeX of the paper (e.g. its \\input files) whose
    zero-argument macros are expanded in the title, as LaTeX would."""
    active = _active_latex(text)
    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", active)
    if not m:
        return ""
    # `definitions` comes last: in clean_zip it is the whole paper in reading
    # order, so the last (re)definition LaTeX reads wins.
    macros = _Macros(active, _active_latex(definitions))
    breaks = re.compile(r"\\\\(?:\[[^\]]*\])?|~")  # line breaks, ties
    raw = _braced(active[m.end() - 1 : m.end() - 1 + _TITLE_MAX], 0)
    title = breaks.sub(" ", _drop_commands(raw, _TITLE_LAYOUT))
    budget = _TITLE_EXPANSION * len(title) + 1000

    def expand(command):
        nonlocal budget
        body = macros.get(command.group(1))
        if body is None or len(body) > budget:
            return command.group(0)
        budget -= len(body)
        return body

    for _ in range(3):  # macros may use other macros
        expanded = re.sub(r"\\([A-Za-z@]+)(?:\s*\{\})?", expand, title)
        if expanded == title:
            break
        title = _drop_commands(expanded, _TITLE_LAYOUT)
    title = breaks.sub(" ", title)
    title = re.sub(r"\\[ ,;:!]|\\(?:quad|qquad|hfill|enspace|enskip|newline|linebreak)\b", " ", title)
    title = re.sub(r"\\[A-Za-z@]+\*?", "", title)  # commands like \\textbf or \\xspace
    title = re.sub(r"(?<!\\)[{}$_^]", "", title)  # groups, math, sub- and superscripts
    title = re.sub(r"\\([{}$_^&%#])", r"\1", title)  # escaped characters, e.g. \&
    return " ".join(title.split())


# Since 2021, *ACL style files are final by default and take a [review] option
# (e.g. acl2023.sty, emnlp2021.sty); older ones need \aclfinalcopy.
_REVIEW_OPTION = re.compile(r"\\DeclareOption\s*\{review\}")


def detect_review_version(text, styles=None):
    """Returns why `text` (the cleaned LaTeX of the paper) is an anonymous or
    line-numbered submission rather than a final version, with the fix, or ""
    if it is not. `styles` maps uploaded style names (lower case, no .sty) to
    their contents, which tells how a year-named *ACL style is switched."""
    styles = styles or {}
    for options, _ in _package_options(text, "acl"):
        if "review" in options:
            return (
                "it uses the ACL template's review option (anonymous, with line "
                "numbers). Change \\usepackage[review]{acl} to "
                "\\usepackage[preprint]{acl} (or [final])"
            )
    for options, name in _package_options(text, r"(?i:(?:acl|naacl|eacl|emnlp|aacl|coling)\d{4})"):
        if _REVIEW_OPTION.search(styles.get(name.lower(), "")):
            if "review" in options:
                return (
                    f"it uses the {name} template's review option (anonymous, with line "
                    f"numbers). Change \\usepackage[review]{{{name}}} to \\usepackage{{{name}}}"
                )
            continue
        if "\\aclfinalcopy" not in text:
            return (
                f"it uses the {name} template without \\aclfinalcopy, so authors are "
                "hidden. Add \\aclfinalcopy to the preamble"
            )
    for options, name in _package_options(text, r"neurips_\d{4}"):
        if not {"final", "preprint"} & set(options):
            return (
                f"it uses the {name} template's submission mode (anonymous, with "
                f"line numbers). Change it to \\usepackage[preprint]{{{name}}} (or [final])"
            )
    for options, name in _package_options(text, r"icml\d{4}"):
        if "accepted" not in options:
            return (
                f"it uses the {name} template's submission mode (anonymous). "
                f"Change it to \\usepackage[accepted]{{{name}}}"
            )
    for _, name in _package_options(text, r"iclr\d{4}_conference"):
        if "\\iclrfinalcopy" not in text:
            return (
                f"it uses the {name} template without \\iclrfinalcopy, so authors are "
                "hidden. Add \\iclrfinalcopy to the preamble"
            )
    for options, name in _package_options(text, r"cvpr|iccv|wacv|eccv"):
        if "review" in options:
            return (
                f"it uses the {name} template's review option (anonymous, with line "
                f"numbers). Change it to \\usepackage[final]{{{name}}}"
            )
    if re.search(r"\\linenumbers\b", text):
        return "it turns on line numbers (\\linenumbers). Remove that line"
    return ""


_CITE = re.compile(r"\\(?:no)?cite[a-zA-Z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
_BIBLATEX = re.compile(r"\\(?:addbibresource|printbibliography)\b")
_BIBTEX_ERRORS = re.compile(r"\(There (?:was|were) \d+ error messages?\)")


def _citations(root, tex, keys, seen):
    """Adds cited keys to `keys` in order of appearance, following \\input."""
    if tex in seen:
        return
    seen.add(tex)
    text = _active_latex(_read_tex(tex))
    for m in re.finditer(f"{_INPUT.pattern}|{_CITE.pattern}", text):
        if m.group(1) is not None:
            target = _resolve(root, "input", m.group(1).strip())
            if target:
                _citations(root, target, keys, seen)
        else:
            for key in _split_args(m.group(2)):
                if key not in keys:
                    keys.append(key)


def generate_bbl(original_root, cleaned_root, main_name):
    """Runs BibTeX on the cleaned paper and writes <main>.bbl into it.

    Citations come from the cleaned sources (so ones only in removed comments
    are left out); .bib and .bst files come from the upload. Returns
    (problem, unknown_keys, missing_bibs): problem is None on success,
    otherwise a short reason; unknown_keys are citations BibTeX found in no
    .bib file; missing_bibs are listed .bib files that are not in the upload
    (like BibTeX, it goes on with the others).
    """
    main_tex = cleaned_root / main_name
    text = "\n".join(_active_latex(_read_tex(p)) for p in cleaned_root.rglob("*.tex"))
    if _BIBLATEX.search(text):
        return "biblatex needs Biber, which this app cannot run", [], []
    bibtex = shutil.which("bibtex")
    if not bibtex:
        return "BibTeX is not installed on this server", [], []

    refs, _ = _scan_references(cleaned_root, main_tex)
    bib_names = [n for _, kind, n, _ in refs if kind == ".bib"]
    styles = [n for _, kind, n, _ in refs if kind == ".bst"]
    if not styles:
        # Templates such as ACL's set the style inside their .sty/.cls.
        for support in sorted(cleaned_root.rglob("*")):
            if support.suffix in (".sty", ".cls") and support.is_file():
                text = _active_latex(_read_tex(support))
                for m in _SUPPORT[".bst"].finditer(text):
                    styles += _split_args(m.group(1))
    if not styles:
        return "no \\bibliographystyle was found", [], []

    keys = []
    _citations(cleaned_root, main_tex, keys, set())
    if not keys:
        return "the paper cites nothing", [], []

    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        databases, missing_bibs = [], []
        for i, name in enumerate(bib_names):
            source = _resolve(original_root, ".bib", name) or _resolve(
                original_root, ".bib", PurePosixPath(name).name
            )
            if not source:
                missing_bibs.append(name)
                continue
            shutil.copy(source, work / f"bib{i}.bib")
            databases.append(f"bib{i}")
        if not databases:
            names = ", ".join(f"“{n}.bib”" for n in missing_bibs)
            return f"{names} {'is' if len(missing_bibs) == 1 else 'are'} not in the zip", [], []
        style = styles[-1]
        local_style = _resolve(original_root, ".bst", style)
        if local_style:
            shutil.copy(local_style, work / "style.bst")
            style = "style"
        aux = [f"\\citation{{{key}}}" for key in keys]
        aux += [f"\\bibstyle{{{style}}}", f"\\bibdata{{{','.join(databases)}}}"]
        (work / "paper.aux").write_text("\n".join(aux) + "\n", encoding="utf-8")
        try:
            run = subprocess.run([bibtex, "paper"], cwd=work, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "BibTeX took too long", [], []
        bbl = work / "paper.bbl"
        # TeX Live's BibTeX exits with 2 after errors, MiKTeX's with 1; both count them.
        if run.returncode > 1 or _BIBTEX_ERRORS.search(run.stdout) or not bbl.is_file():
            if "couldn't open style file" in run.stdout:
                return f"the style “{styles[-1]}.bst” is not in the zip", [], []
            last = (run.stdout.strip().splitlines() or ["unknown error"])[-1]
            return f"BibTeX failed: {last}", [], []
        shutil.copy(bbl, main_tex.with_suffix(".bbl"))
        unknown = re.findall(r'didn\'t find a database entry for "([^"]+)"', run.stdout)
    return None, unknown, missing_bibs


def clean_zip(zip_bytes, extra_args=None, main_hint=None, config_bytes=None, make_bbl=True):
    """Cleans an uploaded project. `extra_args` are arxiv_latex_cleaner flags
    (see build_cleaner_args); `config_bytes` is an optional cleaner_config.yaml;
    `make_bbl` runs BibTeX when the paper needs a .bbl that was not uploaded."""
    extra_args = list(extra_args or [])
    config = _load_config(config_bytes) if config_bytes else None
    if config is not None:
        extra_args += _config_scalar_args(config, extra_args)
    if "--compress_pdf" in extra_args and not shutil.which("gs"):
        raise InvalidOptionsError(
            "“Compress PDF figures” needs Ghostscript, which is not installed on "
            "this server. Untick it and try again."
        )

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        src.mkdir()

        _safe_extract(zip_bytes, src)
        _remove_junk(src)
        restore, converted = _normalize_encodings(src)
        main_tex = find_main_tex(src, main_hint)
        root = main_tex.parent
        warnings = []

        for path in (p for p in converted if root in p.parents):
            warnings.append(
                f"“{_rel(path, src)}” was saved as UTF-16/UTF-32, which LaTeX "
                "cannot read; it was converted to UTF-8."
            )
        in_root = [p for p in restore if root in p.parents]
        sources = [p for p in root.rglob("*") if p.suffix in (".tex", ".sty", ".cls") and p.is_file()]
        if in_root and not any(_declares_encoding(_active_latex(_read_tex(p))) for p in sources):
            names = ", ".join(f"“{_rel(p, src)}”" for p in in_root)
            warnings.append(
                f"{names} is not valid UTF-8 and your sources do not declare an "
                "input encoding. The original bytes were kept, but arXiv's LaTeX "
                "assumes UTF-8, so non-ASCII characters may fail to compile. "
                "Re-save the file as UTF-8, or declare its encoding, e.g. "
                "\\usepackage[latin1]{inputenc} (Western European) or "
                "\\usepackage[cp1251]{inputenc} (Cyrillic)."
            )

        # The cleaner treats .tex files in its input root as entry points, so it
        # must run on the folder that holds the main file.
        outside = [p for p in _all_tex_files(src) if root != p.parent and root not in p.parents]
        if outside:
            warnings.append(
                f"{len(outside)} .tex file(s) outside “{_rel(root, src) or '.'}” "
                "were ignored because they are not inside the main file's folder."
            )

        wrong_case = [p for p in root.rglob("*") if p.suffix.lower() == ".tex" and p.suffix != ".tex"]
        if wrong_case:
            names = ", ".join(f"“{_rel(p, src)}”" for p in wrong_case[:5])
            warnings.append(
                f"{names}: arxiv_latex_cleaner only cleans files ending in "
                "lower-case “.tex”, so comments in these files were NOT removed. "
                "Rename them to .tex and update the references."
            )

        # .bib files are not checked: arXiv never runs BibTeX, it only needs the
        # .bbl (warned about below), so a .bib path cannot break the build.
        missing, uses_bib = find_missing_files(root, main_tex, check_bib=False)
        bbl = main_tex.with_suffix(".bbl")
        missing_bbl = uses_bib and not bbl.is_file()

        main_rel = _rel(main_tex, src)
        root_rel = _rel(root, src) or "."
        restore_rel = [p.relative_to(root) for p in restore if root in p.parents]
        staged = Path(tmp) / STAGING_NAME
        shutil.move(str(root), str(staged))
        input_files = _file_sizes(staged)

        for flag, label in (("--use_external_tikz", "External TikZ"), ("--svg_inkscape", "Inkscape SVG")):
            if flag in extra_args:
                i = extra_args.index(flag) + 1
                folder = (
                    extra_args[i]
                    if i < len(extra_args) and not extra_args[i].startswith("--")
                    else "svg-inkscape"
                )
                if not (staged / folder).is_dir():
                    warnings.append(
                        f"{label} folder “{folder}” was not found next to "
                        f"{main_tex.name}, so that option had no effect."
                    )

        cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(staged), *extra_args]
        if config is not None:
            config_path = Path(tmp) / "cleaner_config.yaml"
            config_path.write_bytes(config_bytes)
            cmd += ["--config", str(config_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            last_line = details.splitlines()[-1] if details else "unknown error"
            raise CleaningFailedError(
                f"arxiv_latex_cleaner could not process your project ({last_line}).",
                details,
            )

        cleaned = Path(tmp) / f"{STAGING_NAME}_arXiv"
        if not cleaned.exists():
            raise CleaningFailedError("Cleaned output folder was not created.")

        if not (cleaned / main_tex.name).is_file():
            raise CleaningFailedError(
                f"The cleaner did not keep {main_tex.name}; the output would be unusable on arXiv."
            )

        _restore_encodings(restore_rel, cleaned)

        # Re-check the cleaned sources: anything they still reference that was
        # uploaded but is absent from the output was dropped by the cleaner.
        generated_bbl = ""
        if missing_bbl:
            problem, unknown, missing_bibs = (
                generate_bbl(staged, cleaned, main_tex.name)
                if make_bbl
                else ("generating it is turned off", [], [])
            )
            if problem is None:
                generated_bbl = bbl.name
                missing_bbl = False
                if missing_bibs:
                    warnings.append(
                        f"{bbl.name} was generated without "
                        + ", ".join(f"“{n}.bib”" for n in missing_bibs)
                        + ", which is not in the zip. Citations only found there will show as “?”."
                    )
                if unknown:
                    warnings.append(
                        "BibTeX found no entry for "
                        + ", ".join(f"“{k}”" for k in unknown[:10])
                        + "; these citations will show as “?”. Add them to your .bib."
                    )
            else:
                warnings.append(
                    f"Your paper uses a .bib bibliography but “{bbl.name}” is not "
                    f"in the zip, and it could not be generated ({problem}). arXiv "
                    "does not run BibTeX/Biber, so references will show as “?”. "
                    "Compile locally (on Overleaf: Logs and output files → Other "
                    f"logs and files) and add {bbl.name} next to {main_tex.name}."
                )

        # Only the paper itself: other .tex files next to it (e.g. the ACL
        # template's acl_lualatex.tex) are separate documents.
        paper = _paper_text(cleaned, cleaned / main_tex.name)
        # \usepackage{name} loads name.sty from the main file's folder.
        styles = {p.stem.lower(): _active_latex(_read_tex(p)) for p in cleaned.glob("*.sty") if p.is_file()}
        review_version = detect_review_version(paper, styles)
        if review_version:
            warnings.append(f"This looks like a submission version, not a final one: {review_version}.")

        dropped = find_dropped_files(staged, cleaned, main_tex.name)
        for ref, reason in dropped[:10]:
            warnings.append(f"arxiv_latex_cleaner left out “{ref}”, which your paper still uses: {reason}.")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
            for file in sorted(cleaned.rglob("*")):
                if file.is_file():
                    out.write(file, file.relative_to(cleaned))

        if buffer.tell() > ARXIV_SIZE_LIMIT:
            warnings.append(
                f"The cleaned zip is {buffer.tell() / 1024 / 1024:.0f} MB, above "
                "arXiv's 50 MB limit. Try “Resize images” in the cleaning options."
            )

        return CleanResult(
            zip_bytes=buffer.getvalue(),
            main_file=main_rel,
            project_root=root_rel,
            missing_files=missing,
            warnings=warnings,
            input_files=input_files,
            output_files=_file_sizes(cleaned),
            dropped_files=[ref for ref, _ in dropped],
            missing_bbl=missing_bbl,
            generated_bbl=generated_bbl,
            review_version=review_version,
            title=extract_title(_read_tex(cleaned / main_tex.name), paper),
        )
