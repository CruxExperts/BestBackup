from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts import prepare_release as release_script
from scripts.prepare_release import ReleasePreparationError, prepare_release


OLD_VERSION = "2.3.4"
TARGET_VERSION = "2.3.5"
RELEASE_DATE = "2026-10-23"
GENERATED = (
    "docs/cli-skills.md",
    "docs/cli-skills-index.json",
    "bbackup/data/cli-skills.md",
    "bbackup/data/cli-skills-index.json",
    "docs/production-cli.md",
    "bbackup/data/production-schemas.json",
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(root: Path, *, real_generator: bool = False) -> None:
    _write(root, "VERSION", f"{TARGET_VERSION}\n")
    _write(root, ".release-please-manifest.json", f'{{\n  ".": "{TARGET_VERSION}"\n}}\n')
    _write(
        root,
        "pyproject.toml",
        '[project]\nname = "bbackup"\nversion = "2.3.4"\nrequires-python = ">=3.12"\n\n'
        '[tool.example]\nversion = "leave-this-alone"\n',
    )
    _write(
        root,
        "bbackup/__init__.py",
        '"""Package source."""\n\n__version__ = "2.3.4"\n__author__ = "Example"\n',
    )
    _write(
        root,
        "uv.lock",
        'version = 1\nrevision = 5\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "dependency"\nversion = "8.8.8"\nsource = { registry = "https://example.invalid" }\n\n'
        '[[package]]\nname = "bbackup"\nversion = "2.3.4"\nsource = { editable = "." }\ndependencies = [\n  { name = "dependency" },\n]\n',
    )
    _write(
        root,
        "README.md",
        "# bbackup\n\n"
        "[![Version 2.3.4](https://img.shields.io/badge/version-2.3.4-22c55e?style=flat-square)]"
        "(https://github.com/CruxExperts/BestBackup/releases/latest)\n\n"
        "## v2.3.4 tested scope\n",
    )
    _write(
        root,
        "docs/VERSIONING.md",
        "# Versioning\n\n`VERSION` is the canonical release version. The current version is `2.3.4`.\n"
        "An earlier support note mentions v2.3.4 and stays historical.\n",
    )
    _write(
        root,
        "CHANGELOG.md",
        "# Changelog\n\nCurated release notes stay with their release.\n\n"
        "## [Unreleased]\n\n"
        "### Added\n\n- Keep this complete curated note.\n\n"
        "### Fixed\n\n- Keep this other category too.\n\n"
        "## [2.3.4] - 2026-10-20\n\n### Changed\n\n- Existing historical entry.\n\n"
        "[Unreleased]: https://github.com/CruxExperts/BestBackup/compare/v2.3.4...HEAD\n"
        "[2.3.4]: https://github.com/CruxExperts/BestBackup/compare/v2.3.3...v2.3.4\n",
    )
    fake_generator = (
        "from __future__ import annotations\n"
        "import os, re, sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parent.parent\n"
        "version = re.search(r'__version__ = \\\"([^\\\"]+)', (root / 'bbackup/__init__.py').read_text()).group(1)\n"
        f"outputs = {GENERATED!r}\n"
        "content = f'Generated version: {version}\\n'\n"
        "if '--check' in sys.argv:\n"
        "    raise SystemExit(0 if all((root / name).is_file() and (root / name).read_text() == content for name in outputs) else 1)\n"
        "(root / outputs[0]).parent.mkdir(parents=True, exist_ok=True)\n"
        "(root / outputs[0]).write_text(content)\n"
        "if os.environ.get('FAIL_RELEASE_GENERATOR'):\n"
        "    raise SystemExit(2)\n"
        "for name in outputs[1:]:\n"
        "    path = root / name\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text(content)\n"
        "(root / 'generator-python.txt').write_text(sys.executable)\n"
    )
    if real_generator:
        source_root = Path(__file__).resolve().parents[1]
        for relative in (
            "scripts/generate_cli_skills.py",
            "bbackup/cli_metadata.py",
        ):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, destination)
    else:
        _write(root, "scripts/generate_cli_skills.py", fake_generator)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    paths = (
        "VERSION",
        ".release-please-manifest.json",
        "pyproject.toml",
        "bbackup/__init__.py",
        "uv.lock",
        "README.md",
        "docs/VERSIONING.md",
        "CHANGELOG.md",
        "generator-python.txt",
        *GENERATED,
    )
    return {
        relative: (root / relative).read_bytes() if (root / relative).is_file() else None
        for relative in paths
    }


