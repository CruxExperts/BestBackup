"""Prepare a Release Please draft locally without publishing or committing it."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_PATTERN = r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?"
VERSION_RE = re.compile(rf"{VERSION_PATTERN}\Z")
CHANGELOG_BASE = "https://github.com/CruxExperts/BestBackup/compare/"


class ReleasePreparationError(ValueError):
    """Raised when release inputs are malformed or ambiguous."""


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleasePreparationError(f"cannot read {relative}: {exc}") from exc


def _one_substitution(text: str, pattern: str, replacement: str, relative: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ReleasePreparationError(
            f"expected exactly one current version field in {relative}; found {count}"
        )
    return updated


def _replace_line_value(
    text: str,
    line_pattern: str,
    expected: str,
    replacement: str,
    relative: str,
) -> str:
    pattern = rf"^{line_pattern}{re.escape(expected)}(?=\r?$)"
    return _one_substitution(text, pattern, lambda _match: replacement, relative)


def _version_from_file(text: str) -> str:
    value = text.strip()
    if not VERSION_RE.fullmatch(value):
        raise ReleasePreparationError(f"VERSION has an invalid value: {value!r}")
    return value


def _project_version(text: str) -> str:
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError as exc:
        raise ReleasePreparationError(f"pyproject.toml is malformed: {exc}") from exc
    value = project.get("version") if isinstance(project, dict) else None
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ReleasePreparationError("pyproject.toml [project].version is missing or invalid")
    return value


def _manifest_version(text: str) -> str:
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleasePreparationError(f".release-please-manifest.json is malformed: {exc}") from exc
    target = manifest.get(".") if isinstance(manifest, dict) else None
    if not isinstance(target, str) or not VERSION_RE.fullmatch(target):
        raise ReleasePreparationError(
            ".release-please-manifest.json must contain a valid root package version at '.'"
        )
    return target


def _replace_project_version(text: str, current: str, target: str) -> str:
    section = re.search(r"(?ms)^\[project\]\r?\n(?P<body>.*?)(?=^\[|\Z)", text)
    if section is None:
        raise ReleasePreparationError("pyproject.toml has no [project] table")
    body = section.group("body")
    updated_body = _replace_line_value(
        body,
        r'version = "',
        f'{current}"',
        f'version = "{target}"',
        "pyproject.toml [project].version",
    )
    return text[: section.start("body")] + updated_body + text[section.end("body") :]


def _replace_lock_package_version(text: str, current: str, target: str) -> str:
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == "[[package]]"]
    matching: list[tuple[int, int]] = []
    for offset, start in enumerate(starts):
        end = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        block = "".join(lines[start:end])
        if re.search(r'^name = "bbackup"\r?$', block, re.MULTILINE):
            matching.append((start, end))
    if len(matching) != 1:
        raise ReleasePreparationError(
            f"expected one bbackup package block in uv.lock; found {len(matching)}"
        )
    start, end = matching[0]
    block = "".join(lines[start:end])
    updated = _replace_line_value(
        block,
        r'version = "',
        f'{current}"',
        f'version = "{target}"',
        "uv.lock bbackup package version",
    )
    return "".join(lines[:start]) + updated + "".join(lines[end:])


def _replace_readme_badge(text: str, current: str, target: str) -> str:
    old_url_version = current.replace("-", "--")
    new_url_version = target.replace("-", "--")
    pattern = (
        rf"(\[!\[Version ){re.escape(current)}"
        rf"(\]\(https://img\.shields\.io/badge/version-)"
        rf"{re.escape(old_url_version)}"
        rf"(-22c55e\?style=flat-square\)\]"
        rf"\(https://github\.com/CruxExperts/BestBackup/releases/latest\))"
    )
    return _one_substitution(
        text,
        pattern,
        rf"\g<1>{target}\g<2>{new_url_version}\g<3>",
        "README.md version badge",
    )


def _validate_versioning(text: str, current: str, target: str) -> str:
    return _replace_line_value(
        text,
        r"`VERSION` is the canonical release version\. The current version is `",
        f"{current}`.",
        f"`VERSION` is the canonical release version. The current version is `{target}`.",
        "docs/VERSIONING.md current version declaration",
    )


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_values(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def _section_headings(lines: list[str]) -> list[tuple[int, str, str]]:
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        body = _line_body(line)
        if body.startswith("## "):
            match = re.fullmatch(r"## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?", body)
            if match is None:
                raise ReleasePreparationError(
                    f"ambiguous changelog section heading on line {index + 1}: {body!r}"
                )
            headings.append((index, match.group(1), match.group(2) or ""))
    return headings


def _definition_indices(lines: list[str], label: str) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if _line_body(line).startswith(f"[{label}]:")
    ]


def _changelog_plan(text: str, current: str, target: str, release_date: str) -> str:
    lines = _line_values(text)
    newline = _line_ending(text)
    headings = _section_headings(lines)
    unreleased_headings = [item for item in headings if item[1] == "Unreleased"]
    current_headings = [item for item in headings if item[1] == current]
    target_headings = [item for item in headings if item[1] == target]
    if len(unreleased_headings) != 1 or len(current_headings) != 1:
        raise ReleasePreparationError(
            "CHANGELOG.md must contain exactly one [Unreleased] and one current-version section"
        )
    if target_headings:
        raise ReleasePreparationError(f"CHANGELOG.md already contains release section [{target}]")

    unreleased_index = unreleased_headings[0][0]
    current_index, _current_label, current_date = current_headings[0]
    if current_index <= unreleased_index or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", current_date):
        raise ReleasePreparationError("CHANGELOG.md current release section is malformed")
    if current_index != next(
        (index for index, label, _date in headings if index > unreleased_index), -1
    ):
        raise ReleasePreparationError("CHANGELOG.md has ambiguous content between release sections")

    unreleased_links = _definition_indices(lines, "Unreleased")
    current_links = _definition_indices(lines, current)
    target_links = _definition_indices(lines, target)
    if len(unreleased_links) != 1 or len(current_links) != 1 or target_links:
        raise ReleasePreparationError("CHANGELOG.md comparison links are missing, duplicated, or stale")

    expected_unreleased = f"[Unreleased]: {CHANGELOG_BASE}v{current}...HEAD"
    unreleased_link = _line_body(lines[unreleased_links[0]])
    if unreleased_link != expected_unreleased:
        raise ReleasePreparationError("CHANGELOG.md [Unreleased] comparison link does not match VERSION")

    current_link = _line_body(lines[current_links[0]])
    prefix = f"[{current}]: {CHANGELOG_BASE}v"
    suffix = f"...v{current}"
    if not current_link.startswith(prefix) or not current_link.endswith(suffix):
        raise ReleasePreparationError("CHANGELOG.md current release comparison link is malformed")
    previous = current_link[len(prefix) : -len(suffix)]
    if not VERSION_RE.fullmatch(previous):
        raise ReleasePreparationError("CHANGELOG.md current release comparison has an invalid prior tag")

    section_body = lines[unreleased_index + 1 : current_index]
    while section_body and not _line_body(section_body[0]).strip():
        section_body.pop(0)
    while section_body and not _line_body(section_body[-1]).strip():
        section_body.pop()
    prepared_section = [
        f"## [Unreleased]{newline}",
        newline,
        f"## [{target}] - {release_date}{newline}",
        newline,
        *section_body,
        newline,
    ]
    updated_lines = lines[:unreleased_index] + prepared_section + lines[current_index:]

    updated_unreleased_idx = next(
        index for index, line in enumerate(updated_lines) if _line_body(line).startswith("[Unreleased]:")
    )
    updated_lines[updated_unreleased_idx] = (
        f"[Unreleased]: {CHANGELOG_BASE}v{target}...HEAD{newline}"
    )
    updated_current_idx = next(
        index for index, line in enumerate(updated_lines) if _line_body(line).startswith(f"[{current}]:")
    )
    updated_lines.insert(
        updated_current_idx,
        f"[{target}]: {CHANGELOG_BASE}v{current}...v{target}{newline}",
    )
    return "".join(updated_lines)


def _is_prepared_changelog(text: str, target: str) -> bool:
    lines = _line_values(text)
    headings = _section_headings(lines)
    unreleased = [item for item in headings if item[1] == "Unreleased"]
    target_sections = [item for item in headings if item[1] == target]
    if len(unreleased) != 1 or len(target_sections) != 1 or not target_sections[0][2]:
        return False
    next_heading_index = next(
        (index for index, _label, _date in headings if index > unreleased[0][0]), -1
    )
    if next_heading_index != target_sections[0][0]:
        return False
    if any(_line_body(line).strip() for line in lines[unreleased[0][0] + 1 : next_heading_index]):
        return False
    unreleased_links = _definition_indices(lines, "Unreleased")
    target_links = _definition_indices(lines, target)
    if len(unreleased_links) != 1 or len(target_links) != 1:
        return False
    if _line_body(lines[unreleased_links[0]]) != f"[Unreleased]: {CHANGELOG_BASE}v{target}...HEAD":
        return False
    target_link = _line_body(lines[target_links[0]])
    prefix = f"[{target}]: {CHANGELOG_BASE}v"
    suffix = f"...v{target}"
    if not target_link.startswith(prefix) or not target_link.endswith(suffix):
        return False
    previous = target_link[len(prefix) : -len(suffix)]
    return bool(VERSION_RE.fullmatch(previous))


def _source_paths(root: Path) -> dict[str, str]:
    return {
        relative: _read(root, relative)
        for relative in (
            "VERSION",
            ".release-please-manifest.json",
            "pyproject.toml",
            "bbackup/__init__.py",
            "uv.lock",
            "README.md",
            "docs/VERSIONING.md",
            "CHANGELOG.md",
        )
    }


def _planned_files(root: Path, release_date: str) -> dict[str, str]:
    files = _source_paths(root)
    target = _version_from_file(files["VERSION"])
    manifest_target = _manifest_version(files[".release-please-manifest.json"])
    if manifest_target != target:
        raise ReleasePreparationError(
            f"VERSION ({target}) and .release-please-manifest.json ({manifest_target}) disagree"
        )
    current = _project_version(files["pyproject.toml"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ReleasePreparationError(f"release date is invalid: {release_date!r}")
    try:
        dt.date.fromisoformat(release_date)
    except ValueError as exc:
        raise ReleasePreparationError(f"release date is invalid: {release_date!r}") from exc

    if current == target:
        if not _is_prepared_changelog(files["CHANGELOG.md"], target):
            raise ReleasePreparationError(
                f"VERSION is already {target}, but its changelog does not prove a prepared release"
            )
        files["pyproject.toml"] = _replace_project_version(
            files["pyproject.toml"], current, target
        )
        files["bbackup/__init__.py"] = _replace_line_value(
            files["bbackup/__init__.py"],
            r'__version__ = "',
            f'{current}"',
            f'__version__ = "{target}"',
            "bbackup/__init__.py __version__",
        )
        files["uv.lock"] = _replace_lock_package_version(files["uv.lock"], current, target)
        files["README.md"] = _replace_readme_badge(files["README.md"], current, target)
        files["docs/VERSIONING.md"] = _validate_versioning(
            files["docs/VERSIONING.md"], current, target
        )
        return files

    files["pyproject.toml"] = _replace_project_version(
        files["pyproject.toml"], current, target
    )
    files["bbackup/__init__.py"] = _replace_line_value(
        files["bbackup/__init__.py"],
        r'__version__ = "',
        f'{current}"',
        f'__version__ = "{target}"',
        "bbackup/__init__.py __version__",
    )
    files["uv.lock"] = _replace_lock_package_version(files["uv.lock"], current, target)
    files["README.md"] = _replace_readme_badge(files["README.md"], current, target)
    files["docs/VERSIONING.md"] = _validate_versioning(
        files["docs/VERSIONING.md"], current, target
    )
    files["CHANGELOG.md"] = _changelog_plan(
        files["CHANGELOG.md"], current, target, release_date
    )
    return files


def _generator_outputs() -> tuple[str, ...]:
    return (
        "docs/cli-skills.md",
        "docs/cli-skills-index.json",
        "bbackup/data/cli-skills.md",
        "bbackup/data/cli-skills-index.json",
        "docs/production-cli.md",
        "bbackup/data/production-schemas.json",
    )


def _invoke_generator(root: Path, check: bool) -> None:
    script = root / "scripts" / "generate_cli_skills.py"
    if not script.is_file():
        raise ReleasePreparationError(f"missing generator: {script}")
    command = [sys.executable, str(script)]
    if check:
        command.append("--check")
    subprocess.run(command, cwd=root, check=True)


def prepare_release(
    *,
    release_date: str | None = None,
    repo_root: Path = REPO_ROOT,
    check: bool = False,
) -> None:
    """Prepare a bot-proposed release using VERSION and the root manifest as its target."""
    root = Path(repo_root).resolve()
    planned = _planned_files(root, release_date or dt.date.today().isoformat())
    paths = {relative: root / relative for relative in planned}
    if check:
        mismatches = [
            relative
            for relative, content in planned.items()
            if paths[relative].read_bytes().decode("utf-8") != content
        ]
        if mismatches:
            raise ReleasePreparationError(
                "release preparation is not complete; would update: " + ", ".join(mismatches)
            )
        _invoke_generator(root, check=True)
        return

    # Keep a small rollback set so a failed generator cannot leave a half-prepared draft.
    owned_by_release_please = {"VERSION", ".release-please-manifest.json"}
    affected = {
        **{relative: path for relative, path in paths.items() if relative not in owned_by_release_please},
        **{relative: root / relative for relative in _generator_outputs()},
    }
    original: dict[Path, bytes | None] = {}
    for path in affected.values():
        original[path] = path.read_bytes() if path.is_file() else None
    try:
        for relative, content in planned.items():
            if relative in owned_by_release_please:
                continue
            paths[relative].write_bytes(content.encode("utf-8"))
        _invoke_generator(root, check=False)
    except Exception:
        for path, content in original.items():
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(content)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare version and changelog files for a local Release Please draft."
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Release section date (YYYY-MM-DD; defaults to today)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that source and generated release files are already prepared without writing",
    )
    args = parser.parse_args(argv)
    try:
        prepare_release(
            release_date=args.date,
            repo_root=args.repo_root,
            check=args.check,
        )
    except (ReleasePreparationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"prepare-release: {exc}", file=sys.stderr)
        return 1
    target = _version_from_file(_read(args.repo_root, "VERSION"))
    print(f"prepare-release: {'checked' if args.check else 'prepared'} v{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
