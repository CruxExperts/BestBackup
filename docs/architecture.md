# Architecture

This page describes the version 2 operations preview and the retained 1.x implementation. The v2 command is `bbackup production`; the legacy Docker/YAML interface remains available while the preview is qualified.

## Version 2 preview architecture

Version 2 uses one operation service for CLI and dashboard work. Portable JSON policy names sources, jobs, local repositories, and replicas; a separate private bindings file supplies repository locations, password-file paths, and native database connection-file references.

### Capture and encryption

File trees are streamed into encrypted restic repositories. SQLite uses its backup API so an open writer's uncommitted changes are excluded. PostgreSQL produces a custom-format export; MySQL and MariaDB require an explicit read-lock policy and refuse unsupported table engines. Native database adapters are fixture-tested; live capture and restore remain release gates.

Restic encrypts repository content using the password file bound to that repository. Local capture completes before a job copies the successful full snapshot ID to its configured replicas. Each repository can use independent password material. Backblaze B2 through the S3 API and Amazon S3 are configurable repository destinations and restore sources; provider protection and recovery qualification are pending.

### Operation service and recovery

CLI and dashboard requests pass through a shared service, bounded subprocess runner, SQLite operation ledger, and cross-process repository lock. The ledger stores sanitized lifecycle events, not command lines, environment values, raw output, or secrets. Interrupted mutations stay uncertain and require administrator reconciliation; bbackup never automatically replays them.

File restore requires a new directory and asks restic to verify restored data. Database restore supports only trusted PostgreSQL custom archives and creates a new database using snapshot-time locale metadata. File or database verification does not prove application startup or application-level correctness.

Cloud recovery kits inventory exact object versions, bind the provider endpoint and complete repository/snapshot identities, and use detached OpenPGP signing plus encryption. Checkpoint creation requires a full restic check and an administrator-coordinated quiet window across every writer. Lifecycle inspection is read-only and does not establish that a provider protects data for thirty days.

### Interfaces and scope

The `bbackup production` CLI is strict and schema-discoverable; the Textual dashboard provides mouse and keyboard job operation. Generated units are disabled when rendered. The preview currently reports test evidence, not production qualification. See [version 2 development](development/version-2.md) for current support boundaries and release gates, [agent integration](AGENT_INTEGRATION.md) for the JSON contract, and [cloud storage](cloud-storage.md) for B2/S3 setup.

The remaining module and configuration sections below describe the retained 1.x Docker/filesystem workflow. Their YAML settings, archive model, and AES/RSA file encryption do not configure the v2 preview.

---

## Technology stack

| Component | Library / tool | Version |
|---|---|---|
| Language | Python | 3.12+ |
| CLI framework | Click | 8.1.7+ |
| Terminal UI | Rich | 13.7.0+ |
| Docker integration | docker-py SDK | 7.0.0+ |
| Config format | PyYAML | 6.0.1+ |
| SFTP | paramiko | 3.4.0+ |
| Encryption | cryptography | 50.0.0+ |
| HTTP (key fetching) | requests | 2.31.0+ |
| Volume backup | rsync | system |
| Metadata/archive handling | tar | system |
| Cloud storage | rclone | optional |

---

## Documentation visuals

GitHub-facing documentation uses Markdown-native visuals wherever possible:

- Use Mermaid fenced code blocks for flow charts and release/readiness diagrams so charts render directly in GitHub pull requests, releases, and Markdown files.
- Keep each diagram self-describing with clear node labels and avoid depending on color alone.
- Store bitmap assets under `docs/assets/` and reference them with relative paths plus specific alt text.
- Keep generated images free of embedded text so README headings, captions, and links remain searchable and accessible.

The README uses a checked-in vector banner, and the preview dashboard image is a captured interface example. Keep product claims and restore boundaries in text so readers do not have to infer behavior from a graphic.

---

## Backup strategy

The tool uses two separate mechanisms depending on what it is backing up.

**Volumes** go through rsync running inside a temporary Alpine container that mounts the target volume. This handles large datasets well and supports incremental mode via `--link-dest`, where unchanged files become hardlinks to the previous backup instead of copies.

**Metadata** (container configs, network configs, logs) goes through `tar` with configurable compression. These are small and benefit more from good compression than from rsync's delta algorithm.

The two strategies produce separate artifacts that are bundled into a backup
directory named `backup_YYYYMMDD_HHMMSS` under `backup.local_staging` (the
starter default is `/tmp/bbackup_staging`) before encryption and remote upload.

---

## Module responsibilities

### `bbackup/cli.py`

Click entry point for the `bbackup` command. Handles argument parsing and delegates to the appropriate class. Commands: `backup`, `restore`, `list-containers`, `list-backup-sets`, `list-backups`, `list-remote-backups`, `list-filesystem-sets`, `init-config`, `init-encryption`, `skills`.

