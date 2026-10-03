# Management CLI (bbman)

Reference for the retained version 1.x `bbman` command, which handles setup,
health, updates, and maintenance for the Docker/filesystem workflow.

---

## Overview

`bbman` remains the management command for the 1.x YAML configuration and
Docker/filesystem workflow. The version 2 preview uses `bbackup production`
with separate strict JSON policy and private host bindings; `bbman setup` does
not configure it. Start with the [v2 quick start](../QUICKSTART.md) or read the
[version 2 architecture and limits](development/version-2.md).

For existing 1.x deployments, `bbman` is separate from `bbackup`. It handles
first-run setup, dependency checks, updates, diagnostics, and cleanup. The
legacy command interface supports structured output and single-object JSON
input where each command accepts those options; check that command's `--help`.

Every `bbman` command supports `--output json` for machine-readable output and `--input-json '{...}'` for single-object parameter passing. Set `BBACKUP_OUTPUT=json` to apply JSON mode globally to all subprocesses.

---

## Legacy 1.x command reference

### `bbman setup`

Interactive first-time setup wizard. Run this before anything else:

```bash
bbman setup
bbman setup --output json       # JSON result from the interactive wizard
```

Checks Docker access, verifies `rsync` and `tar` are installed, installs any
missing Python packages, and creates `~/.config/bbackup/config.yaml` if it does
not exist. The optional encryption-key step defaults to no.

`--no-interactive` is not a successful setup mode. It reports that interactive
setup is required and exits with an error; use `bbman setup` for first-run
configuration.

---

### `bbman health`

Comprehensive health check.

```bash
bbman health
bbman health --output json    # Machine-readable result
```

Checks:
- Docker daemon is running and your user has socket access
- System tools: `rsync`, `tar`, `rclone`
- Python dependencies match `pyproject.toml`
- Config file parses without errors
- Staging and log directories are writable
- Every configured native `snapshot_profiles` profile passes its restic,
  repository, password-file, cache/state, and remote checks

JSON output uses named fields (`{"ok": true, "message": "..."}`) rather than
positional tuples, making it straightforward for agents to check individual
components. An example snapshot profile in the starter template can make
health report `unhealthy` until its restic repository, password file, and tools
are configured; remove that unused example when native snapshots are not part
of the deployment.

---

### `bbman check-deps`

Check (and optionally install) missing dependencies.

```bash
bbman check-deps              # Report only
bbman check-deps --install    # Install missing packages
bbman check-deps -i           # Shorthand
bbman check-deps --output json
```

On Ubuntu 22.04+ and Debian 12+ the system Python is externally managed (PEP 668). If you are not running inside a virtual environment, `--install` will print a warning and skip the install rather than failing with a pip error. Activate the bbackup venv first:

```bash
source ~/best-backup/.venv/bin/activate
bbman check-deps --install
```

---

### `bbman validate-config`

Parse and validate the config file.

```bash
bbman validate-config
bbman validate-config --output json
```

Reports YAML syntax errors, missing required fields, invalid paths, and unrecognized remote types. Shows a summary of what was parsed successfully.

---

### `bbman auth-gdrive`

Authorize Google Drive and create or update a dedicated rclone Drive remote.
bbackup still uses rclone for transfers; this command only performs OAuth setup
and writes the rclone remote configuration.
The helper targets My Drive by default; shared-drive selection is not exposed
by this command.

For a checked-out project, select the optional project extra and run through
that environment:

```bash
uv sync --locked --extra gdrive-auth
uv run bbman auth-gdrive --client-secrets client_secret.json --dry-run --output json
uv run bbman auth-gdrive --client-secrets client_secret.json --remote bbackup-gdrive
```

For an isolated uv tool install, do not run `uv sync` from another directory;
install the helper packages into the tool:

```bash
uv tool install --force \
  --with google-auth-oauthlib \
  --with oauthlib \
  --with requests-oauthlib \
  git+https://github.com/CruxExperts/best-backup.git
bbman auth-gdrive --client-secrets client_secret.json --dry-run --output json
```

Required:

- `--client-secrets PATH`: Google Desktop app OAuth client secrets JSON.

Defaults:

- `--remote bbackup-gdrive`
- `--scope drive`
- `--port 53682`
- `--timeout 300`

Safety behavior:

- `--dry-run` validates the secrets file and planned rclone setup without OAuth
  or rclone writes.
