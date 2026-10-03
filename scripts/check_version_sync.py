"""
Check that release-facing version references match VERSION.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", version):
        raise ValueError(f"VERSION must be MAJOR.MINOR.PATCH with an optional alpha/beta/rc prerelease, got {version!r}")
    return version


def read_python_assignment(path: Path, name: str) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def read_pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def check_file_contains(path: Path, expected: str, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    if expected not in text:
        errors.append(f"{path.relative_to(REPO_ROOT)} does not contain {expected!r}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check version references against VERSION.")
    parser.parse_args(argv)

    errors: list[str] = []
    version = read_version()

    package_version = read_python_assignment(REPO_ROOT / "bbackup" / "__init__.py", "__version__")
    if package_version != version:
        errors.append(f"bbackup/__init__.py __version__ is {package_version!r}, expected {version!r}")

    pyproject = read_pyproject()
    project = pyproject.get("project", {})
    pyproject_version = project.get("version")
    if pyproject_version != version:
        errors.append(f"pyproject.toml project.version is {pyproject_version!r}, expected {version!r}")

    requires_python = project.get("requires-python")
    if requires_python != ">=3.12":
        errors.append(f"pyproject.toml project.requires-python is {requires_python!r}, expected '>=3.12'")

    check_file_contains(REPO_ROOT / "README.md", f"version-{version.replace('-', '--')}-", errors)
    check_file_contains(REPO_ROOT / "README.md", "python-3.12%2B-", errors)
    check_file_contains(REPO_ROOT / "CHANGELOG.md", f"## [{version}]", errors)
    check_file_contains(REPO_ROOT / "CHANGELOG.md", f"v{version}...HEAD", errors)
    check_file_contains(REPO_ROOT / "docs" / "cli-skills.md", f"Version: {version}.", errors)

    if errors:
        for error in errors:
            print(f"version-sync: {error}", file=sys.stderr)
        return 1

    print(f"version-sync: ok ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
