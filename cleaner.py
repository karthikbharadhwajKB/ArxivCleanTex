import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _safe_extract(zip_bytes, dest):
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest_resolved)):
                raise ValueError(f"Unsafe path in zip: {name}")
        zf.extractall(dest)


def _project_root(path):
    entries = list(path.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return path


def clean_zip(zip_bytes, extra_args=None):
    extra_args = extra_args or []

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        src.mkdir()

        _safe_extract(zip_bytes, src)
        root = _project_root(src)

        if not list(root.rglob("*.tex")):
            raise ValueError("No .tex files found in the uploaded zip.")

        cmd = [sys.executable, "-m", "arxiv_latex_cleaner", str(root), *extra_args]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"arxiv_latex_cleaner failed:\n{message}")

        cleaned = root.parent / f"{root.name}_arXiv"
        if not cleaned.exists():
            raise RuntimeError("Cleaned output folder was not created.")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
            for file in sorted(cleaned.rglob("*")):
                if file.is_file():
                    out.write(file, file.relative_to(cleaned))
        return buffer.getvalue()
