"""Runs aclpubcheck on one PDF in the current directory.

Usage: python acl_runner.py PAPER.pdf PAPER_TYPE BOTTOM REFERENCES NAMES

BOTTOM, REFERENCES and NAMES are "1" or "0". aclpubcheck's own command line
never runs its reference checks, and its options live in a module-level
`args`, so this sets that up and calls the checker directly. Results are
written by aclpubcheck as errors-<name>.json plus annotated page images.
"""

import sys
from types import SimpleNamespace

from aclpubcheck import formatchecker


def main(argv):
    pdf, paper_type, bottom, references, names = argv
    # aclpubcheck's flags are store_false: a true value means "run the check".
    formatchecker.args = SimpleNamespace(
        disable_bottom_check=bottom == "1",
        disable_name_check=names == "1",
    )
    formatchecker.Formatter().format_check(
        submission=pdf,
        paper_type=paper_type,
        output_dir=".",
        check_references=references == "1",
    )


if __name__ == "__main__":
    main(sys.argv[1:])
