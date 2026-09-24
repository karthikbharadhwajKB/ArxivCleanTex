import codecs
import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_MAIN = "main.tex"

# arXiv's upload limit (see the arxiv_latex_cleaner README).
ARXIV_SIZE_LIMIT = 50 * 1024 * 1024

# arxiv_latex_cleaner reads its input path as a zip when it ends in ".zip" and
# erases an existing "<input>_arXiv" folder, so it always runs on a copy of the
# main file's folder under this fixed name.
STAGING_NAME = "paper"

# arxiv_latex_cleaner pastes command names into a regex unescaped.
_COMMAND_NAME = re.compile(r"[A-Za-z@]+")

# Junk that zip tools (macOS Finder, Windows, editors) add to archives.
IGNORED_PARTS = {"__MACOSX", ".git", ".DS_Store", "Thumbs.db"}
JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

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


codecs.register_error("arxivcleantex_pua", _pua_errors)

GRAPHICS_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg", ".tikz"]

_COMMENT = re.compile(r"(?<!\\)%.*")
_INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_GRAPHICS = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_BIB = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^}]*\}\s*)+)\}")


class CleanerError(Exception):
    """Base class for errors that are shown to the user as-is."""


class InvalidZipError(CleanerError):
    pass


class NoTexFilesError(CleanerError):
    pass


class MainFileNotFoundError(CleanerError):
    pass


class AmbiguousMainFileError(CleanerError):
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


def _is_ignored(path, base):
    parts = path.relative_to(base).parts
    return any(p in IGNORED_PARTS or p.startswith("._") for p in parts)


def _entry_name(info):
    """Returns the entry's path with "/" separators and properly decoded."""
    name = info.filename
    if not info.flag_bits & 0x800:
        # Without the UTF-8 flag, zipfile decodes names as cp437, but macOS and
        # the `zip` CLI write UTF-8 there anyway.
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    # Some Windows tools write "\\" as the separator; it is never valid in a
    # file name on Windows, so it is always a separator.
    return name.replace("\\", "/")


