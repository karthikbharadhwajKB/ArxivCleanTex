import io
import os
import struct
import zipfile

import pytest

import cleaner
from cleaner import (
    AmbiguousMainFileError,
    CleanerOptions,
    CleaningFailedError,
    InvalidOptionsError,
    InvalidZipError,
    MainFileNotFoundError,
    NoTexFilesError,
    build_cleaner_args,
    clean_zip,
    find_main_tex,
    find_dropped_files,
    find_missing_files,
    parse_commands,
)
from helpers import doc, make_raw_name_zip, make_zip, unzip

APPLE_DOUBLE = (
    struct.pack(">IIH", 0x00051607, 0x00020000, 0)
    + b"Mac OS X        "
    + struct.pack(">H", 2)
    + bytes(range(0x80, 0xFF))
)


# --- Zip extraction -----------------------------------------------------------


class TestEntryName:
    def test_plain_name_is_unchanged(self):
        assert cleaner._entry_name(zipfile.ZipInfo("paper/main.tex")) == "paper/main.tex"

    def test_backslashes_become_separators(self):
        info = zipfile.ZipInfo("paper\\figs\\plot.png")
        assert cleaner._entry_name(info) == "paper/figs/plot.png"

    def test_utf8_bytes_without_flag_are_decoded_as_utf8(self):
        info = zipfile.ZipInfo("résumé.tex".encode("utf-8").decode("cp437"))
        info.flag_bits &= ~0x800
        assert cleaner._entry_name(info) == "résumé.tex"

    def test_real_cp437_name_is_kept(self):
        info = zipfile.ZipInfo("\u2560.tex")  # "╠": cp437 byte 0xCC, invalid UTF-8
        info.flag_bits &= ~0x800
        assert cleaner._entry_name(info) == "\u2560.tex"

    def test_name_with_utf8_flag_is_trusted(self):
        info = zipfile.ZipInfo("图.tex")
        info.flag_bits |= 0x800
        assert cleaner._entry_name(info) == "图.tex"