def test_no_version_argument_uses_bot_target_and_preserves_notes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_repo(tmp_path)
    initial_lock = (tmp_path / "uv.lock").read_bytes()
    initial_version = (tmp_path / "VERSION").read_bytes()
    initial_manifest = (tmp_path / ".release-please-manifest.json").read_bytes()
    monkeypatch.setattr(release_script, "REPO_ROOT", tmp_path)

    assert release_script.main([]) == 0
    assert f"prepared v{TARGET_VERSION}" in capsys.readouterr().out

    assert (tmp_path / "VERSION").read_text() == f"{TARGET_VERSION}\n"
    assert (tmp_path / "VERSION").read_bytes() == initial_version
    assert (tmp_path / ".release-please-manifest.json").read_bytes() == initial_manifest
    assert 'version = "2.3.5"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "2.3.5"' in (tmp_path / "bbackup/__init__.py").read_text()
    lock = (tmp_path / "uv.lock").read_bytes()
    assert b'name = "dependency"\nversion = "8.8.8"' in lock
    assert lock.replace(b'version = "2.3.5"', b'version = "2.3.4"') == initial_lock
    assert "Version 2.3.5" in (tmp_path / "README.md").read_text()
    assert "## v2.3.4 tested scope" in (tmp_path / "README.md").read_text()
    assert "current version is `2.3.5`" in (tmp_path / "docs/VERSIONING.md").read_text()
    assert "An earlier support note mentions v2.3.4" in (tmp_path / "docs/VERSIONING.md").read_text()

    changelog = (tmp_path / "CHANGELOG.md").read_text()
    assert (
        f"## [Unreleased]\n\n## [{TARGET_VERSION}] - "
        f"{release_script.dt.date.today().isoformat()}"
    ) in changelog
    assert changelog.count("### Added") == 1
    assert changelog.count("### Fixed") == 1
    assert "- Keep this complete curated note." in changelog
    assert "[Unreleased]: https://github.com/CruxExperts/BestBackup/compare/v2.3.5...HEAD" in changelog
    assert "[2.3.5]: https://github.com/CruxExperts/BestBackup/compare/v2.3.4...v2.3.5" in changelog

    for relative in GENERATED:
        assert (tmp_path / relative).read_text() == "Generated version: 2.3.5\n"
    assert (tmp_path / "generator-python.txt").read_text() == __import__("sys").executable


def test_is_idempotent_and_check_mode_is_read_only(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path)
    prepared = _snapshot(tmp_path)

    prepare_release(release_date="2026-10-24", repo_root=tmp_path)
    assert _snapshot(tmp_path) == prepared

    before_check = _snapshot(tmp_path)
    prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path, check=True)
    assert _snapshot(tmp_path) == before_check


def test_runs_repository_generator_for_source_and_package_copies(tmp_path: Path) -> None:
    _make_repo(tmp_path, real_generator=True)

    prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path)

    source_catalog = (tmp_path / "docs/cli-skills.md").read_text()
    package_catalog = (tmp_path / "bbackup/data/cli-skills.md").read_text()
    assert "Version: 2.3.5." in source_catalog
    assert package_catalog == source_catalog
    assert "package version is v2.3.5." in (tmp_path / "docs/production-cli.md").read_text()
    assert (tmp_path / "docs/cli-skills-index.json").is_file()
    assert (tmp_path / "bbackup/data/cli-skills-index.json").is_file()
    assert (tmp_path / "bbackup/data/production-schemas.json").is_file()


@pytest.mark.parametrize("damage", ["target-heading", "current-link"])
def test_rejects_ambiguous_changelog_without_partial_writes(
    tmp_path: Path, damage: str
) -> None:
    _make_repo(tmp_path)
    changelog = tmp_path / "CHANGELOG.md"
    text = changelog.read_text()
    if damage == "target-heading":
        text = text.replace("## [2.3.4] - 2026-10-20", "## [2.3.5] - 2026-10-20", 1)
    else:
        text = text.replace("[2.3.4]: https://", "[2.3.4]: https://stale/", 1)
    changelog.write_text(text)
    before = _snapshot(tmp_path)

    with pytest.raises(ReleasePreparationError):
        prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path)

    assert _snapshot(tmp_path) == before


def test_check_mode_rejects_unprepared_target_without_writing(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    before = _snapshot(tmp_path)

    with pytest.raises(ReleasePreparationError, match="not complete"):
        prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path, check=True)

    assert _snapshot(tmp_path) == before


def test_manifest_must_match_bot_proposed_version_before_writes(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    manifest = tmp_path / ".release-please-manifest.json"
    manifest.write_text('{\n  ".": "2.3.6"\n}\n')
    before = _snapshot(tmp_path)

    with pytest.raises(ReleasePreparationError, match="VERSION .* disagree"):
        prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path)

    assert _snapshot(tmp_path) == before


def test_generator_failure_rolls_back_source_and_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repo(tmp_path)
    before = _snapshot(tmp_path)
    monkeypatch.setenv("FAIL_RELEASE_GENERATOR", "1")

    with pytest.raises(Exception):
        prepare_release(release_date=RELEASE_DATE, repo_root=tmp_path)

    assert _snapshot(tmp_path) == before