def _safe_extract(zip_bytes, dest):
    dest_resolved = dest.resolve()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError):
        raise InvalidZipError(
            "The uploaded file is not a valid .zip archive (it may be corrupted "
            "or incomplete). Please re-create the zip and try again."
        )
    with zf:
        for info in zf.infolist():
            name = _entry_name(info)
            target = (dest / name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise InvalidZipError(f"Unsafe path in zip: {name}")
            if info.flag_bits & 0x1:
                raise InvalidZipError(
                    "The zip is password-protected. Please upload an unencrypted zip."
                )
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            try:
                data = zf.read(info)
            except NotImplementedError:
                raise InvalidZipError(
                    f"“{name}” uses a compression method this app cannot read. "
                    "Please re-create the zip with standard (Deflate) compression, "
                    "e.g. with your system's built-in “Compress” option."
                )
            except RuntimeError:
                raise InvalidZipError(
                    "The zip is password-protected. Please upload an unencrypted zip."
                )
            except (zipfile.BadZipFile, EOFError, OSError, ValueError, zlib.error) as error:
                raise InvalidZipError(
                    f"The zip is corrupted: “{name}” could not be read ({error}). "
                    "Please re-create the zip and try again."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _remove_junk(base):
    """Deletes OS metadata (e.g. macOS `__MACOSX/._main.tex`) that is not text."""
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir() and path.name in JUNK_DIRS:
            shutil.rmtree(path)
        elif path.is_file() and (
            path.name in JUNK_FILES or path.name.startswith("._")
        ):
            path.unlink()


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
            text = data.decode("utf-8", errors="arxivcleantex_pua")
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


def _is_main_candidate(path):
    text = _strip_comments(_read_tex(path))
    return "\\documentclass" in text and "\\begin{document}" in text


def _rel(path, base):
    return path.relative_to(base).as_posix()


def _all_tex_files(base):
    return sorted(
        p for p in base.rglob("*.tex") if p.is_file() and not _is_ignored(p, base)
    )


def _match_user_path(base, query):
    """Finds files/folders in `base` matching a user-supplied name or path."""
    query = query.strip().strip("/\\").replace("\\", "/")
    q_parts = PurePosixPath(query).parts
    matches = []
    for p in sorted(base.rglob("*")):
        if _is_ignored(p, base):
            continue
        parts = p.relative_to(base).parts
        if tuple(x.lower() for x in parts[-len(q_parts):]) == tuple(
            x.lower() for x in q_parts
        ):
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
                raise NoTexFilesError(
                    f"The folder “{_rel(folder, base)}” contains no .tex files."
                )
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


def _exists_with_exts(root, name, exts):
    if _is_file(root / name):
        return True
    return any(_is_file(root / (name + ext)) for ext in exts)


def _is_literal(name):
    # Skip references built from macros (e.g. \input{\dir/intro}); we can't resolve them.
    return name and "\\" not in name and "#" not in name


def find_missing_files(root, main_tex, check_bib=True):
    """Returns (missing, uses_bib) for the paper rooted at `main_tex`.

    `missing` lists (source file, reference) pairs for files the paper needs
    but that do not exist. It follows \\input/\\include from the main file and
    checks \\includegraphics and bibliography references, resolved relative to
    `root` (the main file's folder), which is how LaTeX (and arXiv) resolve
    them. `uses_bib` is True when a BibTeX/biblatex bibliography is used.
    """
    missing = []
    uses_bib = False
    seen = set()
    graphic_dirs = [""]
    queue = [main_tex]

    while queue:
        tex = queue.pop(0)
        if tex in seen:
            continue
        seen.add(tex)
        text = _strip_comments(_read_tex(tex))
        where = _rel(tex, root)

        for m in _GRAPHICSPATH.finditer(text):
            graphic_dirs += re.findall(r"\{([^}]*)\}", m.group(1))

        for m in _INPUT.finditer(text):
            name = m.group(1).strip()
            if not _is_literal(name):
                continue
            target = root / (name if PurePosixPath(name).suffix else name + ".tex")
            if _is_file(target):
                queue.append(target)
            elif _is_file(root / name):
                queue.append(root / name)
            else:
                missing.append((where, name))

        for m in _GRAPHICS.finditer(text):
            name = m.group(1).strip()
            if not _is_literal(name):
                continue
            if not any(
                _exists_with_exts(root / d, name, GRAPHICS_EXTS) for d in graphic_dirs
            ):
                missing.append((where, name))

        for m in _BIB.finditer(text):
            uses_bib = True
            if not check_bib:
                continue
            for name in _split_args(m.group(1)):
                if not _exists_with_exts(root, name, [".bib"]):
                    missing.append((where, name))

    return missing, uses_bib


def clean_zip(zip_bytes, extra_args=None, main_hint=None):
    extra_args = extra_args or []

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
        sources = [
            p for p in root.rglob("*") if p.suffix in (".tex", ".sty", ".cls") and p.is_file()
        ]
        if in_root and not any(
            _declares_encoding(_strip_comments(_read_tex(p))) for p in sources
        ):
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
        outside = [
            p
            for p in _all_tex_files(src)
            if root != p.parent and root not in p.parents
        ]
        if outside:
            warnings.append(
                f"{len(outside)} .tex file(s) outside “{_rel(root, src) or '.'}” "
                "were ignored because they are not inside the main file's folder."
            )

        wrong_case = [
            p for p in root.rglob("*") if p.suffix.lower() == ".tex" and p.suffix != ".tex"
        ]
        if wrong_case:
            names = ", ".join(f"“{_rel(p, src)}”" for p in wrong_case[:5])
            warnings.append(
                f"{names}: arxiv_latex_cleaner only cleans files ending in "
                "lower-case “.tex”, so comments in these files were NOT removed. "
                "Rename them to .tex and update the references."
            )

        missing, uses_bib = find_missing_files(root, main_tex)
        bbl = main_tex.with_suffix(".bbl")
        if uses_bib and not bbl.is_file():
            warnings.append(
                f"Your paper uses a .bib bibliography but “{bbl.name}” is not in "
                "the zip. arXiv does not run BibTeX/Biber, so references will "
                f"show as “?”. Compile locally (on Overleaf: Logs and output "
                f"files → Other logs and files) and add {bbl.name} next to "
                f"{main_tex.name}."
            )

        main_rel = _rel(main_tex, src)
        root_rel = _rel(root, src) or "."
        restore_rel = [p.relative_to(root) for p in restore if root in p.parents]
        staged = Path(tmp) / STAGING_NAME
        shutil.move(str(root), str(staged))

        cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(staged), *extra_args]
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
                f"The cleaner did not keep {main_tex.name}; the output would be "
                "unusable on arXiv."
            )

        _restore_encodings(restore_rel, cleaned)

        # Re-check the cleaned sources: anything they still reference that is
        # absent from the output (but was in the upload) was dropped by the
        # cleaner, e.g. names with brackets or folders ending in "git".
        still_missing, _ = find_missing_files(
            cleaned, cleaned / main_tex.name, check_bib=False
        )
        dropped = sorted({ref for where, ref in still_missing if (where, ref) not in missing})
        if dropped:
            names = ", ".join(f"“{ref}”" for ref in dropped[:10])
            warnings.append(
                f"arxiv_latex_cleaner left out {names}, which your paper still "
                "uses. This is a known issue with file or folder names containing "
                "brackets, spaces or other special characters. Rename them (and "
                "their references) or add them to the cleaned zip by hand."
            )

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
        )
