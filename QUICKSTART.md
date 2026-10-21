# Your first backup and verified restore

This guide covers v2.0.3 and its schema-3 policy. For an older v2.0.2 installation, use its [versioned quick start](https://github.com/CruxExperts/BestBackup/blob/v2.0.2/QUICKSTART.md) until you migrate. Keep an independently retrievable copy of each repository password.

## Start with the guided dashboard

Install Python 3.12+, uv, and restic, then use the development environment described in [INSTALL.md](INSTALL.md):

```bash
uv sync --locked
uv run bbackup production tui
```

Setup walks through four short steps:

1. **Sources:** choose an existing folder and name the backup job. Hidden files are included.
2. **Encrypted storage:** choose a local repository and password-file path. An additional local repository is optional. Setup creates missing password files privately after review.
3. **Schedule:** choose a systemd calendar expression and the maximum seconds for each capture or copy. The default is daily and 3,600 seconds per stage.
4. **Review and enroll:** approve the source and storage mappings for this machine and user. Existing configuration files are never replaced.

Every field has a persistent label. Hover for help, or focus a field and press `F1`. Invalid inputs stay in the form so you can correct them. Advanced JSON remains available.

From Overview, choose **Set up storage**. Initialize the local repository first; initialize an optional replica with the local repository as its source. Choose **Run first backup**, review the selected job, and run it once. After success, select a snapshot and choose **Restore** with a new absolute destination folder.

> [!IMPORTANT]
> A second directory on the same host does not establish independent host-loss recovery. Copy repository passwords to independently retrievable encrypted or offline storage. Never store the only password copy inside the repository it unlocks.

## Prefer the command line?

These commands use the same enrolled default configuration as the dashboard:

```bash
uv run bbackup production doctor
uv run bbackup production repositories init --name local
uv run bbackup production jobs run --name daily
```

If you configured the optional replica, initialize it before running the job:

```bash
uv run bbackup production repositories init --name replica --source local
```

Keep the full snapshot IDs returned by successful capture and copy operations. A local capture can succeed while a replica fails; the command returns a nonzero result with each destination's outcome. Inspect failed or uncertain targets before another attempt.

Check the replica, then restore its full snapshot ID into a new folder:

```bash
uv run bbackup production repositories check --name replica
uv run bbackup production restore --repository replica \
  --snapshot FULL_DESTINATION_SNAPSHOT_ID --target "$HOME/restore-test"
```

The restore target must not exist or overlap source, repository, password, or state paths. File verification does not prove application startup or complete machine recovery.

## Policy you can move, bindings that stay private

Schema 3 keeps source identity and job intent in portable policy:

```json
{
  "schema_version": 3,
  "repositories": [{"name": "local", "role": "local"}],
  "sources": [{"name": "documents", "kind": "files"}],
  "jobs": [{"name": "daily", "repository": "local", "sources": ["documents"], "schedule": "daily", "timeout_seconds": 3600}]
}
```

Private bindings resolve those names to actual paths and credential-file references. This example is illustrative; replace each path with an absolute path for the executing account:

```json
{
  "schema_version": 3,
  "state_dir": "/private/state/bbackup",
  "repositories": {
    "local": {"repository": "/backup/local", "password_file": "/private/keys/local.password"}
  },
  "source_bindings": {"documents": {"paths": ["/srv/documents"]}}
}
```

Bindings and password files must be owner-only. Enrollment binds the exact policy and bindings to this machine and user. Copying them to another machine does not authorize execution there. Validated dashboard/CLI saves refresh enrollment automatically; manual edits require one explicit review and enrollment:

```bash
uv run bbackup production service enroll
```

For noninteractive new setup, supply reviewed candidate files using `setup --configuration-file POLICY --bindings-file BINDINGS`. This enrolls the candidates but does not initialize repositories or activate a schedule.

## Migrate an existing schema-2 configuration

Open the dashboard with the existing paths and choose **Migrate and enroll**, or run:

```bash
uv run bbackup production --config config.json --bindings bindings.json config migrate
```

Migration writes enrolled sibling `config.v3.json` and `bindings.v3.json` files. Original bytes remain untouched. Use the returned paths for explicitly named configurations. The default-path workflow recognizes a valid locally enrolled migrated pair on later launches.

## Apply a schedule deliberately

Render the selected job's units for review:

```bash
uv run bbackup production service render --name daily \
  --executable "$PWD/.venv/bin/bbackup"
```

Install the reviewed user-level units and activate their exact timer:

```bash
uv run bbackup production service install --name daily \
  --executable "$PWD/.venv/bin/bbackup" --activate
```

Units are installed under an absolute `$XDG_CONFIG_HOME/systemd/user`, or `~/.config/systemd/user` when that variable is unset or relative. Use the same configuration environment as your user systemd manager. Parent traversal and symbolic-link ancestors are refused.

Omit `--activate` to install disabled units. The command never enables lingering, installs a system-wide service, or elevates privileges. User timers require an available user systemd manager; boot operation may require administrator-managed lingering. A differing existing unit is preserved for review. [Installation](INSTALL.md#optional-systemd-scheduling) documents qualification limits.

The unreleased [reviewed Linux coverage workflow](docs/development/version-2.md#reviewed-linux-coverage-unreleased)
adds explicit private scope discovery, review and drift checks. Its
`service render-system` command renders root-controlled system units for
administrator review; it never installs them. A complete scope check does not
qualify complete-machine disaster recovery.

Persistent timers can catch up after an idle interruption. An interrupted mutation remains uncertain and blocks automatic replay until explicit reconciliation. Retention remains a separate reviewed action; scheduling does not silently prune data.

## Existing Docker and YAML users

The retained 1.x commands use their own configuration. Legacy Docker restores require unused container/volume names; bind-mounted container restoration is refused. SFTP requires a previously trusted host key. See [legacy management](docs/management.md) and [encryption compatibility](docs/encryption.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
