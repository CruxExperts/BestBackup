# Publishing checklist

Use this checklist before changing public project metadata or publishing a release.

## Documentation and community files

- [ ] `README.md` describes the project, install path, quick start, docs, and license.
- [ ] `INSTALL.md` and `QUICKSTART.md` are current.
- [ ] `SECURITY.md` explains supported versions and vulnerability reporting.
- [ ] `.github/CONTRIBUTING.md` documents setup, tests, commits, and PR expectations.
- [ ] `SUPPORT.md` explains where users should ask questions or get help.
- [ ] `.github/ISSUE_TEMPLATE/` and `.github/pull_request_template.md` are present.
- [ ] `LICENSE` has the intended owner and license.
- [ ] `docs/VERSIONING.md` describes release/version checks and local hook setup.
- [ ] The [GitHub Markdown Writing Standard](standards/github-markdown/github-markdown-writing-standard.md), review checklist,
      capabilities reference, and provenance register are current and linked from the documentation index.

## Repository profile

- [ ] GitHub's description tells visitors what the project backs up, how it works, and who it serves; it matches the current user-facing scope.
- [ ] Search topics cover supported capabilities, not planned work or generic marketing terms.
- [ ] The homepage links to a maintained standalone site; leave it unset when the README is the canonical landing page.
- [ ] Each enabled community surface has a maintained purpose: keep Issues for support, enable Discussions only when it will be answered, and disable a blank Wiki rather than duplicate repository docs.

## Version 2 preview claims

- [ ] Identify `2.0.0-alpha.1` as a pre-release preview; do not call it production-ready.
- [ ] Describe Ubuntu 24.04 and 26.04 on AMD64 and ARM64 as target platforms until packages and upgrade/rollback behavior are tested.
- [ ] State that Backblaze B2 through S3 and Amazon S3 are configurable destinations and restore sources while provider-specific thirty-day protection and recovery remain unqualified.
- [ ] Keep native B2 capability inspection distinct from backup transfer; inspection is read-only and always reports `protection_qualified: false`.
- [ ] Describe the local Garage round trip as S3-compatible test evidence, not B2 or Amazon S3 qualification.
- [ ] Keep the prior 745-test result identified as prior evidence; rerun the suite for this release payload.
- [ ] Avoid unmeasured comparative performance, ransomware-proof, and unqualified production claims.
- [ ] Preserve the explicit uncertain-mutation reconciliation and no-automatic-replay behavior in operator guidance.

## Automation

- [ ] `git config core.hooksPath .githooks` is set in the active checkout.
- [ ] `.githooks/commit-msg` validates conventional commit subjects.
- [ ] `.githooks/pre-push` runs version sync, generated-doc checks, and whitespace checks.
- [ ] `.github/workflows/ci.yml` runs syntax, tests, CLI docs, Markdown standards, version sync, support doc presence, and publishing readiness checks.
- [ ] `.github/workflows/release-notes.yml` verifies the tag, Markdown standards, version and publishing checks, builds with `uv build`, and creates a GitHub release from `v*` tags.
- [ ] CI tests Python `3.12`, `3.13`, and `3.14`, matching the supported package classifiers.
- [ ] CI and release workflows smoke test the installed wheel.
- [ ] GitHub workflow actions are on current supported major versions.
- [ ] GitHub workflows declare least-privilege `permissions`.
- [ ] CI installs dependencies with `uv sync --locked` and runs commands with `uv run`.
- [ ] Ruff covers the package, selected release scripts, generated CLI skill checker, and tests.

## Version and release state

- [ ] `VERSION` contains the intended semantic version.
- [ ] Prerelease versions and tags match the package metadata, release workflow, README badge, and matching changelog section.
- [ ] `bbackup/__init__.py`, `pyproject.toml`, README badge, `CHANGELOG.md`, and generated CLI skills docs match `VERSION`.
- [ ] `pyproject.toml` requires Python `>=3.12`.
- [ ] `.python-version` pins the local baseline to `3.12`.
- [ ] `uv.lock` is present and current.
- [ ] `CHANGELOG.md` has a dated section for the release.
- [ ] `uv sync --locked` passes.
- [ ] `uv run python scripts/check_version_sync.py` passes.
- [ ] `uv run python scripts/check_publishing_ready.py` passes.
- [ ] `uv run python scripts/check_markdown_standards.py` passes.
- [ ] `uv run python scripts/generate_cli_skills.py --check` passes.

## Scrub

- [ ] No secrets, API keys, tokens, private keys, `.env` files, or credentials are tracked.
- [ ] No local backup archives, logs, caches, or runtime state are tracked.
- [ ] No machine-specific install paths are documented as required paths.
- [ ] Public URLs point to the intended GitHub repository.
- [ ] Localsetup runtime state and agent run ledgers remain untracked.

## Validation

```bash
uv sync --locked
uv run python scripts/check_markdown_standards.py
uv run python scripts/check_version_sync.py
uv run python scripts/check_publishing_ready.py
uv run python scripts/generate_cli_skills.py --check
uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py
uv build
uv run python scripts/smoke_installed_artifact.py
uv run pytest
git diff --check
```
