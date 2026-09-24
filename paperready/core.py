"""Shared upload handling: safe zip extraction and OS-junk removal."""

import io
import shutil
import zipfile
import zlib

# Junk that zip tools (macOS Finder, Windows, editors) add to archives.
IGNORED_PARTS = {"__MACOSX", ".git", ".DS_Store", "Thumbs.db"}
JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class CleanerError(Exception):
    """Base class for errors that are shown to the user as-is."""


class InvalidZipError(CleanerError):
    pass


def _file_sizes(base):
    return {
        p.relative_to(base).as_posix(): p.stat().st_size
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


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