class TestSafeExtract:
    def test_extracts_files_and_folders(self, tmp_path):
        data = make_zip({"a/b/main.tex": "x", "a/empty/": "", "top.txt": "y"})
        cleaner._safe_extract(data, tmp_path)
        assert (tmp_path / "a/b/main.tex").read_text() == "x"
        assert (tmp_path / "a/empty").is_dir()
        assert (tmp_path / "top.txt").read_text() == "y"

    def test_windows_backslash_paths(self, tmp_path):
        cleaner._safe_extract(make_zip({"paper\\main.tex": "x"}), tmp_path)
        assert (tmp_path / "paper/main.tex").is_file()

    def test_utf8_names_without_flag(self, tmp_path):
        cleaner._safe_extract(make_raw_name_zip({"résumé/图.tex": "x"}), tmp_path)
        assert (tmp_path / "résumé/图.tex").is_file()

    def test_not_a_zip(self, tmp_path):
        with pytest.raises(InvalidZipError, match="not a valid .zip"):
            cleaner._safe_extract(b"definitely not a zip", tmp_path)

    def test_truncated_zip(self, tmp_path):
        with pytest.raises(InvalidZipError, match="not a valid .zip"):
            cleaner._safe_extract(make_zip({"main.tex": doc()})[:40], tmp_path)

    @pytest.mark.parametrize("name", ["../evil.tex", "/etc/evil.tex", "a/../../evil.tex"])
    def test_rejects_paths_outside_destination(self, tmp_path, name):
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(InvalidZipError, match="Unsafe path"):
            cleaner._safe_extract(make_zip({name: "x"}), dest)

    def test_password_protected(self, tmp_path):
        data = bytearray(make_zip({"main.tex": doc()}))
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            data[data.find(signature) + offset] |= 0x1
        with pytest.raises(InvalidZipError, match="password-protected"):
            cleaner._safe_extract(bytes(data), tmp_path)

    def test_unsupported_compression(self, tmp_path):
        data = bytearray(make_zip({"main.tex": doc()}))
        deflate64 = (9).to_bytes(2, "little")
        data[8:10] = deflate64
        central = data.find(b"PK\x01\x02")
        data[central + 10 : central + 12] = deflate64
        with pytest.raises(InvalidZipError, match="compression method"):
            cleaner._safe_extract(bytes(data), tmp_path)

    def test_corrupted_entry(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("main.tex", doc("x" * 5000))
        data = bytearray(buffer.getvalue())
        data[60:70] = b"\xff" * 10
        with pytest.raises(InvalidZipError, match="corrupted"):
            cleaner._safe_extract(bytes(data), tmp_path)


class TestRemoveJunk:
    def test_removes_os_metadata(self, tree):
        base = tree(
            {
                "main.tex": "x",
                "__MACOSX/paper/._main.tex": APPLE_DOUBLE,
                "paper/._main.tex": APPLE_DOUBLE,
                "paper/.DS_Store": "x",
                "figs/Thumbs.db": "x",
                "figs/desktop.ini": "x",
                "figs/plot.png": "x",
            }
        )
        cleaner._remove_junk(base)
        remaining = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        assert remaining == ["figs/plot.png", "main.tex"]
        assert not (base / "__MACOSX").exists()

    def test_removes_symlinks(self, tree):
        base = tree({"main.tex": "x"})
        (base / "link.tex").symlink_to("/etc/passwd")
        cleaner._remove_junk(base)
        assert not (base / "link.tex").is_symlink()


# --- Encodings ----------------------------------------------------------------


class TestEncodings:
    def test_utf8_files_are_untouched(self, tree):
        base = tree({"main.tex": "café", "fig.tikz": "x"})
        assert cleaner._normalize_encodings(base) == ([], [])
        assert (base / "main.tex").read_text("utf-8") == "café"

    def test_non_utf8_file_becomes_utf8_and_round_trips(self, tree):
        original = "Wait… “quoted” café ¢".encode("cp1252")
        base = tree({"main.tex": original})
        restore, converted = cleaner._normalize_encodings(base)
        assert restore == [base / "main.tex"] and converted == []
        text = (base / "main.tex").read_text("utf-8")  # valid UTF-8 now
        assert cleaner._encode_back(text) == original

    def test_placeholders_are_not_whitespace(self, tree):
        # cp1252 "…" is 0x85, which is U+0085 (a line break) under Latin-1.
        base = tree({"main.tex": b"a\x85b"})
        cleaner._normalize_encodings(base)
        text = (base / "main.tex").read_text("utf-8")
        assert len(text.splitlines()) == 1 and not text[1].isspace()

    def test_encode_back_handles_every_high_byte(self):
        data = bytes(range(0x80, 0x100)) + "é".encode("utf-8")
        text = data.decode("utf-8", errors="arxivcleantex_pua")
        assert cleaner._encode_back(text) == data

    @pytest.mark.parametrize("codec", ["utf-16", "utf-32"])
    def test_utf16_and_utf32_are_converted(self, tree, codec):
        base = tree({"main.tex": "café".encode(codec)})
        restore, converted = cleaner._normalize_encodings(base)
        assert restore == [] and converted == [base / "main.tex"]
        assert (base / "main.tex").read_text("utf-8") == "café"

    def test_only_files_the_cleaner_reads(self, tree):
        base = tree({"refs.bib": "é".encode("latin-1"), "style.sty": b"\xff"})
        assert cleaner._normalize_encodings(base) == ([], [])

    def test_restore_encodings(self, tree):
        base = tree({"sec/a.tex": "é".encode("latin-1")})
        cleaner._normalize_encodings(base)
        cleaner._restore_encodings([base.joinpath("sec/a.tex").relative_to(base)], base)
        assert (base / "sec/a.tex").read_bytes() == b"\xe9"

    def test_restore_skips_files_the_cleaner_dropped(self, tree):
        base = tree({})
        cleaner._restore_encodings([base.joinpath("gone.tex").relative_to(base)], base)

    @pytest.mark.parametrize(
        "text",
        [
            "\\usepackage[latin1]{inputenc}",
            "\\usepackage[T1]{fontenc}\\usepackage[ansinew]{inputenc}",
            "\\RequirePackage[koi8-r]{inputenc}",
            "\\usepackage[cp1251]{inputenc}",
            "\\usepackage[utf8,latin9]{inputenc}",
            "\\inputencoding{latin2}",
            "\\begin{CJK}{GBK}{song}",
            "\\XeTeXinputencoding \"cp1252\"",
        ],
    )
    def test_declares_encoding(self, text):
        assert cleaner._declares_encoding(text)

    @pytest.mark.parametrize(
        "text",
        ["", "\\usepackage[utf8]{inputenc}", "\\inputencoding{utf8}", "\\usepackage[T1]{fontenc}"],
    )
    def test_does_not_declare_encoding(self, text):
        assert not cleaner._declares_encoding(text)


# --- Main file detection --------------------------------------------------------


class TestHelpers:
    def test_strip_comments(self):
        assert cleaner._strip_comments("a % c\nb \\% kept % gone") == "a \nb \\% kept "

    def test_is_main_candidate(self, tree):
        base = tree(
            {
                "main.tex": doc(),
                "chapter.tex": "\\section{x}",
                "commented.tex": "% \\documentclass{article}\n\\begin{document}",
            }
        )
        assert cleaner._is_main_candidate(base / "main.tex")
        assert not cleaner._is_main_candidate(base / "chapter.tex")
        assert not cleaner._is_main_candidate(base / "commented.tex")

    def test_all_tex_files_skips_junk(self, tree):
        base = tree({"a.tex": "", "__MACOSX/b.tex": "", "._c.tex": "", ".git/d.tex": "", "e.txt": ""})
        assert [p.name for p in cleaner._all_tex_files(base)] == ["a.tex"]

    def test_match_user_path(self, tree):
        base = tree({"Paper/src/main.tex": "", "other/main.tex": ""})
        assert cleaner._match_user_path(base, "src/main.tex") == [base / "Paper/src/main.tex"]
        assert cleaner._match_user_path(base, " ./paper/ ") == [base / "Paper"]
        assert cleaner._match_user_path(base, "Paper\\src") == [base / "Paper/src"]
        assert len(cleaner._match_user_path(base, "main.tex")) == 2

    def test_match_user_path_skips_junk(self, tree):
        base = tree({"paper/main.tex": "", "__MACOSX/paper/main.tex": ""})
        assert cleaner._match_user_path(base, "paper/main.tex") == [base / "paper/main.tex"]

    def test_in_previous_output(self, tree):
        base = tree({"paper_arXiv/main.tex": "", "paper/main.tex": ""})
        assert cleaner._in_previous_output(base / "paper_arXiv/main.tex", base)
        assert not cleaner._in_previous_output(base / "paper/main.tex", base)


class TestFindMainTex:
    def test_prefers_main_tex(self, tree):
        base = tree({"main.tex": doc(), "other.tex": doc()})
        assert find_main_tex(base) == base / "main.tex"

    def test_deeply_nested(self, tree):
        base = tree({"a/b/c/main.tex": doc(), "a/readme.txt": ""})
        assert find_main_tex(base) == base / "a/b/c/main.tex"

    def test_single_candidate_with_any_name(self, tree):
        base = tree({"src/paper.tex": doc(), "src/intro.tex": "Intro"})
        assert find_main_tex(base) == base / "src/paper.tex"

    def test_shallowest_candidate_wins(self, tree):
        base = tree({"paper.tex": doc(), "figs/standalone.tex": doc()})
        assert find_main_tex(base) == base / "paper.tex"

    def test_main_tex_without_documentclass_is_fallback(self, tree):
        base = tree({"main.tex": "\\input{x}", "x.tex": "y"})
        assert find_main_tex(base) == base / "main.tex"

    def test_ignores_previous_arxiv_output(self, tree):
        base = tree({"paper/main.tex": doc(), "paper_arXiv/main.tex": doc()})
        assert find_main_tex(base) == base / "paper/main.tex"

    def test_ambiguous(self, tree):
        base = tree({"p1/main.tex": doc(), "p2/main.tex": doc()})
        with pytest.raises(AmbiguousMainFileError, match="p1/main.tex"):
            find_main_tex(base)

    def test_no_tex_files(self, tree):
        base = tree({"paper.pdf": "x"})
        with pytest.raises(NoTexFilesError, match="No .tex files"):
            find_main_tex(base)

    def test_only_upper_case_extension(self, tree):
        base = tree({"MAIN.TEX": doc()})
        with pytest.raises(NoTexFilesError, match="upper-case"):
            find_main_tex(base)

    def test_no_candidate(self, tree):
        base = tree({"a.tex": "x", "b.tex": "y"})
        with pytest.raises(MainFileNotFoundError, match="a.tex"):
            find_main_tex(base)

    @pytest.mark.parametrize("hint", ["paper.tex", "paper", "src/paper.tex", "SRC/PAPER.TEX", "  paper.tex  "])
    def test_hint_file(self, tree, hint):
        base = tree({"src/paper.tex": doc(), "main.tex": doc()})
        assert find_main_tex(base, hint) == base / "src/paper.tex"

    @pytest.mark.parametrize("hint", ["p2", "p2/", "./p2", "p2\\"])
    def test_hint_folder(self, tree, hint):
        base = tree({"p1/main.tex": doc(), "p2/main.tex": doc()})
        assert find_main_tex(base, hint) == base / "p2/main.tex"

    def test_hint_folder_without_tex(self, tree):
        base = tree({"main.tex": doc(), "figs/plot.png": ""})
        with pytest.raises(NoTexFilesError, match="figs"):
            find_main_tex(base, "figs")

    def test_hint_folder_without_candidate(self, tree):
        base = tree({"main.tex": doc(), "sections/intro.tex": "x"})
        with pytest.raises(MainFileNotFoundError, match="sections"):
            find_main_tex(base, "sections")

    def test_hint_not_found_lists_files(self, tree):
        base = tree({"main.tex": doc()})
        with pytest.raises(MainFileNotFoundError, match="main.tex"):
            find_main_tex(base, "nope.tex")

    def test_hint_matches_several_files(self, tree):
        base = tree({"a/paper.tex": doc(), "b/paper.tex": doc()})
        with pytest.raises(AmbiguousMainFileError, match="several files"):
            find_main_tex(base, "paper.tex")

    def test_hint_matches_several_folders(self, tree):
        base = tree({"a/src/main.tex": doc(), "b/src/main.tex": doc()})
        with pytest.raises(AmbiguousMainFileError, match="several folders"):
            find_main_tex(base, "src")

    def test_empty_hint_means_auto_detect(self, tree):
        base = tree({"main.tex": doc()})
        assert find_main_tex(base, "   ") == base / "main.tex"


# --- Commands and references ------------------------------------------------------


class TestParseCommands:
    @pytest.mark.parametrize(
        "raw, valid, rejected",
        [
            ("", [], []),
            ("todo note", ["todo", "note"], []),
            ("\\todo, \\note{}", ["todo", "note"], []),
            ("todo,,note", ["todo", "note"], []),
            ("my@cmd", ["my@cmd"], []),
            ("todo* x-y \\a\\b", [], ["todo*", "x-y", "\\a\\b"]),
        ],
    )
    def test_parse(self, raw, valid, rejected):
        assert parse_commands(raw) == (valid, rejected)


class TestFindMissingFiles:
    def test_complete_project(self, tree):
        base = tree(
            {
                "main.tex": doc(
                    "\\input{sections/intro}\\include{sections/end.tex}"
                    "\\includegraphics[width=1cm]{plot}\\includegraphics{figs/b.pdf}"
                    "\\bibliography{refs, more}",
                    "\\graphicspath{{figs/}}",
                ),
                "sections/intro.tex": "\\includegraphics{figs/c}",
                "sections/end.tex": "",
                "figs/plot.png": "",
                "figs/b.pdf": "",
                "figs/c.jpg": "",
                "refs.bib": "",
                "more.bib": "",
            }
        )
        assert find_missing_files(base, base / "main.tex") == ([], True)

    def test_reports_missing_with_source(self, tree):
        base = tree(
            {
                "main.tex": doc("\\input{sections/a}\\includegraphics{nope}\\addbibresource{refs.bib}"),
                "sections/a.tex": "\\input{sections/gone}",
            }
        )
        missing, uses_bib = find_missing_files(base, base / "main.tex")
        assert uses_bib
        assert sorted(missing) == [
            ("main.tex", "nope"),
            ("main.tex", "refs.bib"),
            ("sections/a.tex", "sections/gone"),
        ]

    def test_ignores_comments_and_macros(self, tree):
        base = tree({"main.tex": doc("% \\input{gone}\n\\input{\\dir/x}\\includegraphics{#1}")})
        assert find_missing_files(base, base / "main.tex") == ([], False)

    def test_follows_inputs_of_non_tex_files(self, tree):
        base = tree({"main.tex": doc("\\input{figs/plot.tikz}"), "figs/plot.tikz": "\\input{gone}"})
        assert find_missing_files(base, base / "main.tex") == ([("figs/plot.tikz", "gone")], False)

    def test_follows_inputs_of_files_without_extension(self, tree):
        base = tree({"main.tex": doc("\\input{sections/intro}"), "sections/intro": "\\input{gone}"})
        assert find_missing_files(base, base / "main.tex") == ([("sections/intro", "gone")], False)

    def test_check_bib_false(self, tree):
        base = tree({"main.tex": doc("\\bibliography{refs}")})
        assert find_missing_files(base, base / "main.tex", check_bib=False) == ([], True)

    def test_cyclic_inputs_terminate(self, tree):
        base = tree({"main.tex": doc("\\input{a}"), "a.tex": "\\input{main}"})
        assert find_missing_files(base, base / "main.tex") == ([], False)

    def test_odd_references_do_not_crash(self, tree):
        base = tree({"main.tex": doc("\\input{/}\\input{.}\\input{" + "a" * 300 + "}")})
        missing, _ = find_missing_files(base, base / "main.tex")
        assert len(missing) == 3


class TestResolve:
    def test_input(self, tree):
        base = tree({"a.tex": "", "b": "", "c.tikz": ""})
        assert cleaner._resolve(base, "input", "a") == base / "a.tex"
        assert cleaner._resolve(base, "input", "b") == base / "b"
        assert cleaner._resolve(base, "input", "c.tikz") == base / "c.tikz"
        assert cleaner._resolve(base, "input", "gone") is None

    def test_graphics_with_graphicspath(self, tree):
        base = tree({"figs/plot.pdf": ""})
        assert cleaner._resolve(base, "graphics", "plot", ("",)) is None
        assert cleaner._resolve(base, "graphics", "plot", ("", "figs/")) == base / "figs/plot.pdf"

    @pytest.mark.parametrize("kind", [".sty", ".cls", ".bst", ".bib"])
    def test_support_files(self, tree, kind):
        base = tree({f"sub/x{kind}": ""})
        assert cleaner._resolve(base, kind, "sub/x") == base / f"sub/x{kind}"
        assert cleaner._resolve(base, kind, f"sub/x{kind}") == base / f"sub/x{kind}"
        assert cleaner._resolve(base, kind, "x") is None


class TestScanReferences:
    def test_collects_every_kind(self, tree):
        base = tree(
            {
                "main.tex": "\\documentclass[a4]{myclass}\\usepackage[x]{a, b}"
                "\\RequirePackage{c}\\input{sec}\\bibliographystyle{mybst}"
                "\\bibliography{refs}\\graphicspath{{figs/}}",
                "sec.tex": "\\includegraphics[w]{plot}",
            }
        )
        refs, uses_bib = cleaner._scan_references(base, base / "main.tex")
        assert uses_bib
        assert sorted((w, k, n) for w, k, n, _ in refs) == [
            ("main.tex", ".bib", "refs"),
            ("main.tex", ".bst", "mybst"),
            ("main.tex", ".cls", "myclass"),
            ("main.tex", ".sty", "a"),
            ("main.tex", ".sty", "b"),
            ("main.tex", ".sty", "c"),
            ("main.tex", "input", "sec"),
            ("sec.tex", "graphics", "plot"),
        ]

    def test_missing_ignores_system_packages(self, tree):
        base = tree({"main.tex": doc("", "\\usepackage{amsmath}\\bibliographystyle{plain}")})
        assert find_missing_files(base, base / "main.tex") == ([], False)


class TestFindDroppedFiles:
    def setup(self, tree, original, cleaned, main):
        tree({f"orig/{k}": v for k, v in original.items()})
        base = tree({f"clean/{k}": v for k, v in cleaned.items()})
        return find_dropped_files(base / "orig", base / "clean", main)

    def test_nothing_dropped(self, tree):
        files = {"main.tex": doc("\\includegraphics{a}"), "a.png": ""}
        assert self.setup(tree, files, files, "main.tex") == []

    def test_figure_without_extension(self, tree):
        main = doc("\\includegraphics{figs/d}")
        dropped = self.setup(tree, {"main.tex": main, "figs/d.eps": ""}, {"main.tex": main}, "main.tex")
        assert dropped == [("figs/d", "it is referenced without its extension; write “figs/d.eps”")]

    @pytest.mark.parametrize(
        "command, file",
        [
            ("\\usepackage{styles/s}", "styles/s.sty"),
            ("\\bibliographystyle{bst/b}", "bst/b.bst"),
        ],
    )
    def test_support_file_in_subfolder(self, tree, command, file):
        main = doc(command)
        dropped = self.setup(tree, {"main.tex": main, file: ""}, {"main.tex": main}, "main.tex")
        assert len(dropped) == 1 and "subfolders need the extension" in dropped[0][1]

    def test_class_in_subfolder(self, tree):
        main = "\\documentclass{cls/mine}\\begin{document}\\end{document}"
        dropped = self.setup(tree, {"main.tex": main, "cls/mine.cls": ""}, {"main.tex": main}, "main.tex")
        assert dropped[0][0] == "cls/mine"

    def test_special_characters(self, tree):
        main = doc("\\input{intro (1)}")
        dropped = self.setup(tree, {"main.tex": main, "intro (1).tex": ""}, {"main.tex": main}, "main.tex")
        assert dropped == [("intro (1)", cleaner._dropped_reason("input", "intro (1)", None))]

    def test_files_missing_from_upload_are_not_dropped(self, tree):
        main = doc("\\includegraphics{gone}\\usepackage{styles/gone}")
        assert self.setup(tree, {"main.tex": main}, {"main.tex": main}, "main.tex") == []

    def test_bib_files_are_expected_to_be_removed(self, tree):
        main = doc("\\bibliography{refs}")
        assert self.setup(tree, {"main.tex": main, "refs.bib": ""}, {"main.tex": main}, "main.tex") == []


# --- End to end -----------------------------------------------------------------


class TestCleanZip:
    def test_flat_project(self):
        result = clean_zip(
            make_zip(
                {
                    "main.tex": doc("Hi % secret\n\\input{sections/intro}\\includegraphics{figs/plot}"),
                    "sections/intro.tex": "Intro % secret",
                    "figs/plot.png": "png",
                    "figs/unused.png": "png",
                    "main.aux": "aux",
                }
            )
        )
        files = unzip(result.zip_bytes)
        assert sorted(files) == ["figs/plot.png", "main.tex", "sections/intro.tex"]
        assert b"secret" not in files["main.tex"] + files["sections/intro.tex"]
        assert (result.main_file, result.project_root) == ("main.tex", ".")
        assert result.missing_files == [] and result.warnings == []

    def test_result_details(self):
        files = {
            "paper/main.tex": doc("\\includegraphics{figs/d}\\bibliography{refs}"),
            "paper/figs/d.eps": "eps",
            "paper/refs.bib": "@a{}",
            "paper/main.aux": "aux",
        }
        result = clean_zip(make_zip(files))
        assert sorted(result.input_files) == ["figs/d.eps", "main.aux", "main.tex", "refs.bib"]
        assert list(result.output_files) == ["main.tex"]
        assert result.input_files["figs/d.eps"] == 3
        assert result.dropped_files == ["figs/d"]
        assert result.missing_bbl

    def test_nested_project_is_flattened(self):
        result = clean_zip(make_zip({"a/b/paper/main.tex": doc(), "a/README.md": "x"}))
        assert list(unzip(result.zip_bytes)) == ["main.tex"]
        assert (result.main_file, result.project_root) == ("a/b/paper/main.tex", "a/b/paper")

    def test_main_hint(self):
        result = clean_zip(make_zip({"p1/main.tex": doc("one"), "p2/main.tex": doc("two")}), [], "p2")
        assert b"two" in unzip(result.zip_bytes)["main.tex"]
        assert any("outside “p2”" in w for w in result.warnings)

    @pytest.mark.parametrize(
        "files",
        [
            {"main.tex": doc(), "__MACOSX/._main.tex": APPLE_DOUBLE, ".DS_Store": b"\x00\xa2"},
            {"paper/main.tex": doc(), "__MACOSX/paper/._main.tex": APPLE_DOUBLE},
        ],
    )
    def test_macos_zips(self, files):
        assert list(unzip(clean_zip(make_zip(files)).zip_bytes)) == ["main.tex"]

    def test_latin1_bytes_are_preserved(self):
        source = doc("Price: 5¢ café % commentaire é", "\\usepackage[latin1]{inputenc}").encode("latin-1")
        result = clean_zip(make_zip({"main.tex": source}))
        out = unzip(result.zip_bytes)["main.tex"]
        assert b"5\xa2 caf\xe9 %" in out and b"commentaire" not in out
        assert result.warnings == []

    def test_undeclared_non_utf8_warns(self):
        result = clean_zip(make_zip({"main.tex": doc("Привет").encode("cp1251")}))
        assert any("not valid UTF-8" in w for w in result.warnings)

    def test_encoding_declared_in_sty(self):
        files = {
            "main.tex": doc("Привет", "\\usepackage{mystyle}").encode("koi8-r"),
            "mystyle.sty": "\\RequirePackage[koi8-r]{inputenc}",
        }
        assert clean_zip(make_zip(files)).warnings == []

    def test_utf16_is_converted(self):
        result = clean_zip(make_zip({"main.tex": doc("café").encode("utf-16")}))
        assert "café".encode() in unzip(result.zip_bytes)["main.tex"]
        assert any("UTF-16" in w for w in result.warnings)

    def test_utf16_outside_paper_is_not_reported(self):
        files = {"a/main.tex": doc(), "b/main.tex": doc().encode("utf-16")}
        assert not any("UTF-16" in w for w in clean_zip(make_zip(files), [], "a").warnings)

    def test_windows_zip(self):
        files = {"paper\\main.tex": doc("\\includegraphics{figs/plot}"), "paper\\figs\\plot.png": "x"}
        assert sorted(unzip(clean_zip(make_zip(files)).zip_bytes)) == ["figs/plot.png", "main.tex"]

    def test_non_ascii_names(self):
        files = {"résumé/main.tex": doc("\\includegraphics{图/plot}"), "résumé/图/plot.png": b"x"}
        result = clean_zip(make_raw_name_zip(files))
        assert result.main_file == "résumé/main.tex" and result.missing_files == []
        assert sorted(unzip(result.zip_bytes)) == ["main.tex", "图/plot.png"]

    def test_commands_to_delete(self):
        names, _ = parse_commands("\\todo note")
        result = clean_zip(make_zip({"main.tex": doc("A\\todo{secret}B\\note{x}C")}), ["--commands_to_delete", *names])
        assert b"ABC" in unzip(result.zip_bytes)["main.tex"]

    def test_keep_bib(self):
        files = {"main.tex": doc("\\bibliography{refs}"), "refs.bib": "@a{}", "main.bbl": "x"}
        assert "refs.bib" not in unzip(clean_zip(make_zip(files)).zip_bytes)
        assert "refs.bib" in unzip(clean_zip(make_zip(files), ["--keep_bib"]).zip_bytes)

    def test_missing_bbl_warns(self):
        result = clean_zip(make_zip({"main.tex": doc("\\bibliography{refs}"), "refs.bib": "@a{}"}))
        assert any("main.bbl" in w for w in result.warnings)

    def test_bbl_present(self):
        files = {"main.tex": doc("\\bibliography{refs}"), "refs.bib": "@a{}", "main.bbl": "x"}
        result = clean_zip(make_zip(files))
        assert "main.bbl" in unzip(result.zip_bytes) and result.warnings == []

    def test_bib_paths_are_not_reported_missing(self):
        # Overleaf projects often use root-relative bib paths, e.g. latex/ref.
        files = {"latex/main.tex": doc("\\bibliography{latex/ref}"), "latex/ref.bib": "@a{}"}
        result = clean_zip(make_zip(files))
        assert result.missing_files == []
        assert any("main.bbl" in w for w in result.warnings)

    def test_missing_files_reported(self):
        result = clean_zip(make_zip({"main.tex": doc("\\includegraphics{figs/gone}")}))
        assert result.missing_files == [("main.tex", "figs/gone")]

    def test_files_dropped_by_cleaner_are_reported(self):
        files = {
            "main.tex": doc("\\input{sections/intro (1)}\\includegraphics{digit/plot}"),
            "sections/intro (1).tex": "Intro",
            "digit/plot.png": "x",
        }
        warnings = clean_zip(make_zip(files)).warnings
        assert len(warnings) == 2
        assert all("special characters" in w for w in warnings)
        assert "digit/plot" in warnings[0] and "sections/intro (1)" in warnings[1]

    def test_upstream_file_rules(self):
        files = {
            "main.tex": doc(
                "\\includegraphics{figs/diagram}\\includegraphics{figs/ok.eps}",
                "\\usepackage{amsmath,rootstyle}\\usepackage{styles/sub}"
                "\\usepackage{styles/exact.sty}",
            ),
            "figs/diagram.eps": "x",
            "figs/ok.eps": "x",
            "rootstyle.sty": "x",
            "styles/sub.sty": "x",
            "styles/exact.sty": "x",
        }
        result = clean_zip(make_zip(files))
        assert sorted(unzip(result.zip_bytes)) == [
            "figs/ok.eps", "main.tex", "rootstyle.sty", "styles/exact.sty"
        ]
        assert result.warnings == [
            "arxiv_latex_cleaner left out “figs/diagram”, which your paper still "
            "uses: it is referenced without its extension; write “figs/diagram.eps”.",
            "arxiv_latex_cleaner left out “styles/sub”, which your paper still uses: "
            "files in subfolders need the extension in the reference; write "
            "“styles/sub.sty” or move the file next to the main file.",
        ]

    def test_intentionally_removed_files_are_not_reported(self):
        files = {"main.tex": doc("\\iffalse\\includegraphics{figs/hidden}\\fi ok"), "figs/hidden.png": "x"}
        assert clean_zip(make_zip(files)).warnings == []

    def test_folder_named_like_a_zip(self):
        assert list(unzip(clean_zip(make_zip({"paper.zip/main.tex": doc()})).zip_bytes)) == ["main.tex"]

    def test_upper_case_extension_warns(self):
        files = {"main.tex": doc("\\input{App.TEX}"), "App.TEX": "x % secret"}
        assert any("App.TEX" in w for w in clean_zip(make_zip(files)).warnings)

    def test_size_limit_warning(self, monkeypatch):
        monkeypatch.setattr(cleaner, "ARXIV_SIZE_LIMIT", 10)
        assert any("50 MB" in w for w in clean_zip(make_zip({"main.tex": doc()})).warnings)

    def test_errors_propagate(self):
        with pytest.raises(InvalidZipError):
            clean_zip(b"nope")
        with pytest.raises(NoTexFilesError):
            clean_zip(make_zip({"a.txt": "x"}))

    def test_cleaner_failure(self, monkeypatch):
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "Traceback (most recent call last):\nValueError: boom"

        monkeypatch.setattr(cleaner.subprocess, "run", lambda *a, **k: Failed())
        with pytest.raises(CleaningFailedError, match="ValueError: boom") as info:
            clean_zip(make_zip({"main.tex": doc()}))
        assert "Traceback" in info.value.details

    def test_cleaner_produced_no_output(self, monkeypatch):
        class Succeeded:
            returncode = 0
            stdout = stderr = ""

        monkeypatch.setattr(cleaner.subprocess, "run", lambda *a, **k: Succeeded())
        with pytest.raises(CleaningFailedError, match="not created"):
            clean_zip(make_zip({"main.tex": doc()}))

    def test_cleaner_dropped_main_file(self, monkeypatch, tmp_path):
        class Succeeded:
            returncode = 0
            stdout = stderr = ""

        def fake_run(cmd, **kwargs):
            (cleaner.Path(cmd[-1]).parent / "paper_arXiv").mkdir()
            return Succeeded()

        monkeypatch.setattr(cleaner.subprocess, "run", fake_run)
        with pytest.raises(CleaningFailedError, match="did not keep main.tex"):
            clean_zip(make_zip({"main.tex": doc()}))


# --- Output fidelity ------------------------------------------------------------


UPSTREAM_PROJECT = {
    "main.tex": doc(
        "Hi % secret\n\\input{sections/intro}\\includegraphics{figs/plot}"
        "\\todo{draft}\\iffalse hidden\\fi\\begin{comment}x\\end{comment}"
        "\\hl{kept}\\begin{note}n\\end{note}\\ifdraft d\\else e\\fi"
        "\\bibliography{refs}",
        "\\usepackage{rootstyle}",
    ),
    "sections/intro.tex": "Intro % secret",
    "sections/unused.tex": "Unused",
    "notes.tex": "Root file % secret",
    "figs/plot.png": "png",
    "figs/unused.png": "png",
    "rootstyle.sty": "sty",
    "main.bbl": "bbl",
    "refs.bib": "bib",
    "main.aux": "aux",
    "main.log": "log",
}


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--keep_bib"],
        ["--commands_to_delete", "todo"],
        ["--commands_only_to_delete", "hl", "--environments_to_delete", "note"],
        ["--if_exceptions", "ifdraft"],
    ],
)
def test_output_is_exactly_what_arxiv_latex_cleaner_produces(tmp_path, extra_args):
    """The app only prepares the input; the cleaned files must be byte-for-byte
    what running arxiv_latex_cleaner on the same folder produces."""
    project = tmp_path / "project"
    for name, data in UPSTREAM_PROJECT.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(data)
    cleaner.subprocess.run(
        [cleaner.sys.executable, "-m", "arxiv_latex_cleaner", str(project), *extra_args],
        check=True,
        capture_output=True,
    )
    upstream_out = tmp_path / "project_arXiv"
    expected = {
        p.relative_to(upstream_out).as_posix(): p.read_bytes()
        for p in upstream_out.rglob("*")
        if p.is_file()
    }

    for layout in ("", "nested/folder/"):
        files = {layout + name: data for name, data in UPSTREAM_PROJECT.items()}
        assert unzip(clean_zip(make_zip(files), extra_args).zip_bytes) == expected