- Existing rclone remotes are preserved unless `--force` is passed.
- `--no-open-browser` uses the same loopback OAuth flow but leaves URL opening
  to the user. For an SSH-hosted install, first connect from the workstation
  with `ssh -L 53682:127.0.0.1:53682 user@server`, then run the command with
  `--no-open-browser --port 53682` inside that session and open its printed URL
  on the workstation. It requires text output during OAuth; JSON mode suppresses
  prompts to preserve valid machine-readable output.
- The command never accepts a raw `--client-secret` value and redacts client
  secrets, access tokens, refresh tokens, and token JSON from output.
- The temporary rclone RC server uses a Unix socket inside a mode-0700
  directory. Google credentials appear only in its local request body, never in
  process arguments or a TCP listener.

JSON example:

```bash
bbman auth-gdrive --input-json '{"client_secrets":"client_secret.json","dry_run":true}' --output json
```

Configure bbackup to use the resulting rclone remote:

```yaml
remotes:
  gdrive:
    enabled: true
    type: rclone
    remote_name: bbackup-gdrive
    path: /backups/docker
```

---

### `bbman status`

Show backup history.

```bash
bbman status
bbman status --output json
```

Prints total backup count, combined size, how many are encrypted, and the most recent backup timestamps. In JSON mode, raw statistics are returned directly rather than a formatted display.

---

### `bbman cleanup`

Remove old staging files and logs.

```bash
bbman cleanup
bbman cleanup --staging-days 7    # Keep staging files newer than N days
bbman cleanup --log-days 30       # Keep log files newer than N days
bbman cleanup --yes               # Skip confirmation prompt
bbman cleanup --no-backups        # Do not touch old backup directories
bbman cleanup --no-temp           # Skip temporary file cleanup
bbman cleanup --yes --output json # Agent-friendly: skip prompt + JSON result
```

In JSON mode, confirmation is automatically skipped (equivalent to `--yes`).

---

### `bbman diagnostics`

Generate a diagnostic report.

```bash
bbman diagnostics
bbman diagnostics --report-file report.txt    # Save to file
bbman diagnostics --output json               # JSON output
```

Includes system info, Docker version, Python environment, config summary, and recent errors from the log file. Useful for bug reports.

Note: the save-to-file option is `--report-file` (not `--output`, which is reserved for the output format selector).

---

### `bbman check-updates`

Check whether the installed version is behind the configured repository.

```bash
bbman check-updates
bbman check-updates --branch main
bbman check-updates --output json
```

Uses SHA-256 checksums (Git-compatible) to compare local files against the remote tree. Reports which files have changed without downloading anything.

---

### `bbman update`

Pull updates from the repository.

```bash
bbman update
bbman update --method git       # Use git pull
bbman update --method download  # Download changed files directly
bbman update --yes              # Skip confirmation
bbman update --yes --output json
```

Creates a backup of the current installation before applying changes. Verifies checksums after updating. In JSON mode, confirmation is automatically skipped.

---

### `bbman repo-url`

Manage the repository URL used for update checks and downloads.

```bash
bbman repo-url                              # Show current URL and its source
bbman repo-url --url https://github.com/CruxExperts/best-backup
bbman repo-url --output json
```

The URL is resolved in this order:

1. `BBACKUP_REPO_URL` environment variable
2. `repo_url` in `~/.config/bbackup/management.yaml`
3. Default compiled into `bbman.py`

---

### `bbman run`

Launch `bbackup` through the wrapper.

```bash
bbman run backup --containers myapp
bbman run list-containers
bbman run restore --backup-path /path/to/backup --all
bbman run backup --containers myapp --output json   # JSON envelope wraps bbackup output
```

Arguments pass through directly to `bbackup`. With `--output json`, the subprocess output is captured and wrapped in the standard JSON envelope.

---

### `bbman skills`

Discover what `bbman` can do. Useful for AI agents performing progressive capability discovery.

```bash
bbman skills                     # Level-0: list all skill ids and summaries (JSON)
bbman skills setup               # Level-1: step-by-step guide + JSON schemas (JSON)
bbman skills auth-gdrive
bbman skills maintenance
bbman skills updates
bbman skills dependencies
bbman skills --format markdown   # Full Markdown skills catalog (all commands)
```

Level-0 JSON output includes an `agent_hint` field pointing agents to the recommended env vars and `--input-json` usage.

