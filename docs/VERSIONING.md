# Versioning

`VERSION` is the canonical release version. The current version is `2.0.5`.
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
stable tag such as `v2.0.5` is not. Every tag must match `VERSION`, its dated
changelog section, and generated schemas before release validation.

Normal release batches use a patch bump by default, including commits with
`feat:` subjects. Release Please uses `always-bump-patch`; use its supported
`Release-As: X.Y.Z` commit-body trailer to request an exact non-default version.
Historical `Release-Type: major|minor|patch|none` trailers are human review notes,
not instructions parsed by Release Please. Defer or close a draft release PR
when its changes are not ready for a release; publication is always deliberate.

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

### Prepare the release pull request

**Activation is blocked as of 2026-10-23.** The SHA-pinned
`.github/workflows/release-please.yml` has an explicit false job guard. Its
triggers are defined for `main` pushes and manual dispatch, but the Action is
not executed. The audited v5.0.0 lock includes `handlebars@4.7.8`, affected by
[critical CVE-2026-33937](https://github.com/handlebars-lang/handlebars.js/security/advisories/GHSA-2w6w-674q-4c4q)
and [critical CVE-2026-106445](https://github.com/advisories/GHSA-p8wg-vrv2-v86f).
The latter requires at least 4.7.10. Other transitive advisories are also
present. Exact-lock OSV and npm registry checks confirm the findings; the
upstream `main` lock remains affected. This is a dependency finding, not a
claim that this repository was exploited.

Before activation, review a patched immutable upstream release, its lockfile
**and rebuilt bundled Action**, rerun advisory checks, replace the SHA, and
restore the job condition to permit only `refs/heads/main`. Then allow Actions
to create PRs in repository settings and verify the first draft. No repository
permission setting was changed during this blocked setup. To roll back the
setup, revert its task commit; no tag or published artifact needs removal.

The configuration below describes the intended behavior after activation.
Its root manifest starts at `2.0.5`; it proposes
the next version in `VERSION` and `.release-please-manifest.json` through a
**draft PR**. It does not create tags, GitHub releases, or merge PRs. The
existing signed-tag and signed-checksum publication flow remains authoritative.

Release Please deliberately leaves the curated changelog and generated files
to a maintainer. Its bot-created commit is unsigned. Preserve the default
branch's required signatures: create a candidate branch from current `main`,
apply the draft's changes, then prepare and sign them locally. Do not merge
the unsigned draft or relax branch protection. For example, after inspecting
the draft and choosing its exact branch name:

```bash
git fetch origin
git switch -c release/candidate origin/main
git diff origin/main...origin/RELEASE_BRANCH -- VERSION .release-please-manifest.json | git apply
```

Run the preparation helper on that candidate branch:

```bash
uv run python scripts/prepare_release.py
```

This synchronizes package metadata, the root lockfile version, the README
badge, this document's current version, and the existing CLI generators. It
moves the complete curated `[Unreleased]` content to the dated candidate
section, preserving historical release and support evidence. Review that
content and add any missing notes from the draft PR before marking it ready.
The dependency versions remain unchanged.

Commit the prepared files with the required project signing key and push the
candidate branch. Open a normal PR containing that signed commit, referencing
the bot draft; close the bot draft as superseded once the candidate merges.
No private signing key or personal token is installed in
Actions. Release Please uses `GITHUB_TOKEN`; GitHub does not automatically run
PR workflows for its bot-created commits. The normal signed candidate PR runs
PR CI. If needed, explicitly dispatch the existing CI workflow for that branch:

```bash
gh workflow run ci.yml --ref RELEASE_BRANCH
```

Verify the run's head SHA matches the final prepared commit. Draft creation
alone is not release readiness. Repository settings must allow GitHub Actions
to create PRs; default workflow token permissions may remain read-only because
this workflow requests only contents and pull-request writes. Required branch
protection, signatures, review conversations and checks still apply.

### Validate and publish deliberately

1. Review the draft's proposed `VERSION` and manifest, or update both for a manually selected release.
2. Run the preparation command above, then review all synchronized references and curated notes.
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
