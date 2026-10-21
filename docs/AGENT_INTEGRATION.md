# Agent integration

The version 2 command line is designed for agents and other noninteractive clients. It accepts strict JSON input, publishes a schema for each operation, and emits sanitized versioned results. Stable v2.0.4 qualification retains the tested local/file, selected live database, headless dashboard, and AMD64 Ubuntu scope listed below. New cloud and Linux workflows ship as experimental and remain unqualified; B2/AWS and native Docker are not production-qualified. Follow the same read-before-write and no-replay rules you would use for an operator.

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

Restore requires a new target directory and verifies file data. It does not validate application startup or database behavior. Database restores accept trusted PostgreSQL, MySQL, and MariaDB archives with database-specific destination safeguards: PostgreSQL creates a new database using snapshot-time locale metadata; MySQL/MariaDB require the original database name on a separate empty recovery server with only built-in schemas. Set the explicit `trusted_archive` acknowledgement. Never restore untrusted archives: database restores execute archived SQL. See [the v2 database restore limits](development/version-2.md#native-database-capture-and-restore).

Reviewed Linux coverage also has an experimental manual PostgreSQL path in
v2.0.4; it does not change ordinary PostgreSQL adapters or stable
qualification. `jobs run`, `coverage discover`, `coverage review`,
`coverage check`, and `coverage recovery-evidence` accept the boolean
`--quiet-administrative-window-confirmed`, which defaults to false. Set it only
while an externally coordinated window prevents administrator, schema, role,
database, and configuration changes; it does not stop those changes itself.
Ordinary row writes may continue. Scheduled covered PostgreSQL jobs remain
blocked because timer dispatch cannot confirm the window. See the [manual
coverage procedure](development/version-2.md#experimental-postgresql-covered-capture)
for declaration, capture, and restore limits.

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

If a mutating command returns `requires_reconciliation` or the connection is lost after dispatch, do not repeat the command. Read the repository or destination state, inspect the operation ID, and decide what actually completed. Reconciliation records an administrator-reviewed outcome without rerunning the command.

For retained remote database exports, successful reconciliation verifies the
snapshot and each export's actual bytes before repairing the capture receipt
and releasing its owned staging files. A failed outcome retains the exports.
Remote capture reconciliation also requires `quiescent_window_confirmed: true`
after stopping repository writers for the entire inspection. It uses Restic's
`--no-lock` for reads and refuses observed repository locks; those observations
cannot prevent a new writer from starting elsewhere.
`runs retained-exports` discovers bounded capture IDs, declared counts and record
digests, including captures stopped before dispatch with no ledger row.
`runs discard-exports` explicitly discards reviewed failed exports using the exact
current record digest. See the [remote-primary recovery workflow](cloud-storage.md#remote-primary-with-bounded-staging-experimental)
for persistent-staging requirements and disposal steps. `runs list` remains a
ledger event stream.

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

## Development policy and enrollment

The machine result envelope remains schema version 2. Configuration and private-binding documents use schema version 3 in the development checkout; these are separate versioned interfaces.

Portable policy contains logical sources and job safety choices. Private `source_bindings` contain file paths and database/container selectors. Existing schema-2 files are inspection/migration inputs only. `config migrate` creates enrolled sibling `.v3.json` files without replacing originals; `setup --enroll` or `service enroll` explicitly approves a reviewed schema-3 pair on the current machine and user.

Default paths resolve from an absolute `XDG_CONFIG_HOME`, or the current user's `.config/bbackup` directory. Explicit `--config` and `--bindings` override them. A valid locally enrolled migrated default pair is reused on later launches.

The shared operation boundary verifies one bounded configuration snapshot and holds a policy lease throughout execution. Trusted configuration saves preserve previous bytes and refresh enrollment from the exact validated candidate; raw edits and imported bindings require enrollment again. Busy edits return promptly with unsaved text preserved. Error envelopes include actionable `schema_migration_required`, `enrollment_required`, `machine_identity_unavailable`, or `busy` codes where applicable.

A job captures once, then attempts each independent replica once. Partial results retain local snapshot identity, successful copies, and destination failures in `data`; `success` is false and exit status is nonzero. Cancellation stops later dispatch. Failed destination writes remain subject to reconciliation; the read-only source lease does not unnecessarily block other destinations. No failed or uncertain mutation is automatically replayed.

`timeout_seconds` is a positive job setting, default 3600, applied independently to native source capture, restic capture, and each copy. It is not a total job deadline. See the [generated command reference](production-cli.md) for the exact current schemas and field help.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
