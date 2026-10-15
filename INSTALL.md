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

## Debian package installation

Native Ubuntu packages target Ubuntu 24.04 with Python 3.12 and Ubuntu 26.04 with Python 3.14, on amd64 or arm64. Use a package built natively for the target release, architecture, and Python version; the package builder refuses cross-build labels.

Inspect the package, contents, manifest, and checksum before installing. For the current preview's amd64 artifact:

```bash
PACKAGE=dist/deb/bbackup_2.0.0~a1_ubuntu24.04_amd64.deb
(cd "$(dirname "$PACKAGE")" && sha256sum -c "$(basename "${PACKAGE%.deb}.sha256")")
dpkg-deb --info "$PACKAGE"
dpkg-deb --contents "$PACKAGE"
REVIEW_DIR="$(mktemp -d)"
dpkg-deb --extract "$PACKAGE" "$REVIEW_DIR"
cat "$REVIEW_DIR/usr/share/doc/bbackup/build-manifest.json"
```

Install only after reviewing that output:

```bash
sudo apt install ./dist/deb/bbackup_2.0.0~a1_ubuntu24.04_amd64.deb
bbackup --version
bbman --help
```

The release build matrix targets four uniquely named native packages: `bbackup_<version>_ubuntu24.04_amd64.deb`, `bbackup_<version>_ubuntu24.04_arm64.deb`, `bbackup_<version>_ubuntu26.04_amd64.deb`, and `bbackup_<version>_ubuntu26.04_arm64.deb`. A configured matrix is not evidence that all four artifacts have been built or qualified. Each package contains the versioned Python package, its locked dependencies, and the verified restic binary. Installation does not install Python packages into system Python, access a package index at runtime, enable or start services, or remove configuration, private bindings, credentials, repositories, or backup state.

Package upgrades and removals refuse while either command is active. A root-owned marker blocks both launchers while package files are changing. Successful configuration verifies the installed payload checksum manifest before clearing the marker. On abort, a marker requires the exclusive process lock and complete payload validation before removal. Without a marker, the abort hook takes a shared lock, verifies the payload and launchers, and returns without clearing anything; existing clients can continue while transition writers remain excluded.

The abort hook belongs to the **already-installed** package. An older build without the marker-free abort correction can become half-configured after refusing an active-client upgrade. Stop clients and install the corrected matching package before relying on that behavior. For any failed transition, inspect dpkg status and logs. Run `sudo dpkg --configure -a` only when dpkg reports an unconfigured package and its installed hook can validate the payload; never remove the marker manually. If repair cannot verify the restored package, reinstall the preserved matching `.deb` with `sudo dpkg --install /path/to/bbackup_<version>_ubuntu<release>_<architecture>.deb`, then inspect `sudo dpkg --audit` and package logs. No backup operation is replayed. Keep prior `.deb` files for explicitly reviewed rollback after stopping clients. Package rollback never rolls user data back.

## Disabled systemd templates

The package includes `bbackup@.service` and `bbackup@.timer` under `/usr/lib/systemd/system`, but never enables or starts them and does not create configuration, enroll a job, or provide default secrets. The templates use the fixed paths `/etc/bbackup/config.json` and `/etc/bbackup/bindings.json`; each timer instance defaults to `OnCalendar=daily`. The service runs as root under the system manager's default configuration.

To enroll and activate a job, first prepare complete, reviewed configuration and private-binding input files. The production `setup` command creates the fixed-path files only when they do not already exist; it preserves private file permissions. Then validate the enrolled configuration and inspect the configured jobs before choosing an instance to schedule:

```bash
read -r -p 'Reviewed portable configuration file: ' CONFIG_INPUT
read -r -p 'Reviewed private bindings file: ' BINDINGS_INPUT
sudo bbackup production --config /etc/bbackup/config.json --bindings /etc/bbackup/bindings.json \
  setup --configuration-file "$CONFIG_INPUT" --bindings-file "$BINDINGS_INPUT"
sudo bbackup production --config /etc/bbackup/config.json --bindings /etc/bbackup/bindings.json doctor
sudo bbackup production --config /etc/bbackup/config.json --bindings /etc/bbackup/bindings.json jobs list
sudo systemctl daemon-reload
sudo systemd-analyze verify /usr/lib/systemd/system/bbackup@.service /usr/lib/systemd/system/bbackup@.timer
read -r -p 'Exact configured job name to schedule daily: ' JOB_NAME
sudo systemctl enable --now "bbackup@${JOB_NAME}.timer"
```

Do not enable the timer until the configuration, bindings, repository access, and job name have been reviewed. The generic `bbackup@` timer is daily only; it does not express weekly, monthly, or another per-job calendar. For a custom schedule, render the concrete job-specific service/timer pair with `bbackup production --config /etc/bbackup/config.json --bindings /etc/bbackup/bindings.json service render --name "$JOB_NAME" --executable /usr/bin/bbackup`, review and install those returned units, then explicitly enable only the matching timer.

Remove the program without deleting user data with:

```bash
sudo apt remove bbackup
```

See [scripts/README.md](scripts/README.md#build-a-native-ubuntu-package) for the native package build and review commands.

## Linux platform status

Ubuntu 24.04/26.04, Python 3.12/3.14 respectively, and amd64/arm64 remain the intended package targets. Native AMD64 Ubuntu-container checks passed fresh installation, actual installed-command capture/check/restore, refusal while the installed dashboard was running, interrupted-unpack launcher blocking, damaged-payload refusal, and recovery by reinstalling the verified same-version package. Configuration, password files, and SQLite state were preserved. These bounded checks do not qualify ARM64, cross-application-version rollback, systemd boot/reboot behavior, B2, or Amazon S3. See [qualification and remaining work](docs/development/version-2.md#qualification-and-remaining-work) for the evidence and open release gates.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
