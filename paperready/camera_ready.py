"""Checks that need both the LaTeX source and the camera-ready PDF.

The ACL camera-ready mode cleans the source for arXiv and checks the PDF with
aclpubcheck; these checks compare the two, e.g. to catch a review-version
source uploaded next to a final PDF, or a PDF of a different paper.
"""

import re
import unicodedata
from dataclasses import dataclass

# Share of the title's letters whose words must appear, in order, in the PDF's
# header; titles with fewer words must match exactly, as short ones match too easily.
_TITLE_WORDS_FOUND = 0.8
_TITLE_MIN_WORDS = 4
# The line with the "Abstract" heading (ACL, NeurIPS, ICLR, IEEE "Abstract—…"),
# which PDF text often merges with the other column ("001 Abstract To load…");
# "Abstractive" doesn't count. Stopping early only makes the header shorter.
_ABSTRACT_HEADING = re.compile(r"(?<![a-z])(?<!extended )abstract(?![a-z])", re.IGNORECASE)
# Letters a first page has above its "Abstract" heading at the least (a title).
_HEADER_MIN = 20


@dataclass
class CrossCheck:
    ok: bool
    title: str
    detail: str = ""


def _compact(text):
    """Lower-case letters and digits only, without accents and with ligatures
    spelled out, so PDF text that lost its spaces or hyphenation
    ("Demo-\\ngraphic", "AnonymousACLsubmission"), or prints "ö" or "ﬃ" where
    the source has \\"{o} or ffi, still matches."""
    # Lower-case after NFKD: letters like ℝ or 𝐀 decompose to capitals.
    return "".join(ch for ch in unicodedata.normalize("NFKD", text).lower() if ch.isalnum())


def same_version(source_review, pdf_review):
    if source_review and pdf_review:
        return CrossCheck(
            False,
            "Both are review versions",
            "The source and the PDF are both anonymous submissions. Switch the template "
            f"to the final version and rebuild the PDF: {source_review}.",
        )
    if source_review:
        return CrossCheck(
            False,
            "The source is still the review version",
            "The PDF is final, but arXiv would get the anonymous submission built from "
            f"this source: {source_review}.",
        )
    if pdf_review:
        return CrossCheck(
            False,
            "The PDF is a review version",
            "The source is final, but the PDF is an anonymous submission. Upload the "
            "camera-ready PDF built from this source.",
        )
    return CrossCheck(True, "Source and PDF are both the final version")


def _header(first_page):
    """The first page above its "Abstract" heading (title, authors), compacted;
    without a heading, its first 1000 compacted characters. The title comes
    first, so a line like "Extended Abstract" at the very top is no heading."""
    header = ""
    for line in first_page.splitlines():
        if _ABSTRACT_HEADING.search(line) and len(header) >= _HEADER_MIN:
            return header
        header += _compact(line)
    return header[:1000]


def _in_order(words, text):
    """The largest total length of `words` that occur in `text` in this order."""
    frontier = {0: 0}  # where the last matched word ends -> matched length so far
    for word in words:
        for end, score in list(frontier.items()):
            i = text.find(word, end)
            if i >= 0 and frontier.get(i + len(word), -1) < score + len(word):
                frontier[i + len(word)] = score + len(word)
        best, kept = -1, {}
        for end in sorted(frontier):  # drop states that end later with no more matched
            if frontier[end] > best:
                kept[end] = best = frontier[end]
        frontier = kept
    return max(frontier.values())


def _mostly_in(title, header):
    """True if nearly all of a long enough title's words (by length) appear in
    order in the header. This tolerates what a source title cannot show, e.g. a
    macro the PDF prints as "SELF-INSTRUCT", or a venue line."""
    words = [w for w in map(_compact, re.split(r"[\s/-]+", title)) if len(w) >= 3]
    if len(words) < _TITLE_MIN_WORDS:
        return False
    return _in_order(words, header) >= _TITLE_WORDS_FOUND * sum(map(len, words))


def _exactly_in(title, text):
    """True if the compacted title is in the compacted `text`, also when only
    ASCII letters and digits are compared: the source may drop a symbol the PDF
    prints (e.g. a math letter the title spells with a macro). The ASCII
    comparison needs a mostly ASCII title, or "Модели BERT" would match any
    page with "BERT" on it."""
    compact = _compact(title)
    ascii_title = re.sub(r"[^0-9a-z]", "", compact)
    return compact in text or (
        len(ascii_title) >= _TITLE_WORDS_FOUND * len(compact)
        and ascii_title in re.sub(r"[^0-9a-z]", "", text)
    )


def _title_on_page(title, first_page):
    """True if the title is at the top of the first page: above the abstract,
    so another paper's abstract that uses the same words doesn't count."""
    if _ABSTRACT_HEADING.search(title):
        # The title's own "Abstract" would end the header early: whole page, exactly.
        return _exactly_in(title, _compact(first_page))
    header = _header(first_page)
    return _exactly_in(title, header) or _mostly_in(title, header)


def same_paper(source_title, pdf_first_page):
    if not _compact(source_title):
        return CrossCheck(
            True,
            "Same paper (not checked)",
            "No \\title with letters or digits was found in the source, so the PDF "
            "could not be matched to it.",
        )
    if _title_on_page(source_title, pdf_first_page):
        return CrossCheck(True, "Source and PDF are the same paper")
    return CrossCheck(
        False,
        "The PDF may be a different paper",
        f"The title in the source, “{source_title}”, is not on the PDF's first page. "
        "Check that you uploaded the matching PDF and zip.",
    )


def cross_checks(clean_result, acl_report, pdf_first_page):
    """All checks between a cleaned source (arxiv.CleanResult) and its PDF
    (acl.AclReport plus the text of the PDF's first page)."""
    return [
        same_version(clean_result.review_version, acl_report.likely_review_version),
        same_paper(clean_result.title, pdf_first_page),
    ]
