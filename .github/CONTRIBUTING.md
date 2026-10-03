# Contributing

Thanks for helping improve bbackup. Keep changes focused and make behavior claims match the checked-in implementation and its evidence. Version 2.0.0-alpha.1 is a pre-release; `bbackup production` remains a preview while the legacy 1.x workflow is retained.

## Development setup

Use Python 3.12 or newer and the locked project environment:

```bash
git clone https://github.com/CruxExperts/best-backup.git
cd best-backup
uv sync --locked
uv run bbackup --version
uv run bbackup production --help
```

Enable the repository hooks once per checkout:

```bash
git config core.hooksPath .githooks
```

The preview's ordinary tests use local repositories and executable fixtures. Live cloud accounts, production database credentials, and real recovery data are not required for unit tests. Never commit passwords, host bindings, customer data, or cloud tokens.

## Validate a change

Run checks that cover the changed behavior. For a release-ready source change, use the repository's full validation sequence in [VERSIONING.md](../docs/VERSIONING.md). For documentation changes, run the Markdown standards checker.

```bash
uv run ruff check bbackup/ scripts/ tests/
uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py
uv run python scripts/check_markdown_standards.py
uv run pytest
git diff --check
```

The production CLI and dashboard share an operations service. Do not add an alternate path that bypasses repository locks, the sanitized ledger, or uncertain-operation handling. Generated command docs come from `bbackup/cli_metadata.py`; update the owner and run `uv run python scripts/generate_cli_skills.py --check` rather than editing generated output.

## Commits and pull requests

Use conventional commit subjects such as `fix: reject unsafe restore targets` or `docs: clarify replica setup`. Normal batches use a patch increment unless the commit body includes a deliberate `Release-Type: major|minor|patch|none` trailer. See [release versioning](../docs/VERSIONING.md).

Open pull requests against `main`, complete the [pull-request template](pull_request_template.md), and describe the exact checks and platforms exercised. Do not describe fixture or Garage testing as B2/AWS or Ubuntu package qualification. A successful file restore verifies file data; it does not demonstrate application recovery.

Report behavior issues through the [bug report template](ISSUE_TEMPLATE/bug_report.md). Use GitHub's private vulnerability reporting for security issues; see the repository [security policy](../SECURITY.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