Every command accepts `--output [text|json]`, `--input-json JSON`, and `backup`/`restore` also accept `--dry-run`. In JSON mode the command emits the standard envelope to stdout; all diagnostic text goes to stderr. The `BBACKUP_OUTPUT` and `BBACKUP_NO_INTERACTIVE` env vars apply globally.

### `bbackup/cli_utils.py`

Shared foundation for the AI-agent-friendly JSON I/O layer. Contains:

- `output_option`, `input_json_option`, `dry_run_option` -- Click decorators applied to every command.
- `merge_json_input(ctx, json_str)` -- parses `--input-json` and overwrites matching `ctx.params` keys.
- `render_output(data, fmt, command, ...)` -- writes the standard JSON envelope to stdout when `fmt == "json"`.
- `json_error(command, message, exit_code, fmt)` -- emits an error envelope (JSON mode) or stderr line (text mode) then exits.
- `flatten_health_tuples(raw)` -- converts `Tuple[bool, str]` / `Tuple[bool, List, List]` values to named dicts for JSON consumers.
- Exit constants: `EXIT_SUCCESS=0`, `EXIT_USER_ERROR=1`, `EXIT_CONFIG_ERROR=2`, `EXIT_SYSTEM_ERROR=3`, `EXIT_PARTIAL=4`, `EXIT_CANCELLED=5`.
- Env var names: `BBACKUP_OUTPUT_ENV`, `BBACKUP_NO_INTERACTIVE_ENV`.

### `bbackup/skills.py`

Static skill descriptors for AI agent capability discovery. Contains `BBACKUP_SKILLS` and `BBMAN_SKILLS` dicts keyed by skill id, and `get_skill(cli, skill_id)` accessor.

Each skill entry includes: `id`, `summary`, `workflow` (ordered command list), `steps` (each with `command`, `description`, `optional_flags`, `valid_values`, `input_json_schema`), `examples`, `output_format`, `exit_codes`.

`get_skill(cli, None)` returns a level-0 overview with `agent_hint` and a compact skills list (80-90% token savings vs loading full `--help` trees). `get_skill(cli, skill_id)` returns the full step-by-step descriptor.

### JSON envelope contract

All commands emit exactly one JSON object to **stdout** in JSON mode:

```json
{
  "schema_version": "1",
  "command": "backup",
  "success": true,
  "data": { "...command-specific fields..." },
  "errors": []
}
```

`schema_version` bumps only on breaking field removals or renames; additive fields are always backward-compatible. All diagnostic and progress text is routed to **stderr** so stdout can be parsed unambiguously.

### `bbackup/config.py`

Loads and validates the YAML config file. Discovers config location using the priority chain below. Contains all dataclasses: `BackupScope`, `BackupSet`, `RemoteStorage`, `RetentionPolicy`, `IncrementalSettings`, `EncryptionSettings`, `Config`. CLI overrides are merged on top of file defaults.

### `bbackup/snapshot.py`

Owns native restic snapshot profiles. It discovers Git repositories, reconciles
stable repository IDs in the state ledger, builds tagged backup commands, and
records successful snapshot IDs. Configured retention is emitted as a separate
`restic forget` command per active repository or include-path. It filters by the
configured host plus stable bbackup/profile/scope/ID tags and groups only by
host, so mutable user tags cannot split retention policy. Retired repositories
are never selected automatically.
Retired cleanup uses `purge-plan` with exact state-ledger snapshot IDs and
remains dry-run-first.
Generated systemd services share a user-runtime `flock` lock.

### `bbackup/docker_backup.py`

Talks to the Docker API. Starts temporary Alpine containers, mounts target volumes, and runs rsync or tar inside them. Also inspects container and network configs. All Docker API errors are caught and reported via `BackupStatus`.

### `bbackup/backup_runner.py`

Orchestrates the full backup workflow:

```text
init → select items → prepare staging dir
  → backup configs → backup volumes → backup networks
  → backup filesystem paths (if filesystem_targets provided)
  → write backup_manifest.json with file sizes and SHA-256 hashes
  → [if solid_archive] create_solid_archive → optionally encrypt whole file → upload file → cleanup staging only after success
  → [else] encrypt dir (if enabled) → upload dir to remotes
  → rotate old backups
  → report
```

Reads and writes `BackupStatus` throughout so the TUI stays current.

### `bbackup/filesystem_backup.py`

Backs up arbitrary host filesystem paths and directory trees using rsync directly (no Docker). Key behaviors:

