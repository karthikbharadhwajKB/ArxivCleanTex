import io
import zipfile

def make_zip(files):
    """Builds a zip from {name: str | bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def make_raw_name_zip(files):
    """Builds a zip whose names are raw UTF-8 bytes without the UTF-8 flag,
    as written by macOS Finder and the `zip` CLI."""
    placeholders = {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for i, (name, data) in enumerate(files.items()):
            raw = name.encode("utf-8")
            placeholder = (chr(ord("A") + i) * len(raw)).encode()
            placeholders[placeholder] = raw
            zf.writestr(placeholder.decode(), data)
    data = buffer.getvalue()
    for placeholder, raw in placeholders.items():
        data = data.replace(placeholder, raw)
    return data


def doc(body="Hello", preamble=""):
    return (
        "\\documentclass{article}\n\\usepackage{graphicx}\n"
        f"{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"
    )


def unzip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def write_tree(base, files):
    for name, data in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)
    return base
