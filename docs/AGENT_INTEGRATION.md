# Agent integration

The version 2 command line is designed for agents and other noninteractive clients. It accepts strict JSON input, publishes a schema for each operation, and emits sanitized versioned results. The `bbackup production` interface is a preview; follow the same read-before-write and no-replay rules you would use for an operator.

## Discover commands and schemas

List the version 2 operation registry and its JSON schemas:

```bash
bbackup production skills
```

Request one operation's schema without executing it:

```bash
bbackup production jobs run --schema
bbackup production recovery inspect --schema
```

The `--schema` result includes the operation summary, required fields, accepted types, and additional-property rules. The command list and schema set are versioned; agents should discover them at runtime instead of assuming a newer command exists.

## Submit strict JSON input

Provide both portable configuration and private host bindings for operations that access repositories:

```bash
bbackup production \
  --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" \
  jobs run --input-json '{"name":"daily"}'
```

Production operations always emit a JSON envelope. Input parsing rejects duplicate JSON keys, unknown fields, invalid types, and a value supplied both as a flag and in JSON. The portable configuration contains source and repository names; private bindings contain credential-file references and repository locations. Never put passwords or cloud credentials in prompts, JSON payloads, logs, or repository URLs.

## Handle results and errors

Successful results include `schema_version`, the command name, `success: true`, and command-specific `data`. Errors use the same envelope and a sanitized error code. Invalid input exits with status 2. Operational failures exit with status 3.

Capture and copy results return complete snapshot IDs. Preserve the destination ID returned for a replica and pass it unchanged to restore:

```bash
bbackup production \
  --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" \
  restore --repository replica --snapshot FULL_DESTINATION_SNAPSHOT_ID \
  --target "$HOME/restore-test"
```

Restore requires a new target directory and verifies file data. It does not validate application startup or database behavior. For databases, the preview only restores trusted PostgreSQL exports into a new database; the caller must set the explicit `trusted_archive` acknowledgement. See [the v2 preview limits](development/version-2.md#native-database-capture-preview).

## Read bounded operation records

Use pagination for operation events and snapshot receipts:

```bash
bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" \
  runs list --limit 100 --offset 0

bbackup production --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" \
  runs stream --limit 100 --offset 0
```

`runs stream` emits one JSON object per line. Pages contain at most 1,000 events. The local ledger stores sanitized lifecycle data and excludes raw command arguments, environment values, and process output.

## Reconcile uncertain mutations

If a mutating command returns `requires_reconciliation` or the connection is lost after dispatch, do not repeat the command. Read the repository or destination state, inspect the operation ID, and decide what actually completed. Reconciliation records an administrator-reviewed outcome; it does not rerun the command or repair missing receipts.

```bash
bbackup production \
  --config "$HOME/.config/bbackup/config.json" \
  --bindings "$HOME/.config/bbackup/bindings.json" \
  runs reconcile --input-json '{"repository":"local","operation_id":"OPERATION_ID","outcome":"succeeded","repository_reviewed":true}'
```

Allowed outcomes are `succeeded`, `failed`, and `abandoned`. Review the destination before recording one. Use one shared state directory for all bbackup processes that coordinate a repository; local locking cannot coordinate another host or an external restic writer.

## Recovery checkpoints

The cloud recovery commands require explicit storage identity, full repository and snapshot IDs, trusted signing fingerprints, and independent password custody. A checkpoint additionally requires a full repository check and an administrator-coordinated quiescent window across every writer. Lifecycle inspection reports configuration; it does not establish provider protection. See [cloud storage](cloud-storage.md) and [the recovery limits](development/version-2.md#independent-recovery-artifacts).

The B2 capability inspector is read-only and does not require configuration or host bindings:

```bash
bbackup production recovery b2-inspect --bucket BUCKET --prefix restic/
```

By default it reads `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` from the process environment. Optional `--key-id-env` and `--key-env` flags select different environment variable names; they never accept credential values. The response verifies bucket access and version listing, and reports the key's read/write capabilities, unsafe capabilities, and lifecycle conflicts. Capability fields do not establish a successful object write or restore. It always reports `protection_qualified: false`. Do not pass access-key values in command arguments or JSON input.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
