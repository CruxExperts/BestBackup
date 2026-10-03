# Install bbackup

This guide separates the version 2.0.0-alpha.1 preview from the retained 1.x command line. The alpha is a pre-release and is not production-qualified; the legacy 1.x tag remains available as v1.8.6.

## Version 2 preview

The preview uses Python 3.12 or newer and the `restic` executable. Install `restic` with your Linux distribution's package manager or from the [restic installation guide](https://restic.readthedocs.io/en/stable/020_installation.html).

Install the pinned preview with uv:

```bash
uv tool install --force 'git+https://github.com/CruxExperts/best-backup.git@v2.0.0-alpha.1'
bbackup --version
bbackup production --help
```

The command is pinned to the `v2.0.0-alpha.1` source tag. To inspect or modify the source, use the development checkout below.

### Optional native database clients

PostgreSQL capture requires `pg_dump` and `psql` for source metadata; restoring into PostgreSQL requires `psql` and `pg_restore`. MySQL capture requires `mysql` and `mysqldump`, or the compatible MariaDB clients. Database clients are not installed by the Python package. MySQL/MariaDB capture requires an explicit read-lock policy and pauses database writes during the dump.

### Optional systemd scheduling

The preview can render user-level systemd service and timer unit files. Rendering does not install or enable them. Review [the quick start](QUICKSTART.md) and generated unit contents before enabling a schedule.

### Cloud repositories

Restic repositories can use local storage, Backblaze B2 through the S3 API, or Amazon S3. Configure cloud credentials in the supervised process environment. Do not place credentials in portable JSON policy or a repository URL. See [cloud storage](docs/cloud-storage.md) for provider setup, inspection, and qualification limits.

## Existing 1.x release

To install the existing v1.8.6 release with the Docker/filesystem commands:

```bash
uv tool install --force 'git+https://github.com/CruxExperts/best-backup.git@v1.8.6'
bbackup --version
bbman --version
```

This workflow requires Python 3.12+, Docker Engine and socket access, `rsync`, and `tar`. `rclone` is optional for remotes that use it. Docker socket access grants root-equivalent host control.

The legacy setup wizard is interactive:

```bash
bbman setup
bbman health --output json
bbman validate-config --output json
```

See [docs/management.md](docs/management.md) for legacy setup, health, remote, and maintenance commands.

## Development checkout

Use a checkout when you want to run the preview from source or contribute changes:

```bash
git clone https://github.com/CruxExperts/best-backup.git
cd best-backup
uv sync --locked
uv run bbackup --version
uv run bbackup production --help
```

Optional Google Drive OAuth tooling belongs to the retained 1.x command line. Select its project extra only when working on that workflow:

```bash
uv sync --locked --extra gdrive-auth
uv run bbman auth-gdrive --client-secrets client_secret.json --dry-run --output json
```

For editable package installation, run `uv tool install --editable .` from the checkout. See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for focused checks.

## Shared server command links

Use this only when administrators want the command links in `/usr/local/bin`. The tool itself and runtime state remain separate; configure private bindings for the service account that runs bbackup.

```bash
UV="$(command -v uv)"
sudo env \
  UV_TOOL_DIR=/opt/uv/tools \
  UV_TOOL_BIN_DIR=/usr/local/bin \
  "$UV" tool install --force \
  'git+https://github.com/CruxExperts/best-backup.git@v2.0.0-alpha.1'

bbackup --version
```

The `UV="$(command -v uv)"` prefix preserves uv's absolute path when `sudo` uses a restricted `PATH`. Uninstall the shared tool with matching directories:

```bash
UV="$(command -v uv)"
sudo env UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin "$UV" tool uninstall bbackup
```

## Uninstall and update

Remove the uv tool with:

```bash
uv tool uninstall bbackup
```

If a user install is missing from `PATH`, inspect the uv tool bin directory:

```bash
uv tool dir --bin
```

Add that directory to the account's shell `PATH`, then open a new shell.

Install a newer release tag with `uv tool install --force` and the exact tag. Preserve your JSON/YAML configuration, private bindings, password files, repositories, and state directory separately; uninstalling the command does not manage or remove those files.

## Linux platform status

Ubuntu 24.04 and 26.04 on AMD64 and ARM64 are the version 2 target platforms. Ubuntu packages and upgrade/rollback behavior have not been qualified. Existing local evidence includes Linux Mint and a Garage S3-compatible storage test; that does not qualify B2, Amazon S3, or Ubuntu releases.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
