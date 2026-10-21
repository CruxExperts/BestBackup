# Versioning

`VERSION` is the canonical release version. The current version is `2.0.4`.
The earlier `v2.0.0` and `v2.0.1` tags did not produce release artifacts; the
retained legacy line is `1.8.6`. Stable support is limited to the tested scope
in [`docs/development/version-2.md`](development/version-2.md).

Release-facing references must match it before publishing:

- `bbackup/__init__.py` `__version__`
- `pyproject.toml` `version`
- `pyproject.toml` `requires-python` set to `>=3.12`
- README version badge
- `CHANGELOG.md` current release header and `[Unreleased]` compare target
- generated CLI skills docs
- GitHub-facing Markdown standards and provenance checks

The release workflow accepts alpha, beta, and release-candidate suffixes. A
prerelease tag such as `v2.0.0-alpha.1` is marked as a GitHub prerelease; a
stable tag such as `v2.0.4` is not. Every tag must match `VERSION`, its dated
changelog section, and generated schemas before release validation.

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

6. Create a signed conventional commit for the release candidate.
7. Publish the signed candidate through a pull request, pass the required checks, and merge. Wait for successful trusted CI on the exact resulting default-branch commit.
8. Create a signed annotated tag `v<VERSION>` at that exact commit and push the
   tag. The tag-triggered workflow verifies the signed tag, default-branch ancestry, and exact-commit CI, then builds the wheel and
   source archive, creates native package artifacts, and uploads checksummed
   release inputs; it does not publish a GitHub release.
9. After that exact tag run succeeds, sign its `SHA256SUMS` with the project
   release key. Dispatch `release-notes.yml` on the default branch with the
   successful `build_run_id`, exact tag, and detached signature. The workflow
   binds the run to the tag commit, verifies the complete artifact inventory
   and fixed-fingerprint signature, then locates the complete draft through the
   paginated release inventory, verifies its exact release ID and assets, and
   publishes that ID once. Existing drafts and published releases for the tag
   block a new creation. Future published releases are immutable.
10. Verify the published tag, assets, checksum manifest, and signature. Never
    replace an existing tag or replay a publication after an uncertain result.
