# Version 2 preview quick start

This guide creates an encrypted local restic repository, copies a successful snapshot to an independent repository, and verifies a restore into a new directory. It uses strict JSON policy and private host bindings. Version 2.0.0-alpha.1 is a pre-release preview; review the [preview limits](docs/development/version-2.md) before using real data.

## Requirements

- Python 3.12 or newer and `restic` on `PATH`.
- Read access to the source paths and write access to the repository and state locations.
- `openssl` for the example password generation.
- A second storage location for an independent replica. Backblaze B2 through the S3 API and Amazon S3 are supported configurations; provider qualification is still pending.

Docker, `rsync`, and `tar` are needed only for the retained 1.x Docker/filesystem commands. Native database capture and restore have separate client requirements; see [version 2 details](docs/development/version-2.md).

## Create private paths and passwords

The example uses user-owned paths. Adjust them for the account that runs bbackup. Keep source data, repositories, passwords, and state separate.

```bash
install -d -m 700 \
  "$HOME/.config/bbackup" \
  "$HOME/.local/state/bbackup" \
  "$HOME/.local/share/bbackup" \
  "$HOME/backup-local" \
  "$HOME/backup-replica"

umask 077
openssl rand -hex 32 > "$HOME/.local/share/bbackup/local.password"
openssl rand -hex 32 > "$HOME/.local/share/bbackup/replica.password"
chmod 600 "$HOME/.local/share/bbackup/"*.password
```

Store each password file outside captured sources and repositories. Keep an independent recovery copy of both passwords. A recovery kit can contain the repository password, but creating that kit on the protected host does not establish off-host custody.

## Write portable policy

Save this as `~/.config/bbackup/config.json`, replacing `/srv/documents` with an existing absolute source path:

```json
{
  "schema_version": 2,
  "repositories": [
    {"name": "local", "role": "local"},
    {"name": "replica", "role": "replica"}
  ],
  "sources": [
    {"name": "documents", "kind": "files", "paths": ["/srv/documents"]}
  ],
  "jobs": [
    {"name": "daily", "repository": "local", "sources": ["documents"], "replicas": ["replica"]}
  ]
}
```

Policy names repositories and sources. It contains no passwords or cloud credentials.

## Write host-private bindings

Create `~/.config/bbackup/bindings.json` with absolute paths for the account that runs bbackup. The shell expands `$HOME` while writing the file:

```bash
cat > "$HOME/.config/bbackup/bindings.json" <<JSON
{
  "schema_version": 2,
  "state_dir": "$HOME/.local/state/bbackup",
  "repositories": {
    "local": {
      "repository": "$HOME/backup-local",
      "password_file": "$HOME/.local/share/bbackup/local.password"
    },
    "replica": {
      "repository": "$HOME/backup-replica",
      "password_file": "$HOME/.local/share/bbackup/replica.password"
    }
  }
}
JSON
chmod 600 "$HOME/.config/bbackup/bindings.json"
```

Bindings are machine-specific and must remain private. The state directory must be private to its owner. Use the same state directory for every bbackup process that accesses these repositories.

For a cloud replica, replace its `repository` value with the provider's restic repository identifier and provide credentials through the supervised process environment. Keep credentials out of the JSON file and URL. Follow the [cloud storage guide](docs/cloud-storage.md) for B2/S3 configuration, B2 inspection, and provider qualification limits.

## Initialize and run a job

Validate configured bindings and check for restic:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" doctor
```

Initialize each repository. The replica copies the source repository's chunker settings, while its password remains independent:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" repositories init --name local
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" repositories init --name replica --source local
```

Run the configured job:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" jobs run --name daily
```

The command captures locally, records a successful full snapshot ID, then copies it to each declared replica. Save the full destination snapshot ID from the result. A failed or interrupted mutation may require read-only inspection and explicit reconciliation before another attempt.

## Check and restore

Check a repository's data:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" repositories check --name replica
```

Restore using the full snapshot ID returned for `replica` and a new absolute target directory:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" restore \
  --repository replica --snapshot FULL_DESTINATION_SNAPSHOT_ID \
  --target "$HOME/restore-test"
```

The target must not already exist and must not overlap a source, repository, password, or state path. The restore verifies file data; it does not start applications or validate database behavior. Use an isolated test target before relying on any recovery process.

## Mouse and agent operation

Open the dashboard with:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" tui
```

For unattended jobs and agents, use the strict `--input-json` interface. It rejects duplicate JSON fields and unknown command fields, and returns a versioned JSON envelope. Start with the [agent integration guide](docs/AGENT_INTEGRATION.md); it documents schemas, bounded run history, and uncertain-operation reconciliation.

## Legacy 1.x commands

The 1.x command family remains available for existing Docker and YAML configurations. Use `bbman setup` and `bbackup backup` only for that retained workflow. Its configuration and encryption settings do not configure the v2 production preview. See [legacy management](docs/management.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
