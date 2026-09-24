from urllib.parse import parse_qs, urlparse

import pytest

from paperready import feedback


def query(url):
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{feedback.REPO_URL}/issues/new"
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


def test_bug_report_without_error():
    params = query(feedback.bug_report_url())
    assert params["template"] == "bug_report.yml"
    assert "arxiv_latex_cleaner 1.0.11" in params["environment"] and "Python 3." in params["environment"]
    assert "title" not in params and "error" not in params and "mode" not in params


@pytest.mark.parametrize(
    "mode, name",
    [("arxiv", "Prepare for arXiv"), ("acl", "Check ACL format"), ("camera", "ACL camera-ready")],
)
def test_bug_report_mode(mode, name):
    assert query(feedback.bug_report_url(mode))["mode"] == name


def test_unknown_mode_is_left_out():
    assert "mode" not in query(feedback.bug_report_url("nope"))


def test_bug_report_with_error():
    params = query(feedback.bug_report_url("arxiv", "  No .tex files were found.\nMake sure you zipped…  "))
    assert params["title"] == "[Bug] No .tex files were found."
    assert params["error"] == "No .tex files were found.\nMake sure you zipped…"


def test_long_error_is_shortened():
    params = query(feedback.bug_report_url("arxiv", "x" * 5000))
    assert params["error"] == "x" * feedback.MAX_ERROR_CHARS + "\n… (shortened)"
    assert len(params["title"]) == len("[Bug] ") + 80


def test_non_ascii_error_keeps_the_link_short():
    url = feedback.bug_report_url("arxiv", "é" * 1000)
    assert len(url) < 6000
    assert query(url)["error"].endswith("é\n… (shortened)")


def test_feature_request():
    assert query(feedback.feature_request_url()) == {"template": "feature_request.yml"}


def test_missing_package_version(monkeypatch):
    def not_found(package):
        raise feedback.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(feedback.metadata, "version", not_found)
    assert "aclpubcheck not installed" in feedback.environment()


def test_issue_forms_match_the_link_fields():
    """The query parameters fill form fields by id, so the ids must exist."""
    from pathlib import Path

    import yaml

    forms = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"
    bug = yaml.safe_load((forms / "bug_report.yml").read_text())
    ids = {field.get("id") for field in bug["body"]}
    assert {"mode", "error", "environment"} <= ids
    assert (forms / "feature_request.yml").is_file()
    assert yaml.safe_load((forms / "config.yml").read_text()) == {"blank_issues_enabled": False}