- Source path is rsynced to `backup_dir/filesystems/<target_name>/`
- Exclude patterns are written to a temp file and passed via `--exclude-from`; the temp file is always deleted in a `finally` block regardless of success or failure
- Incremental mode uses `--link-dest` pointing at the most recent previous `backup_YYYYMMDD_HHMMSS` sibling directory that contains the same target name. The current run directory is always excluded from the scan so a backup never links to itself
- Progress output is streamed line-by-line to the caller's callback for live TUI updates

### `bbackup/archive.py`

Solid-archive support: creates a single compressed tarball from a backup directory and optionally encrypts the whole file. Exposes `create_solid_archive()`, `unpack_solid_archive()`, and naming helpers `is_solid_archive_name()`, `strip_solid_archive_suffix()` used by list/rotation/restore. Archive creation writes `.partial` files and promotes them to the final path only after tar/encryption succeeds. When solid archive encryption succeeds, plaintext staging is removed before upload.

### `bbackup/manifest.py`

Every non-cancelled backup writes `backup_manifest.json` before encryption/upload. The manifest records schema version, backup name, tool version, requested source scope, filesystem source paths, volume artifact names, encryption mode, item results, errors, file sizes, and SHA-256 hashes. When Docker config or network metadata exists, backup runs also write a compressed `metadata.tar.*` archive before the manifest so verification covers that archive. Restore verifies the manifest when present and fails before mutation on missing, changed, unlisted, or escaping file paths. Backups without a manifest are treated as legacy backups.

### `bbackup/restore.py`

Reads a backup directory or a solid archive file and restores containers,
volumes, networks, and filesystem paths. When the backup path is a file (e.g.
`.tar.gz` or `.tar.gz.enc`), unpacks to a temp dir, runs the same restore logic,
then removes the temp dir. Supports renaming on restore (`--rename old:new`).
Handles decryption (per-dir or whole-archive for solid archives) and manifest
verification before restore. The CLI `--dry-run` path stops after selecting
targets, so it does not verify manifest hashes, Docker access, or destination
permissions. Existing Docker volume replacement uses a staging-volume copy
preflight so a failed first copy leaves the existing volume untouched; Docker
does not support atomic volume rename, so final replacement is destructive
after preflight. Container/network replacement can stop or remove existing
objects, and filesystem restore uses `rsync --delete`.


### `bbackup/tui.py`

Two main classes:

- `BackupStatus`: thread-safe state object (lock-protected). Tracks current action, per-item status, errors, pause/cancel flags, transfer metrics.
- `BackupTUI`: the live dashboard. Renders a BTOP-style layout at 4 fps using Rich's `Live`. Handles keyboard input (Q, P, S, H). Also provides interactive selection dialogs for containers, backup sets, and scope options.

### `bbackup/remote.py`

Abstracts three upload targets behind a common interface: local filesystem
(shutil), rclone (subprocess), and SFTP (paramiko). Each remote is tried
independently, so one failure does not abort others. Uploads write to
`.partial` destinations first and promote to final paths only after the copy
succeeds; a failed remote can leave an overall partial result while another
remote succeeds. A local path or endpoint on the same host is not off-host
protection, and bbackup does not detect that topology. Upload progress feeds
into `BackupStatus`.

### `bbackup/rotation.py`

Applies daily/weekly/monthly retention. Calculates which backups to keep and removes the rest, oldest-first. Also checks storage quota thresholds and triggers cleanup when the limit is approached.

### `bbackup/encryption.py`

Two encryption modes:

- **Symmetric**: AES-256-GCM with a per-file random IV. One key encrypts and decrypts.
- **Asymmetric**: RSA-OAEP wrapping an AES session key. The public key encrypts, the private key decrypts.

Public keys can be referenced by path, URL, or GitHub shortcut (`github:USER/gist:ID`). Fetched keys are cached at `~/.cache/bbackup/keys/` with mode 600.

### `bbackup/logging.py`

`get_logger(name)` returns a properly named logger for any module. `setup_logging(config)` configures a `RotatingFileHandler` at `~/.local/share/bbackup/bbackup.log` on startup.

### `bbackup/management/`

13-module subpackage powering the `bbman` command. Each module handles one lifecycle concern:

| Module | Responsibility |
|---|---|
| `first_run.py` | Detect first run, locate config path |
| `setup_wizard.py` | Interactive first-time setup; delegates package installs to `dependencies.py` |
| `health.py` | Docker, system tool, config health checks |
| `diagnostics.py` | Generate diagnostic report |
| `dependencies.py` | Check and install missing packages; `is_venv()` guard prevents pip from running on an externally-managed system Python (PEP 668) |
| `gdrive_auth.py` | Google Desktop app OAuth helper that writes rclone Drive remotes without owning backup transfers |
| `updater.py` | Self-update from remote repo |
| `version.py` | Version detection, Git-compatible checksums |
| `repo.py` | Manage update source URL |
| `config.py` | Management-layer config (separate from backup config) |
| `status.py` | Backup history and totals |
| `cleanup.py` | Staging dir and log cleanup |
| `utils.py` | Shared utilities |

