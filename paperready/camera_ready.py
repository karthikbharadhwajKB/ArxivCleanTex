"""Checks that need both the LaTeX source and the camera-ready PDF.

The ACL camera-ready mode cleans the source for arXiv and checks the PDF with
aclpubcheck; these checks compare the two, e.g. to catch a review-version
source uploaded next to a final PDF, or a PDF of a different paper.
"""

import re
from dataclasses import dataclass


@dataclass
class CrossCheck:
    ok: bool
    title: str
    detail: str = ""


def _compact(text):
    """Lower-case letters and digits only, so PDF text that lost its spaces or
    hyphenation ("Demo-\\ngraphic", "AnonymousACLsubmission") still matches."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


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


def same_paper(source_title, pdf_first_page):
    if not source_title:
        return CrossCheck(
            True,
            "Same paper (not checked)",
            "No \\title was found in the source, so the PDF could not be matched to it.",
        )
    if _compact(source_title) in _compact(pdf_first_page):
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