---

## Agent integration

`bbman` is natively compatible with AI agents:

```bash
# Set globally once
export BBACKUP_OUTPUT=json
export BBACKUP_NO_INTERACTIVE=1

# Discover capabilities (JSON)
bbman skills

# Discover capabilities (Markdown)
bbman skills --format markdown

# Per-command skills docs
bbman health --skills

# Run with structured input/output
bbman health --output json
bbman cleanup --input-json '{"staging_days": 14, "yes": true}' --output json
bbman check-updates --output json
```

Every command returns the standard JSON envelope:

```json
{
  "schema_version": "1",
  "command": "health",
  "success": true,
  "data": { "docker": {"ok": true, "message": "..."}, "overall": "healthy" },
  "errors": []
}
```

The separate version 2 interface uses strict input schemas and a different result envelope; see [agent integration](AGENT_INTEGRATION.md).

---

## Management config file

`~/.config/bbackup/management.yaml` controls wrapper behavior:

```yaml
repo_url: "https://github.com/CruxExperts/best-backup"
auto_check_updates: true
check_interval_days: 7
auto_setup_on_first_run: true
health_check_before_run: false
update_method: "git"           # git, download, or manual
```

---

## First-run detection

Run `bbman setup` explicitly on first use to create the initial config and mark
first-run setup complete. The wizard is interactive; `bbman setup --no-interactive`
returns an error stating that interactive setup is required. If config is
missing, validation commands report the missing state and point back to
`bbman setup`. `bbackup init-config` is a separate template writer and
overwrites the config path without an overwrite prompt.

---

## Common workflows

### Initial setup

```bash
bbman setup
bbman health --output json
bbman validate-config --output json
bbackup backup --backup-set production --dry-run --output json
```

### Before a backup run

```bash
bbman health
bbackup backup --backup-set production --dry-run --output json
bbackup backup --backup-set production --output json
```

### Maintenance

```bash
bbman status
bbman cleanup --yes
bbman diagnostics --report-file diagnostics.txt
```

### Update cycle

```bash
bbman check-updates
bbman update --yes
bbman health
```

### Agent-driven workflow

```bash
export BBACKUP_OUTPUT=json BBACKUP_NO_INTERACTIVE=1
bbman skills                          # discover capabilities
bbman health --output json            # preflight check
bbman run backup --containers myapp --no-interactive --output json
bbman status --output json
bbman cleanup --yes --output json
```

## Backup and restore safety boundaries

The `bbman` checks are pre-flight checks; the backup worker creates the
artifact. Every non-cancelled run writes `backup_manifest.json` beneath the
local staging backup directory and reports the staging path plus per-item and
per-remote status in JSON. Inspect local staging with:

```bash
bbackup list-backups --backup-dir /tmp/bbackup_staging --output json
```

Local, SFTP, and rclone uploads are independent. Each writes a `.partial`
destination and promotes it only after a successful copy. One failed remote can
therefore produce a partial overall result while another remote succeeds.
A local path or endpoint on the same host is not off-host protection, and
bbackup does not detect that topology.

Before a restore, use:

```bash
bbackup restore --backup-path /tmp/bbackup_staging/backup_YYYYMMDD_HHMMSS \
  --all --dry-run --output json
```

Restore dry-run reports selected targets only; it does not verify manifest
hashes, Docker access, or destination permissions. A real restore verifies the
manifest before mutation, but can stop/remove existing containers, replace
volumes or networks, and use `rsync --delete` for filesystem destinations.
Treat `--dry-run` as a plan, not as a non-destructive integrity proof.

---

## Troubleshooting

**Setup wizard does not appear automatically**

Run it directly: `bbman setup`

**Update fails**

```bash
bbman repo-url                               # Confirm URL is correct
bbman diagnostics --report-file report.txt  # Check for errors
cat ~/.local/share/bbackup/bbackup.log      # Review recent log entries
```

**Health check failures**

| Symptom | Fix |
|---|---|
| Docker not accessible | `sudo usermod -aG docker $USER && newgrp docker` |
| Missing Python packages | Activate the venv first, then `bbman check-deps --install` |
| Config parse error | `bbman validate-config` for details |
| rsync not found | Install the `rsync` system package |
| tar not found | Install the `tar` system package |

The Docker group grants root-equivalent host control; use it only for an
intentionally trusted account.

---

Back to [README.md](../README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
