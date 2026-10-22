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

## Version 2 tested-scope claims

- [ ] Describe v2.0.4 as qualified only for local file capture/restore, live PostgreSQL 17.11 / MySQL 8.4.12 / MariaDB 11.8.9 capture/restore, headless dashboard happy paths, and AMD64 Ubuntu 24.04/26.04 packages; this is the v2.0.3 tested core scope.
- [ ] Describe new v2.0.4 cloud and Linux workflows as experimental and unqualified; requests #23 and #25 were canceled and closed as not planned, while requests #24 and #26 remain open. Do not claim a guaranteed thirty-day window, off-host custody, final remote-server qualification, or complete-machine recovery.
- [ ] Keep B2/AWS, native Docker, ARM64 backup/restore, cross-application-version upgrades/rollback, root-owned systemd package templates, and cloud/off-host protection explicitly unqualified; describe the separate user-timer guest drills with their exact limits.
- [ ] State that ARM64 packages may be built by the release matrix but are not functionally qualified by package-install smoke alone.
- [ ] Keep native B2 capability inspection distinct from backup transfer; inspection is read-only and always reports `protection_qualified: false`.
- [ ] Describe the local Garage round trip as S3-compatible test evidence, not B2 or Amazon S3 qualification.
- [ ] Rerun the full test suite and installed-artifact smoke for the exact release candidate; prior test counts are historical evidence.
- [ ] Avoid unmeasured comparative performance, ransomware-proof, and unqualified production claims.
- [ ] Preserve the explicit uncertain-mutation reconciliation and no-automatic-replay behavior in operator guidance.

## Automation

- [ ] `git config core.hooksPath .githooks` is set in the active checkout.
- [ ] `.githooks/commit-msg` validates conventional commit subjects.
- [ ] `.githooks/pre-push` runs version sync, generated-doc checks, Markdown/asset provenance, and whitespace checks.
- [ ] `.github/workflows/ci.yml` runs syntax, tests, CLI docs, Markdown standards, version sync, support doc presence, and publishing readiness checks.
- [ ] `.github/workflows/release-notes.yml` validates and builds on a pushed `v*` tag; a separate default-branch `workflow_dispatch` verifies the exact successful run, tag commit, artifact inventory, and fixed-fingerprint `SHA256SUMS` signature before publishing.
- [ ] CI tests Python `3.12`, `3.13`, and `3.14`, matching the supported package classifiers.
- [ ] CI and release workflows smoke test the installed wheel.
- [ ] GitHub workflow actions use verified full commit SHAs; CI verifies hash-pinned bootstrap tooling.
- [ ] GitHub workflows declare least-privilege `permissions`.
- [ ] CI installs dependencies with `uv sync --locked`, collects integration tests across `tests/`, and exposes an aggregate `CI required` result.
- [ ] Ruff covers the package, all maintenance/release scripts, generated CLI skill checker, and tests.

## GitHub enforcement and release provenance

- [ ] Default-branch protection explicitly targets the default branch, requires signatures, pull requests, resolved conversations, and trusted passing CI, with no broad administrator bypass.
- [ ] Solo maintenance uses zero required human approvals; code ownership routes notifications without creating an impossible second-reviewer requirement.
- [ ] `v*` tag integrity and administrator creation authority are separate rulesets.
- [ ] Secret scanning, push protection, private reporting, code scanning, and external-contributor workflow approval are enabled and verified.
- [ ] SHA enforcement is enabled only after every remote workflow is pinned; required aggregate CI is selected only after that check exists and passes.
- [ ] Release source has successful exact-commit trusted default-branch CI, default-branch ancestry, and an annotated tag signed by the pinned release key.
- [ ] `source-verification.json` and `release-provenance.json` bind exact source, CI/build runs, artifact hashes, locked dependencies, and active artwork provenance; include both in the signed checksum inventory.
- [ ] Complete signed assets are uploaded and verified in a draft before one publication; immutable releases apply to future releases.
- [ ] Preserve previous settings privately before mutations. Reconcile uncertain publication or installation results read-only; never replay blindly.

## Version and release state

- [ ] `VERSION` contains the intended semantic version.
- [ ] Stable or prerelease version and tag match package metadata, release workflow, README badge, and dated changelog section.
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
uv run python scripts/check_assets.py
uv run python scripts/check_version_sync.py
uv run python scripts/check_publishing_ready.py
uv run python scripts/generate_cli_skills.py --check
uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py
uv build
uv run python scripts/smoke_installed_artifact.py
uv run pytest
git diff --check
```
