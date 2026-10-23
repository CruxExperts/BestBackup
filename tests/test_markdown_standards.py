from pathlib import Path

from scripts import check_markdown_standards


FIXTURE = Path(__file__).parent / "fixtures" / "markdown" / "changelog-duplicate-category.md"
UNVERSIONED_FIXTURE = Path(__file__).parent / "fixtures" / "markdown" / "changelog-unversioned-category.md"
SAME_VERSION_DATES_FIXTURE = (
    Path(__file__).parent / "fixtures" / "markdown" / "changelog-same-version-different-dates.md"
)


def test_same_release_category_heading_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        check_markdown_standards,
        "relative_name",
        lambda _path: "CHANGELOG.md",
    )
    errors = check_markdown_standards.scan_file(FIXTURE)

    assert any(
        "heading anchor is duplicated in the same changelog version: added" in error
        for error in errors
    ), errors


def test_category_after_non_version_heading_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        check_markdown_standards,
        "relative_name",
        lambda _path: "CHANGELOG.md",
    )
    errors = check_markdown_standards.scan_file(UNVERSIONED_FIXTURE)

    assert any(
        "release category heading must be under a version heading: added" in error
        for error in errors
    ), errors


def test_same_release_identifier_with_different_dates_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        check_markdown_standards,
        "relative_name",
        lambda _path: "CHANGELOG.md",
    )
    errors = check_markdown_standards.scan_file(SAME_VERSION_DATES_FIXTURE)

    assert any(
        "heading anchor is duplicated in the same changelog version: added" in error
        for error in errors
    ), errors


def test_public_documents_skips_vendored_markdown_but_scans_authored_docs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(check_markdown_standards, "REPO_ROOT", tmp_path)
    authored = tmp_path / "docs" / "operator-guide.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("## Missing an H1\n", encoding="utf-8")
    vendored = tmp_path / ".github" / "release-please" / "node_modules" / "pkg" / "README.md"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("<script>vendor content</script>\n", encoding="utf-8")
    assert check_markdown_standards.scan_file(vendored)

    documents = check_markdown_standards.public_documents()

    assert authored in documents
    assert vendored not in documents
    authored_errors = check_markdown_standards.scan_file(authored)
    assert any("exactly one visible H1" in error for error in authored_errors)