# --- Cleaner options --------------------------------------------------------------


def png_bytes(width, height, noisy=False):
    from PIL import Image

    image = Image.new("RGB", (width, height), "red")
    if noisy:
        image.putdata([((i * 7919) % 256,) * 3 for i in range(width * height)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def image_size(data):
    from PIL import Image

    return Image.open(io.BytesIO(data)).size


class TestBuildCleanerArgs:
    def test_defaults_add_nothing(self):
        assert build_cleaner_args(CleanerOptions()) == ([], [])

    def test_every_option(self):
        args, notes = build_cleaner_args(
            CleanerOptions(
                keep_bib=True,
                resize_images=True, im_size=800,
                compress_pdf=True, pdf_im_resolution=300,
                images_allowlist='{"a.png": 2000, "b.pdf": 150}',
                convert_png_to_jpg=True, png_quality=70, png_size_threshold=1.5,
                commands_to_delete="\\todo note",
                commands_only_to_delete="hl",
                environments_to_delete="comment",
                if_exceptions="\\ifdraft",
                use_external_tikz="\\tikz\\out\\",
                svg_inkscape=True, svg_inkscape_path="svgs/",
            )
        )
        assert notes == []
        assert args == [
            "--keep_bib",
            "--resize_images", "--im_size", "800",
            "--compress_pdf", "--pdf_im_resolution", "300",
            "--convert_png_to_jpg", "--png_quality", "70", "--png_size_threshold", "1.5",
            "--images_allowlist", '{"a.png": 2000, "b.pdf": 150}',
            "--commands_to_delete", "todo", "note",
            "--commands_only_to_delete", "hl",
            "--environments_to_delete", "comment",
            "--if_exceptions", "ifdraft",
            "--use_external_tikz", "tikz/out",
            "--svg_inkscape", "svgs",
        ]

    def test_dependent_values_ignored_when_off(self):
        options = CleanerOptions(im_size=9, pdf_im_resolution=9, png_quality=9, svg_inkscape_path="x")
        assert build_cleaner_args(options) == ([], [])

    def test_svg_inkscape_default_folder(self):
        assert build_cleaner_args(CleanerOptions(svg_inkscape=True))[0] == ["--svg_inkscape"]

    def test_invalid_names_become_notes(self):
        args, notes = build_cleaner_args(
            CleanerOptions(
                commands_to_delete="todo x-y",
                commands_only_to_delete="a*",
                environments_to_delete="note2 note",
                if_exceptions="draft ifok \\if-x",
            )
        )
        assert args == [
            "--commands_to_delete", "todo",
            "--environments_to_delete", "note",
            "--if_exceptions", "ifok",
        ]
        assert len(notes) == 4
        assert "`x-y`" in notes[0] and "`a*`" in notes[1] and "`note2`" in notes[2]
        assert "`\\if-x`" in notes[3] and "`draft`" in notes[3]

    @pytest.mark.parametrize(
        "options, message",
        [
            (CleanerOptions(images_allowlist="{bad"), "not valid JSON"),
            (CleanerOptions(images_allowlist='["a.png"]'), "map image paths"),
            (CleanerOptions(images_allowlist='{"a.png": "big"}'), "map image paths"),
            (CleanerOptions(images_allowlist='{"a.png": true}'), "map image paths"),
            (CleanerOptions(convert_png_to_jpg=True, png_quality=101), "between 0 and 100"),
            (CleanerOptions(use_external_tikz="../outside"), "inside your project"),
            (CleanerOptions(svg_inkscape=True, svg_inkscape_path="a/../../b"), "inside your project"),
        ],
    )
    def test_invalid_options(self, options, message):
        with pytest.raises(InvalidOptionsError, match=message):
            build_cleaner_args(options)


class TestConfig:
    def test_empty_config(self):
        assert cleaner._load_config(b"") == {}

    def test_valid_config(self):
        config = b"im_size: 100\npatterns_and_insertions:\n  - {pattern: a, insertion: b, description: c}\n"
        assert cleaner._load_config(config)["im_size"] == 100

    @pytest.mark.parametrize(
        "config, message",
        [
            (b"key: [unclosed", "not valid YAML"),
            (b"\xff\xfe", "not valid YAML"),
            (b"- a\n- b", "YAML mapping"),
            (b"patterns_and_insertions:\n  - {pattern: a, insertion: b}", "description"),
            (b"patterns_and_insertions: nope", "description"),
        ],
    )
    def test_invalid_config(self, config, message):
        with pytest.raises(InvalidOptionsError, match=message):
            cleaner._load_config(config)

    def test_scalar_config_values_become_flags(self):
        config = {
            "resize_images": True, "im_size": 150, "keep_bib": False,
            "svg_inkscape": True, "use_external_tikz": "tikz", "png_size_threshold": 0.1,
            "commands_to_delete": ["todo"],
        }
        assert cleaner._config_scalar_args(config, []) == [
            "--resize_images", "--im_size", "150", "--png_size_threshold", "0.1",
            "--use_external_tikz", "tikz", "--svg_inkscape",
        ]

    def test_config_folders_must_stay_inside_project(self):
        with pytest.raises(InvalidOptionsError, match="use_external_tikz"):
            cleaner._config_scalar_args({"use_external_tikz": "../../etc"}, [])

    def test_ui_values_take_precedence(self):
        config = {"im_size": 150, "svg_inkscape": "custom"}
        assert cleaner._config_scalar_args(config, ["--im_size", "900"]) == ["--svg_inkscape", "custom"]


class TestCleanZipOptions:
    def clean(self, files, config=None, **options):
        args, _ = build_cleaner_args(CleanerOptions(**options))
        return clean_zip(make_zip(files), args, None, config)

    def body(self, result):
        return unzip(result.zip_bytes)["main.tex"].split(b"\\begin{document}")[1]

    def test_content_options(self):
        result = self.clean(
            {"main.tex": doc("A\\hl{kept}B\\todo{gone}C\\begin{note}secret\\end{note}D")},
            commands_to_delete="todo", commands_only_to_delete="hl", environments_to_delete="note",
        )
        assert b"AkeptBCD" in self.body(result)

    def test_resize_and_allowlist(self):
        files = {
            "main.tex": doc("\\includegraphics{a}\\includegraphics{b}"),
            "a.png": png_bytes(1000, 800),
            "b.png": png_bytes(1000, 800),
        }
        out = unzip(self.clean(files, resize_images=True, im_size=200, images_allowlist='{"b.png": 600}').zip_bytes)
        assert image_size(out["a.png"]) == (200, 160)
        assert image_size(out["b.png"]) == (600, 480)

    def test_png_to_jpg(self):
        files = {
            "main.tex": doc("\\includegraphics{figs/a.png}\\includegraphics{figs/b}"),
            "figs/a.png": png_bytes(300, 300, noisy=True),
            "figs/b.png": png_bytes(300, 300, noisy=True),
        }
        result = self.clean(files, convert_png_to_jpg=True, png_size_threshold=0.0)
        assert sorted(unzip(result.zip_bytes)) == ["figs/a.jpg", "figs/b.jpg", "main.tex"]
        assert b"figs/a.jpg" in self.body(result)
        assert result.warnings == []

    def test_compress_pdf_needs_ghostscript(self, monkeypatch):
        monkeypatch.setattr(cleaner.shutil, "which", lambda name: None)
        with pytest.raises(InvalidOptionsError, match="Ghostscript"):
            self.clean({"main.tex": doc()}, compress_pdf=True)

    def test_compress_pdf_runs_with_ghostscript(self, monkeypatch, tmp_path):
        # A stand-in `gs` that copies the input, to check the option reaches it.
        fake_gs = tmp_path / "gs"
        fake_gs.write_text(
            '#!/bin/sh\nfor a; do case $a in -sOutputFile=*) out=${a#-sOutputFile=};; esac; done\n'
            'echo compressed > "$out"\n'
        )
        fake_gs.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
        files = {"main.tex": doc("\\includegraphics{fig.pdf}"), "fig.pdf": "%PDF original"}
        out = unzip(self.clean(files, compress_pdf=True, pdf_im_resolution=100).zip_bytes)
        assert out["fig.pdf"] == b"compressed\n"

    def test_external_tikz(self):
        files = {
            "main.tex": doc("\\tikzsetnextfilename{fig1}\\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture}"),
            "tikz/fig1.pdf": "pdf",
        }
        result = self.clean(files, use_external_tikz="tikz")
        assert b"\\includegraphics{tikz/fig1.pdf}" in self.body(result)
        assert "tikz/fig1.pdf" in unzip(result.zip_bytes)

    @pytest.mark.parametrize("options", [{"use_external_tikz": "nope"}, {"svg_inkscape": True}])
    def test_missing_option_folder_warns(self, options):
        result = self.clean({"main.tex": doc()}, **options)
        assert any("was not found" in w for w in result.warnings)

    def test_svg_inkscape(self):
        pdf_tex = "\\put(0,0){\\includegraphics[page=1]{d_svg-tex.pdf}}"
        files = {
            "main.tex": doc("\\includesvg{figs/d.svg}"),
            "figs/d.svg": "<svg/>",
            "svg-inkscape/d_svg-tex.pdf_tex": pdf_tex,
            "svg-inkscape/d_svg-tex.pdf": "pdf",
        }
        result = self.clean(files, svg_inkscape=True)
        assert b"\\includeinkscape{svg-inkscape/d_svg-tex.pdf_tex}" in self.body(result)
        assert sorted(unzip(result.zip_bytes)) == [
            "main.tex", "svg-inkscape/d_svg-tex.pdf", "svg-inkscape/d_svg-tex.pdf_tex"
        ]

    def test_config_file(self):
        config = (
            b"im_size: 150\nresize_images: true\ncommands_to_delete: [todo]\n"
            b"patterns_and_insertions:\n"
            b"  - pattern: '\\\\figcomp\\{(?P<first>.*?)\\}'\n"
            b"    insertion: '\\includegraphics{{{first}}}'\n"
            b"    description: figcomp\n"
        )
        files = {"main.tex": doc("\\figcomp{a}\\todo{x}\\note{y}"), "a.png": png_bytes(1000, 800)}
        result = self.clean(files, config=config, commands_to_delete="note")
        body = self.body(result)
        assert b"\\includegraphics{a}" in body and b"todo" not in body and b"note" not in body
        assert image_size(unzip(result.zip_bytes)["a.png"]) == (150, 120)

    def test_invalid_config_file(self):
        with pytest.raises(InvalidOptionsError):
            self.clean({"main.tex": doc()}, config=b"- a")
