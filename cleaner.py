import io
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_MAIN = "main.tex"

# Junk that zip tools (macOS Finder, Windows, editors) add to archives.
IGNORED_PARTS = {"__MACOSX", ".git", ".DS_Store", "Thumbs.db"}

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
    pass


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


def _safe_extract(zip_bytes, dest):
    dest_resolved = dest.resolve()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise InvalidZipError(
            "The uploaded file is not a valid .zip archive. "
            "Please re-create the zip and try again."
        )
    with zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise InvalidZipError(f"Unsafe path in zip: {name}")
        zf.extractall(dest)


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


def _pick_main(candidates, base, scope_desc):
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


def _exists_with_exts(root, name, exts):
    candidate = root / name
    if candidate.is_file():
        return True
    return any((root / (name + ext)).is_file() for ext in exts)


def _is_literal(name):
    # Skip references built from macros (e.g. \input{\dir/intro}); we can't resolve them.
    return name and "\\" not in name and "#" not in name


def find_missing_files(root, main_tex):
    """Returns (source file, missing reference) pairs for files the paper needs.

    Follows \\input/\\include from the main file and checks \\includegraphics
    and bibliography references. Paths are resolved relative to `root`, the
    folder of the main file, which is how LaTeX (and arXiv) resolves them.
    """
    missing = []
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
            target = root / name
            if not target.suffix:
                target = target.with_suffix(".tex")
            if target.is_file():
                queue.append(target)
            elif (root / name).is_file():
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
            for name in _split_args(m.group(1)):
                if not _exists_with_exts(root, name, [".bib"]):
                    missing.append((where, name))

    return missing


def clean_zip(zip_bytes, extra_args=None, main_hint=None):
    extra_args = extra_args or []

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        src.mkdir()

        _safe_extract(zip_bytes, src)
        main_tex = find_main_tex(src, main_hint)
        root = main_tex.parent
        warnings = []

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

        missing = find_missing_files(root, main_tex)

        cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(root), *extra_args]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise CleaningFailedError(f"arxiv_latex_cleaner failed:\n{message}")

        cleaned = root.parent / f"{root.name}_arXiv"
        if not cleaned.exists():
            raise CleaningFailedError("Cleaned output folder was not created.")

        if not (cleaned / main_tex.name).is_file():
            raise CleaningFailedError(
                f"The cleaner did not keep {main_tex.name}; the output would be "
                "unusable on arXiv."
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
            for file in sorted(cleaned.rglob("*")):
                if file.is_file():
                    out.write(file, file.relative_to(cleaned))

        return CleanResult(
            zip_bytes=buffer.getvalue(),
            main_file=_rel(main_tex, src),
            project_root=_rel(root, src) or ".",
            missing_files=missing,
            warnings=warnings,
        )
