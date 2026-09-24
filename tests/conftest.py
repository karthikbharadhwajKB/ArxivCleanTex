import pytest

from helpers import write_tree


@pytest.fixture
def tree(tmp_path):
    """Writes {relative path: str | bytes} under a temp dir and returns it."""
    return lambda files: write_tree(tmp_path, files)
