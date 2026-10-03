# Versioning

`VERSION` is the canonical release version. The current
version is `2.0.0-alpha.1`, a preview release; the retained legacy line is
`1.8.6`. The alpha is not production-qualified.

Release-facing references must match it before publishing:

- `bbackup/__init__.py` `__version__`
- `pyproject.toml` `version`
- `pyproject.toml` `requires-python` set to `>=3.12`
- README version badge
- `CHANGELOG.md` current release header and `[Unreleased]` compare target
- generated CLI skills docs
- GitHub-facing Markdown standards and provenance checks

The release workflow accepts alpha, beta, and release-candidate suffixes. A
tag such as `v2.0.0-alpha.1` creates a GitHub prerelease and does not mark it as
the latest stable release. Release tags still have to match `VERSION`, and
their changelog section and generated schemas must be present before publishing.

Normal release batches use a patch bump by default, including commits with
`feat:` subjects. Use a `Release-Type: major|minor|patch|none` trailer in the
commit body when a release needs a non-default impact or an explicit no-bump
decision.

Run the version sync check:

```bash
uv run python scripts/check_version_sync.py
```

Regenerate CLI skills docs after command metadata or package version changes:

```bash
uv run python scripts/generate_cli_skills.py
uv run python scripts/generate_cli_skills.py --check
```

Refresh the locked dependency graph only after dependency or supported Python
version changes:

```bash
uv lock
uv sync --locked
```

## Commit and hook setup

This repo includes local Git hooks in `.githooks/`. Git does not enable tracked
hook directories automatically after clone, so enable them once per checkout:

```bash
git config core.hooksPath .githooks
```

The hooks enforce conventional commit subjects and run release-readiness checks
before push.

## Release flow

1. Update `VERSION`.
2. Sync the version references listed above.
3. Run `uv lock` if dependencies or supported Python versions changed.
4. Add a dated `CHANGELOG.md` section for the release.
5. Run:

```bash
uv sync --locked
uv run python scripts/check_version_sync.py
uv run python scripts/check_markdown_standards.py
uv run python scripts/check_publishing_ready.py
uv run python scripts/generate_cli_skills.py --check
uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py
uv build
uv run python scripts/smoke_installed_artifact.py
uv run pytest
git diff --check
```

6. Commit with a conventional commit message.
7. Tag the release as `v<VERSION>` (for example, `v2.0.0-alpha.1`) and push the tag after CI is green.

Pushing a tag that matches `v*` runs `.github/workflows/release-notes.yml`.
The release job verifies that the tag matches `VERSION`, runs the version and
publishing checks, runs Ruff across the package, selected release scripts, and
tests, runs py_compile, generated-doc checks, pytest, builds release artifacts
with `uv build`, smoke tests the built wheel, and creates a GitHub release from
the matching `CHANGELOG.md` section. The release job fails if the matching
changelog section is missing.
