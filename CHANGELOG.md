# Changelog

All notable changes to this project will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). This project uses [semantic versioning](https://semver.org/).

Historical entries preserve the install and packaging guidance that was current
for that release. Use `README.md`, `INSTALL.md`, and `docs/VERSIONING.md` for
current uv-based install, development, and release instructions.

---

## [Unreleased]

---

## [2.0.0-alpha.1] - 2026-10-03

**Keep a copy close. Keep a copy elsewhere.** This preview introduces the
restic-centered bbackup 2.0 workflow for Linux administrators and small teams.
Backblaze B2 and Amazon S3 are supported repository destinations and restore
sources; provider-specific recovery-protection qualification is still underway.

### Added

- Encrypted local restic capture, explicit snapshot replication with independent
  passwords, full-data repository checks, and verified restore into new directories.
- Transactional SQLite capture, native PostgreSQL and locked MySQL/MariaDB
  exports, and PostgreSQL restore into a new database using snapshot-time metadata.
- A mouse and keyboard Textual dashboard sharing the same operations service
  as the strict version-2 JSON CLI, with generated schemas and agent discovery.
- Bounded subprocesses, real process-group cancellation, private operation state,
  repository serialization, and explicit reconciliation of interrupted mutations.
- Signed, GnuPG-encrypted recovery kits and exact historical S3 object-version
  reconstruction that can operate without the original host ledger.
- Native Backblaze B2 inspection using the pinned vendor Python SDK, with SDK
  backoff inside a bounded cancellable process and no persisted credentials.
- A shorter README, a fresh visual identity, a practical documentation hub,
  local-to-cloud recipes, and clear recovery and release boundaries.

### Fixed

- Legacy uploads now report partial failures truthfully and preserve local
  artifacts when all required remote uploads have not succeeded.
- Legacy filesystem restore rejects escaping source paths and returns structured
  failures for unavailable destinations or failed copies.
- Legacy dashboard terminal restoration and operation finalization are more
  reliable; unattended backups no longer assume an interactive terminal.

### Changed

- S3 inventory and recovery use bounded standard SDK retries with five total
  attempts per request. Native B2 inspection delegates protocol retries to b2sdk.
- Dependencies are locked; urllib3 is updated to 2.8.0 for the maintainer's fixes.
- The optional Google Drive OAuth client requires oauthlib 4.0.0 or newer,
  incorporating the upstream fixes for CVE-2026-49264 and CVE-2026-49265.
- Preview features remain under `bbackup production`. Existing 1.x commands and
  configuration remain available while their replacements are completed.

### Release status

This is an **alpha prerelease**, not the production-qualified 2.0 release.
Local restic and Garage S3 capture → copy → check → restore cycles passed.
Database-client fixtures, independent recovery drills, and headless dashboard
checks passed; live B2/AWS and native database qualification remain outstanding.
Docker recovery, retention enforcement, complete UI flows, and Ubuntu packages
are still under development. See [implementation status](docs/development/version-2.md).

## [1.8.6] - 2026-08-04

### Added

- Optional secure Google Drive OAuth setup through `bbman auth-gdrive`, including dry-run and JSON output, redacted diagnostics, and headless SSH callback guidance. The helper configures My Drive remotes; live Google authorization is not exercised in automated tests.
- A repository-wide GitHub Markdown standard, capabilities reference, review checklist, provenance manifest, and CI/pre-push validator now govern public documentation.

### Changed

- Pytest uses bounded xdist auto-workers and isolated Docker integration resources to reduce runtime without overcommitting shared hosts.
- CI and release validation install checksum-pinned restic 0.19.1 so native snapshot lifecycle tests cannot silently skip.

### Fixed

- Snapshot runs apply configured active-repository and include-path retention through scoped, conjunctive tag selectors.
- Snapshot retirement and purge planning isolate snapshots by host and repository, reconcile removed IDs without losing history, and use exact ledger snapshot IDs.
- Generated schedule services share a user-runtime lock so backup, maintenance, and verification jobs do not overlap.
- Google Drive configuration handles rclone continuation responses, bounds continuation calls, loads optional support lazily, and defaults to My Drive without exposing shared-drive selection.

### Security

- Removed the unused GitPython dependency and its `gitdb` and `smmap` transitive packages, eliminating the repository's exposure to their known advisories.
- Raised the `cryptography` dependency floor to 50.0.0, the first release fixing GHSA-g6cj-pr64-35w5 / CVE-2026-69247, and made health/setup checks reject older or prerelease versions.

---

## [1.8.5] - 2026-07-09

### Added

- Native restic snapshot profiles with commands for `snapshot init`, `snapshot run`, `snapshot check`, `snapshot restore`, `snapshot retire`, `snapshot purge-plan`, and `snapshot schedule`.
- Local restic integration coverage that initializes a temporary repository, snapshots a Git repo, checks the repository, restores content, and verifies the state ledger snapshot ID.
- Profile schedule rendering for daily backup, weekly non-destructive maintenance checks, and monthly verification checks with configurable `verification_read_data_subset`.
- Generated CLI skills metadata for the native snapshot command surface.

### Changed

- Snapshot schedule units now render matching service/timer pairs, use `/usr/bin/env bbackup`, and escape systemd percent specifiers.
- Snapshot commands now accept required options from `--input-json` consistently for agent-driven workflows.
- Retired-repo destructive cleanup remains dry-run-first/manual through `snapshot purge-plan`.
- The pytest `integration` marker now covers external-tool integration tests such as Docker and restic.

### Fixed

- Google Drive rclone snapshot profiles without a dedicated `client_id` remain rejected by default, while `allow_default_rclone_drive_client: true` provides a conservative profile-local opt-in.
- Non-dry snapshot operations now enforce profile safety preflight checks before running restic.
- Git repo identity no longer changes on every commit; legacy head-derived repo IDs migrate safely, and unknown active same-path identity conflicts block history merging.
- `list-backups` and restore dry-run planning no longer initialize Docker for file-only planning paths.
- Restore dry-runs now validate filesystem destination and multi-filesystem constraints before returning a success plan.

---

## [1.8.4] - 2026-06-22

### Security

- Raised the `cryptography` runtime dependency floor to `48.0.1` to pick up patched OpenSSL wheels for GHSA-537c-gmf6-5ccf.

### Changed

- Release publishing now uploads only the bbackup wheel and source archive artifacts.

---

## [1.8.3] - 2026-06-22

### Added

- Backup runs now include a compressed `metadata.tar.*` archive of Docker config and network metadata when those metadata directories exist.

### Changed

- CI now tests Python 3.12, 3.13, and 3.14 and uses `actions/checkout@v7`.
- Ruff release checks now include tests and the generated CLI skills script.
- TUI headers now display the package version instead of a hardcoded `1.0.0`.
- Username-only GitHub public-key lookup now uses standard repositories only; explicit Gist IDs remain supported.

### Fixed

- `restore --all --dry-run` now discovers containers, volumes, networks, and filesystem targets from solid archive backup files.
- Non-dry-run filesystem restores now reject missing or ambiguous destinations before any restore mutation runs.

---

## [1.8.2] - 2026-06-09

### Changed

- Simplified the GitHub install and redeploy documentation around the single `uv tool install --force git+https://github.com/CruxExperts/best-backup.git` command.
- Moved the sudo `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` deployment form out of the README and Quickstart path, keeping it only as an advanced system-wide install option in `INSTALL.md`.

---

## [1.8.1] - 2026-06-09

### Fixed

- Solid archive backups now remove the expanded staging directory after the archive is created, so local solid mode leaves a single `.tar.*` artifact instead of both the archive and the unpacked backup tree.
- Rclone uploads of solid archive files now use `rclone copyto` to create one remote file, instead of `rclone copy` creating a destination folder for the file.

---

## [1.8.0] - 2026-06-09

### Added

- `backup_manifest.json` generation for non-cancelled backups, including schema version, source scope, filesystem source paths, volume artifacts, item results, errors, file sizes, and SHA-256 hashes.
- Restore-time manifest verification that fails before mutation when files are missing, changed, unlisted, or outside the backup root.
- Temp-to-final promotion for local, SFTP, rclone, and solid-archive writes so partial uploads are not exposed as completed backups.
- GitHub-facing documentation graphics and Mermaid diagrams for the backup pipeline and release/readiness flow.

### Changed

- Successful encrypted backups now remove plaintext staging and report encryption based on actual output state.
- Rclone remote listing now uses top-level `lsf` output so retention targets backup directories and archive files instead of individual nested files.
- Existing Docker volume restore uses a staging-volume copy preflight before removing the original volume.
- Direct `--paths` and configured filesystem sets now reject duplicate target names that would overwrite each other.
- `init-encryption` now rejects unsupported generated-key passwords before creating key directories.
- Generated asymmetric keys are RSA-4096 only; ECDSA is no longer advertised as a backup encryption option.

### Fixed

- Docker volume backup and restore now propagate failed `docker cp` return codes.
- Failed Docker volume backup attempts remove incomplete local volume artifacts.
- Solid archive failures preserve an existing final archive and remove partial files.
- Partial backup JSON no longer reports encrypted output when encryption was skipped due to item failures.

---

## [1.7.0] - 2026-03-06

### Added

- Solid archive mode: optional single compressed tarball (and optional whole-file encryption) before upload so remotes receive one file instead of many. Config `backup.solid_archive` and CLI `--solid-archive` / `--no-solid-archive`. Restore accepts a backup path that is a file: it unpacks to a temp dir, restores selected items, then cleans up. Listing and retention treat directories and archive files (e.g. `backup_*.tar.gz`, `backup_*.tar.gz.enc`) the same. Staging cleanup runs only after at least one successful upload. See `config.yaml.example` and [docs/architecture.md](docs/architecture.md).

---

## [1.6.0] - 2026-03-04

### Added

- Rclone transfer and concurrency options: config-driven `transfers` and `checkers` for all rclone operations (upload, list, size, purge). Per-remote optional `rclone_options` and optional top-level `rclone.default_options`; recommended defaults 8/8, cap 32. See `config.yaml.example` and [docs/architecture.md](docs/architecture.md#rclone-options).

---

## [1.5.0] - 2026-03-02

### Added

- `is_venv()` helper in `bbackup/management/dependencies.py`: detects whether the current Python is running inside a virtual environment by checking `sys.real_prefix` and `sys.base_prefix`
- PEP 668 guard in `install_python_packages()`: on Ubuntu 22.04+ and Debian 12+ (externally-managed system Python), the function now prints a clear activation message and returns `False` instead of letting pip fail with an unhelpful error
- `bbackup/cli_metadata.py`: unified metadata layer describing CLI commands, parameters, JSON fields, and examples for `bbackup` and `bbman`
- `scripts/generate_cli_skills.py`: generator for `docs/cli-skills.md` and `docs/cli-skills-index.json`, producing a versioned CLI skills catalog from metadata
- `docs/cli-skills.md`: agent-friendly, generated skills catalog for all core `bbackup`/`bbman` commands, with CLI + JSON examples
- Per-command `--skills` flags on core `bbackup`/`bbman` commands to print their skills section from `docs/cli-skills.md`
- CI step in `.github/workflows/ci.yml` that runs `python scripts/generate_cli_skills.py --check` to ensure skills docs are up to date

### Changed

- `bbackup/management/setup_wizard.py`: the "Install missing packages?" branch now delegates to `install_python_packages()` from `dependencies.py` instead of calling `subprocess.run(pip install ...)` directly, consolidating the venv check and error handling in one place
- `INSTALL.md`: pipx is now the recommended install method; dedicated PEP 668 troubleshooting section points users to pipx; manual venv install documented for development use
- `QUICKSTART.md` Step 1: installation example updated to use pipx, with a server-wide pipx variant for shared installs
- `README.md` installation section: updated to show pipx-based install for single user and server-wide setups, pointing to `INSTALL.md` for alternatives
- `README.md` and `docs/README.md`: link the new `docs/cli-skills.md` catalog from the documentation tables

---

## [1.4.0] - 2026-02-28

### Changed

- README overhauled: centered hero header, GitHub Alerts (`[!TIP]`), icon column in feature table, collapsible CLI sections via `<details>`, docs table, and install one-liner at top of page
- Version badge in README updated to reflect current release

---

## [1.3.3] - 2026-02-27

### Added

- AI-agent-friendly JSON I/O layer across all `bbackup` and `bbman` commands
- `--output [text|json]` on every command: emits a versioned JSON envelope to stdout; all diagnostic text goes to stderr
- `--input-json JSON` on every command: accepts all parameters as a single flat JSON object; merges over CLI flags; unknown keys silently ignored (forward-compatible)
- `--dry-run` on `bbackup backup` and `bbackup restore`: resolves targets and returns a plan without executing
- `bbackup skills` and `bbman skills` subcommands for progressive AI agent capability discovery; each skill includes step-by-step guidance and per-step `input_json_schema`
- `BBACKUP_OUTPUT=json` env var: sets JSON mode globally for all subprocesses without passing `--output json` to each command
- `BBACKUP_NO_INTERACTIVE=1` env var: suppresses TUI, prompts, and pagers system-wide
- `--no-interactive` flag on `bbman setup`: skip wizard and return config state as JSON (agent mode)
- `bbackup/cli_utils.py`: shared foundation module with decorators, envelope builder, `merge_json_input`, `json_error`, `flatten_health_tuples`, and semantic exit constants (0-5)
- `bbackup/skills.py`: static skill descriptors for 6 `bbackup` skills and 4 `bbman` skills with per-step `input_json_schema` (JSON Schema)
- 30 new tests in `tests/test_cli.py` across 6 classes: `TestJSONOutputMode`, `TestInputJSON`, `TestSkillsCommand`, `TestDryRun`, `TestEnvVars`, `TestExitCodes`
- Agent integration section in `README.md` with quickstart, envelope spec, env var table, exit code table, skills protocol, and dry-run example

### Changed

- `bbman diagnostics --output` (file path) renamed to `bbman diagnostics --report-file` to free `--output` for the universal format selector
- `bbman --version` now reports the actual package version from `bbackup.__version__` instead of the hardcoded `"1.0.0"`
- All `sys.exit(1)` calls replaced with semantic exit constants: `EXIT_USER_ERROR=1`, `EXIT_CONFIG_ERROR=2`, `EXIT_SYSTEM_ERROR=3`, `EXIT_PARTIAL=4`, `EXIT_CANCELLED=5`
- `bbman health` and `bbman check-deps` JSON output uses named sub-dicts (`{"ok": bool, "message": "..."}`) instead of positional tuples
- `bbackup list-containers` JSON output now includes the `"id"` field for each container
- `bbackup list-backup-sets` JSON output now includes the full `"scope"` dict per set
- Error output from pre-run validation (unknown backup set, missing path, etc.) routed to stderr in text mode and to the `errors` array in JSON mode; stdout is reserved for the JSON envelope only

### Fixed

- TUI no longer blocks agents: `--output json` or `BBACKUP_NO_INTERACTIVE=1` automatically bypasses `run_with_live_dashboard()`
- `backup` command now captures and uses the return value from `runner.run_backup()` for the JSON result payload
- `bbman status` in JSON mode returns structured data directly instead of returning `None`
- `bbman cleanup` JSON output uses `.get("kept", 0)` to guarantee a stable shape regardless of cleanup module version

---

## [1.3.2] - 2026-02-27

### Changed

- Documentation updates aligned with v1.3.0 and v1.3.1: README, INSTALL, QUICKSTART, docs/architecture.md, docs/management.md, docs/encryption.md updated to reflect filesystem backup, restore options, CLI changes, and project structure

---

## [1.3.1] - 2026-02-27

### Added

- 53 unit tests for filesystem backup and restore in `tests/test_filesystem_backup.py`, covering `FilesystemBackup`, `restore_filesystem_path`, `restore_backup` filesystem loop, and `BackupRunner` integration
- 7 real-rsync integration tests in `tests/integration/test_docker_integration.py`: basic backup, exclude patterns, incremental hardlinks, progress callback, and roundtrip restore
- `--yes` / `-y` flag on `maintenance/release.py` for non-interactive (agent/CI) execution

### Fixed

- `FilesystemBackup._build_rsync_cmd` was passing the current run directory as the staging directory to `_find_previous_backup`, causing incremental backups to use `--link-dest` pointing at themselves
- `_find_previous_backup` now accepts `current_run_dir` and excludes it from the candidate scan so a backup never links to itself

---

## [1.3.0] - 2026-02-26

### Added

- Filesystem backup: back up arbitrary host paths and directory trees with rsync, gitignore-style exclude patterns, and incremental `--link-dest` support
- New `bbackup/filesystem_backup.py` module with `FilesystemBackup` class
- `FilesystemTarget` and `FilesystemBackupSet` dataclasses in `config.py`
- `filesystems:` section in `config.yaml.example` with annotated examples
- `--paths`, `--exclude`, `--filesystem-set` options on `bbackup backup`
- `--filesystem`, `--filesystem-destination` options on `bbackup restore`
- `bbackup list-filesystem-sets` subcommand
- Filesystem panel in live TUI dashboard (third column alongside containers and volumes)

---

## [1.2.1] - 2026-02-26

### Changed

- Expanded test coverage from 59% to 76% across all core modules
- Resolved all ruff lint violations in the `bbackup` package

### Fixed

- `BackupRunner._parse_rsync_progress` hoisted from closure to method for reuse
- `BackupStatus.update()` no longer receives `transfer_speed` as a kwarg (set directly)

---

## [1.2.0] - 2026-02-26

### Added

- Full CI testing suite: 289 unit tests covering all `bbackup/` modules
- `scripts/run_tests.py` agentic sandbox test runner with Docker isolation and self-healing debug loop
- `Dockerfile.test` for sandboxed test execution
- `requirements-dev.txt` with pytest, pytest-cov, pytest-mock
- GitHub Actions CI workflow with unit and integration test jobs
- LICENSE file (MIT)
- CHANGELOG.md following Keep a Changelog format
- Community health files: CONTRIBUTING.md, CODE_OF_CONDUCT.md, issue templates, PR template
- Project identity system: copyright footer stamped across all public-facing docs
- `maintenance/release.py` automated release workflow with doc validation
- `maintenance/check_docs.py` stale-doc detection with agent-driven auto-update support
- `maintenance/bump_version.py` conventional-commit version bumping

---

## [1.1.0] - 2026-02-26

### Added

- Rich TUI with BTOP-style live dashboard and real-time transfer metrics (speed, bytes, file count, ETA)
- Docker backup for containers, volumes, networks, and configuration metadata
- Incremental backups using rsync `--link-dest` (hardlinks unchanged files instead of copying)
- AES-256-GCM symmetric encryption at rest
- RSA-4096 / ECDSA P-384 asymmetric encryption with public/private key separation
- GitHub key hosting: reference public keys by `github:USERNAME/gist:ID` shortcut
- Remote storage: Google Drive via rclone, SFTP via paramiko, local directory
- Time-based backup rotation with daily/weekly/monthly retention and storage quota enforcement
- Full restore of containers, volumes, and networks with optional rename on restore
- Backup sets: named container groups defined in config for repeatable runs
- Selective backup: `--config-only`, `--volumes-only`, `--no-networks` flags
- `bbman` management CLI: setup wizard, health checks, dependency management, diagnostics, self-update
- Interactive container selection with TUI dialog
- Keyboard controls in TUI: Q (quit), P (pause/resume), S (skip), H (help)
- YAML configuration with priority chain and CLI override support
- Rotating file logger via `get_logger()` factory

### Changed

- Project structured for standalone GitHub distribution

---

The `Unreleased` comparison is a live `HEAD` link for navigation, not an immutable historical citation.

[Unreleased]: https://github.com/CruxExperts/best-backup/compare/v2.0.0-alpha.1...HEAD
[2.0.0-alpha.1]: https://github.com/CruxExperts/best-backup/compare/v1.8.6...v2.0.0-alpha.1
[1.8.6]: https://github.com/CruxExperts/best-backup/compare/v1.8.5...v1.8.6
[1.8.5]: https://github.com/CruxExperts/best-backup/compare/v1.8.4...v1.8.5
[1.8.4]: https://github.com/CruxExperts/best-backup/compare/v1.8.3...v1.8.4
[1.8.3]: https://github.com/CruxExperts/best-backup/compare/v1.8.2...v1.8.3
[1.8.2]: https://github.com/CruxExperts/best-backup/compare/v1.8.1...v1.8.2
[1.8.1]: https://github.com/CruxExperts/best-backup/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/CruxExperts/best-backup/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/CruxExperts/best-backup/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/CruxExperts/best-backup/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/CruxExperts/best-backup/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/CruxExperts/best-backup/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/CruxExperts/best-backup/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/CruxExperts/best-backup/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/CruxExperts/best-backup/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/CruxExperts/best-backup/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/CruxExperts/best-backup/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/CruxExperts/best-backup/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/CruxExperts/best-backup/releases/tag/v1.1.0

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