### `bbackup/cli_metadata.py`

Structured metadata registry describing all `bbackup` and `bbman` commands, parameters, JSON fields, and examples. Drives the JSON skills output, the generated `docs/cli-skills.md` catalog, and future tooling.

---

## Configuration system

### Discovery order

1. `~/.config/bbackup/config.yaml`
2. `~/.bbackup/config.yaml`
3. `/etc/bbackup/config.yaml`
4. `./config.yaml`

### Override chain

Config file provides defaults. CLI arguments override them at runtime. Interactive selection (when `--interactive` is active, which is the default) allows further runtime changes before the backup starts.

### Configuration sections

| Section | Purpose |
|---|---|
| `backup` | Staging dir, backup sets, default scope |
| `remotes` | Remote storage destinations |
| `rclone` | Optional default `transfers`/`checkers` for all rclone remotes |
| `retention` | Daily/weekly/monthly counts, quota |
| `incremental` | Enable rsync `--link-dest` mode |
| `logging` | Log path, level, rotation |
| `encryption` | Method, key paths, algorithm |
| `docker` | Socket path, timeout |

### Rclone options

For remotes with `type: rclone`, you can tune transfer concurrency so uploads use bandwidth more effectively. Options apply to all rclone operations (upload, list, size, purge) for that remote.

- **Per-remote:** Under the remote, set optional `rclone_options` with `transfers` (parallel file transfers) and `checkers` (parallel checkers for listing). Both are integers from 1 to 32; recommended defaults are 8 and 8.
- **Global default:** Top-level `rclone.default_options` sets defaults for all rclone remotes; per-remote `rclone_options` overrides it.
- **Omitted:** If neither is set, bbackup uses transfers=8 and checkers=8.

### Google Drive rclone auth

`bbman auth-gdrive` is a management helper for creating the rclone Drive remote
that bbackup later uses. It lives in `bbackup/management/gdrive_auth.py`, uses
the optional `gdrive-auth` dependency extra, and runs Google Desktop app
loopback OAuth through `google-auth-oauthlib`.

The helper validates an installed-app `client_secret.json`, requires a returned
refresh token, converts credentials to rclone token JSON, and calls rclone RC
`config/create` or `config/update` through a temporary Unix socket inside a
mode-0700 directory. It does not hand-edit `rclone.conf`, does not implement
native Google Drive uploads, and does not replace `bbackup/remote.py`; backup
and listing operations continue to use rclone as an external transfer engine.

---

## BackupStatus data flow

`BackupStatus` is the shared state object between the backup worker thread and the TUI render thread. The worker calls `status.update()`, `status.add_error()`, and updates per-item dicts. The TUI reads these on every render cycle (4 fps). A `threading.Lock` protects all writes.

---

## Release readiness flow

The release flow checks the canonical version, builds artifacts, runs installed smoke tests and the full suite, then prepares the GitHub release only after validation passes.


```mermaid
flowchart LR
    version[VERSION<br/>single source of truth]
    sync[Version sync check<br/>package • README • CLI docs]
    build[Build artifacts<br/>wheel • sdist]
    smoke[Installed smoke test<br/>CLI entry points]
    tests[Test suite<br/>pytest • py_compile]
    publish[GitHub release prep<br/>PR • tag after CI]

    version --> sync
    sync --> build
    build --> smoke
    smoke --> tests
    tests --> publish
```

---

## Known gaps

- S and H keyboard shortcuts print to console rather than opening a modal overlay. Modal implementation is planned.

---

## Repository structure

```text
best-backup/
├── bbackup/                # Main package
│   ├── cli.py
│   ├── config.py
│   ├── docker_backup.py
│   ├── filesystem_backup.py
│   ├── backup_runner.py
│   ├── restore.py
│   ├── tui.py
│   ├── remote.py
│   ├── rotation.py
│   ├── encryption.py
│   ├── logging.py
│   ├── bbman_entry.py
│   └── management/         # bbman subpackage
├── docs/                   # Reference documentation
│   ├── architecture.md     # This file
│   ├── management.md       # bbman reference
│   ├── encryption.md       # Encryption guide
│   └── dev/               # Internal dev notes and test logs
├── scripts/                # Test and utility scripts
├── bbackup.py              # bbackup entry point
├── bbman.py                # bbman entry point
├── config.yaml.example
├── pyproject.toml
└── uv.lock
```

---

Back to [README.md](../README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
