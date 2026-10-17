"""
Check GitHub publishing readiness surfaces that are easy to drift.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_REPO = "github.com/CruxExperts/BestBackup"
LEGACY_REPO = "github.com/cptnfren/best-backup"
PRIVATE_ROOTS = (str(REPO_ROOT.resolve()), str(Path.home()))
PY_COMPILE_COMMAND = (
    "uv run python -m py_compile bbackup.py bbman.py bbackup/*.py "
    "bbackup/data/*.py bbackup/management/*.py scripts/*.py"
)
PY_COMPILE_TARGETS = (
    "bbackup.py",
    "bbman.py",
    "bbackup/*.py",
    "bbackup/data/*.py",
    "bbackup/management/*.py",
    "scripts/*.py",
)


EXPECTED_ACTIONS = {
    ".github/workflows/ci.yml": [
        "actions/checkout",
        "actions/setup-python",
        "actions/upload-artifact",
    ],
    ".github/workflows/stale.yml": [
        "actions/stale",
    ],
}


PUBLIC_SCAN_ROOTS = [
    ".github",
    "bbackup",
    "docs",
    "scripts",
    "README.md",
    "INSTALL.md",
    "QUICKSTART.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    ".python-version",
    "project.yaml",
]

PUBLIC_EXTENSIONS = {".md", ".py", ".sh", ".yaml", ".yml", ".toml", ".txt"}


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def same_file(left: str, right: str) -> bool:
    return (REPO_ROOT / left).read_bytes() == (REPO_ROOT / right).read_bytes()


def tracked_public_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", *PUBLIC_SCAN_ROOTS],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files: list[Path] = []
    for line in result.stdout.splitlines():
        path = Path(line)
        if path.suffix in PUBLIC_EXTENSIONS and (REPO_ROOT / path).exists():
            files.append(path)
    return files


def main() -> int:
    errors: list[str] = []

    for path, actions in EXPECTED_ACTIONS.items():
        text = read(path)
        for action in actions:
            if not re.search(r"uses:\s*" + re.escape(action) + r"@[0-9a-f]{40}\b", text):
                errors.append(f"{path} must pin {action} to a full commit SHA")
    for workflow in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", workflow.read_text(), re.MULTILINE):
            if not action.startswith("./") and not re.fullmatch(r"[A-Za-z0-9_./-]+@[0-9a-f]{40}", action):
                errors.append(f"{workflow.name}: action must use a full commit SHA: {action}")

    for path in (
        "AGENTS.md",
        ".github/CONTRIBUTING.md",
        ".github/pull_request_template.md",
        "docs/PUBLISHING_CHECKLIST.md",
        "docs/VERSIONING.md",
        "docs/prompts/bootstrap-planning-agent.md",
        ".github/workflows/release-notes.yml",
    ):
        if PY_COMPILE_COMMAND not in read(path):
            errors.append(
                f"{path} must document the full py_compile command including bbackup/data/*.py"
            )

    if (REPO_ROOT / "setup.py").exists():
        errors.append("setup.py should not be present; pyproject.toml is the packaging source")
    for legacy_requirements in ("requirements.txt", "requirements-dev.txt"):
        if (REPO_ROOT / legacy_requirements).exists():
            errors.append(f"{legacy_requirements} should not be present; use pyproject.toml and uv.lock")
    manifest = REPO_ROOT / "MANIFEST.in"
    if not manifest.is_file() or any(pattern not in manifest.read_text() for pattern in (
        "recursive-include scripts", "recursive-include docs", "recursive-include packaging",
        "include VERSION uv.lock", "prune .agents",
    )):
        errors.append("MANIFEST.in must include portable source inputs and exclude private agent state")
    if not (REPO_ROOT / "uv.lock").exists():
        errors.append("uv.lock is missing")
    if (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() != "3.12":
        errors.append(".python-version must pin the local baseline to 3.12")
    for source, packaged in (
        ("config.yaml.example", "bbackup/data/config.yaml.example"),
        ("docs/cli-skills.md", "bbackup/data/cli-skills.md"),
        ("docs/cli-skills-index.json", "bbackup/data/cli-skills-index.json"),
    ):
        if not (REPO_ROOT / packaged).exists():
            errors.append(f"{packaged} is missing from packaged runtime resources")
        elif not same_file(source, packaged):
            errors.append(f"{packaged} is not in sync with {source}")

    release_workflow = read(".github/workflows/release-notes.yml")
    if "contents: write" not in release_workflow:
        errors.append(".github/workflows/release-notes.yml must grant contents: write")
    if (
        "uv build" not in release_workflow
        or "dist/bbackup-*.tar.gz" not in release_workflow
        or "dist/bbackup-*.whl" not in release_workflow
    ):
        errors.append(".github/workflows/release-notes.yml must build and attach bbackup dist artifacts")
    for release_check in (
        "uv run ruff check",
        "uv run python -m py_compile",
        "uv run python scripts/generate_cli_skills.py --check",
        "scripts/verify_release_source.py",
        "uv run python scripts/smoke_installed_artifact.py",
    ):
        if release_check not in release_workflow:
            errors.append(f".github/workflows/release-notes.yml must run {release_check}")

    ci_workflow = read(".github/workflows/ci.yml")
    if "contents: read" not in ci_workflow:
        errors.append(".github/workflows/ci.yml must grant contents: read")
    if "python-version: [\"3.12\", \"3.13\", \"3.14\"]" not in ci_workflow:
        errors.append(".github/workflows/ci.yml must test Python 3.12, 3.13, and 3.14")
    if "uv sync --locked" not in ci_workflow or "uv run pytest" not in ci_workflow:
        errors.append(".github/workflows/ci.yml must install and test through uv")
    if "--require-hashes" not in ci_workflow or ".github/uv-bootstrap.txt" not in ci_workflow:
        errors.append(".github/workflows/ci.yml must verify its pinned uv bootstrap")
    if "uv run pytest tests/ -m integration" not in ci_workflow:
        errors.append("CI must collect integration tests across the complete tests directory")
    if "name: CI required" not in ci_workflow or "if: always()" not in ci_workflow:
        errors.append("CI must expose its aggregate required result")
    for path in ("SECURITY.md", "SUPPORT.md", ".github/pull_request_template.md"):
        if "2.0.0-alpha.1 preview" in read(path) or "Version 2 is a preview" in read(path):
            errors.append(f"{path} contains stale version-two support wording")
    for target in PY_COMPILE_TARGETS:
        if target not in ci_workflow:
            errors.append(f".github/workflows/ci.yml py_compile step is missing {target}")

    stale_workflow = read(".github/workflows/stale.yml")
    if "issues: write" not in stale_workflow or "pull-requests: write" not in stale_workflow:
        errors.append(".github/workflows/stale.yml must grant issues and pull-requests write permissions")

    public_files = tracked_public_files()
    for path in public_files:
        text = read(str(path))
        if path != Path("scripts/check_publishing_ready.py"):
            if LEGACY_REPO in text:
                errors.append(f"{path} still references {LEGACY_REPO}")
            if any(root in text for root in PRIVATE_ROOTS):
                errors.append(f"{path} contains a local workspace or home path")

    for path in ("README.md", "INSTALL.md", "QUICKSTART.md", "SUPPORT.md"):
        if CANONICAL_REPO not in read(path):
            errors.append(f"{path} does not reference {CANONICAL_REPO}")

    for path in public_files:
        text = read(str(path))
        if re.search(r"github[.]com/[^\s\"<>)]*/best-backup(?:[/#?\s\"<>)]|$)", text):
            if CANONICAL_REPO not in text and path != Path("docs/prompts/bootstrap-planning-agent.md"):
                errors.append(f"{path} has a best-backup GitHub URL but not {CANONICAL_REPO}")

    changelog = read("CHANGELOG.md")
    if not re.search(r"https://github\.com/CruxExperts/BestBackup/compare/v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?\.\.\.HEAD", changelog):
        errors.append("CHANGELOG.md [Unreleased] compare link is not canonical")

    if errors:
        for error in errors:
            print(f"publishing-ready: {error}", file=sys.stderr)
        return 1

    print("publishing-ready: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
