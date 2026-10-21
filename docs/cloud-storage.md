# Cloud storage: Backblaze B2 and Amazon S3

Keep your first recovery point on local storage. The v2.0.3 tested stable-release
scope includes local/file capture and restore, selected live database drills,
headless dashboard happy paths, and AMD64 Ubuntu packages.

**Backblaze B2 and Amazon S3 are implemented but not production-qualified.** A
small live B2 source-path drill passed on 2026-10-20; it does not qualify B2
protection or production recovery. No live AWS capture/check/restore result is
recorded. The local Garage S3 test also passed capture, copy, full-data checking,
and restore, but does not qualify B2 or Amazon S3. Treat the provider setup below
as experimental; keep B2/AWS outside production recovery until the remaining
provider and independent-recovery evidence is complete.

## Choose a connection

| Storage | Repository binding | Credential variables | Qualification status |
|:--|:--|:--|:--|
| Backblaze B2, S3 route | `s3:https://s3.REGION.backblazeb2.com/BUCKET/restic` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Implemented; not production-qualified |
| Amazon S3 | `s3:https://s3.REGION.amazonaws.com/BUCKET/restic` | AWS credential provider chain | Implemented; not production-qualified |
| Backblaze B2, native restic route | `b2:BUCKET:restic` | `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY` | Implemented; not production-qualified; use S3 route for checkpoint creation |

Use the actual endpoint shown for your bucket, rather than the literal `REGION`
placeholder. Keep buckets private and choose a dedicated repository prefix.
Restic implements backup transfer, repository encryption, and restore. The
Backblaze vendor SDK implements native B2 inspection; boto3 implements cloud
checkpoint inventory and historical-object downloads through S3.

The [restic storage guide](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
documents both backends. Backblaze also publishes a
[restic integration guide](https://www.backblaze.com/docs/cloud-storage-integrate-restic-with-backblaze-b2).

## Connect Backblaze B2

1. Create a private test bucket in your Backblaze account.
2. Create a bucket-scoped application key with the capabilities needed for your
   intended backup and restore workflow. Keep administrator credentials separate.
3. Load the credentials through your private environment manager or supervised
   service environment. Do not put values into configuration, shell history, or Git.
4. Inspect the bucket through the native vendor SDK:

```bash
bbackup production recovery b2-inspect --bucket YOUR_BUCKET --prefix restic/
```

Inspection reads `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY`. If your private
environment uses different names, pass the **names**, never their values:

```bash
bbackup production recovery b2-inspect --bucket YOUR_BUCKET --prefix restic/ \
  --key-id-env YOUR_KEY_ID_VARIABLE --key-env YOUR_SECRET_VARIABLE
```

The result reports bucket access, version listing, write capability, dangerous
administration capabilities, and overlapping lifecycle conflicts. It returns the
bucket's S3 endpoint and excludes account IDs, keys, tokens, and file names.
An empty bucket is a valid inspection target. `read_capability_present` is a key
capability observation; it does not claim an actual file download has succeeded.

For the S3 route, supply the same B2 application key ID and application key under
the standard AWS credential variable names in the private process environment.
For the native restic route, use the native variable names in the table above.
The native SDK check does not change bucket policies or persist authorization.

For central lifecycle inspection, Backblaze documents the separate
`readBucketLifecycleRules` capability for `Get Lifecycle Configuration`. Add
that read capability only to the centrally controlled verifier/retention
identity; the source key does not need it. Do not add
`writeBucketLifecycleRules` for a read-only inspection. See
[Backblaze's S3-compatible app-key capabilities](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys).

### Restricted source profile (unreleased)

The `restricted_source_profile` result is a versioned assessment of the SDK's
authorization metadata. It requires one exact bucket and a nonempty,
directory-scoped machine prefix, repository read/list/write capabilities, and
no permanent-version deletion, administration, or unknown capabilities. A broad
key can still be inspected, but does not satisfy this profile. An eligible
metadata result alone does not prove that a forbidden operation is denied. The
small live trial below passed seven denial checks, but full recovery
qualification remains required by
[issue #23](https://github.com/CruxExperts/BestBackup/issues/23).

Use a distinct prefix per machine, such as `machines/MACHINE_ID/`. Keep the
provisioner, central verifier, and retention executor identities off source
machines. Backblaze distinguishes deleting by name using `writeFiles` from
permanently deleting a specific version using `deleteFiles`; a source writer may
hide names or upload replacement versions. This profile does not prevent those
attacks. Recovery depends on exact historical version identities, adequate
noncurrent retention, independent escrow, and successful restoration. See
[Backblaze's S3 app-key capabilities](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys)
and [Object Lock behavior](https://www.backblaze.com/docs/cloud-storage-object-lock).

A small live B2 source-path trial passed on 2026-10-20 using an exact-bucket,
machine-prefix source key. The trial completed two captures, a repository check,
and restores of the older and newer snapshots; the incremental file was absent
from the older restore and present in the newer one, and both restores preserved
the hidden fixture file and symlink. It also checked the source key's four
reported capabilities and seven denied operations. The detailed evidence and
limits are recorded in the
[version 2 development qualification record](development/version-2.md#live-backblaze-b2-source-trial).
This narrow result does not establish the thirty-day historical grace period,
provider lock/hold behavior, off-host escrow, or production recovery.

### Central recovery acceptance (unreleased)

On an independent verifier, `recovery accept` opens a source-signed encrypted
recovery kit, reconstructs the exact historical repository versions, and runs a
full Restic data check and exact repository/snapshot identity verification. Only
then does it write a signed central acceptance record into a new private
directory. The verifier fingerprint must differ from the source signer. Trust
that fingerprint through separately controlled configuration and keep its
private signing key and the escrow decryption authority off source hosts. Supply
complete primary-key fingerprints; signing-subkey fingerprints are rejected.

```bash
bbackup production recovery accept \
  --kit /secure/escrow/recovery-kit.gpg --target /secure/acceptances/NEW_RECORD \
  --source-signer SOURCE_SIGNER_FINGERPRINT --verifier CENTRAL_VERIFIER_FINGERPRINT \
  --repository cloud --repository-id FULL_REPOSITORY_ID --snapshot FULL_SNAPSHOT_ID \
  --bucket YOUR_BUCKET --endpoint https://s3.REGION.backblazeb2.com \
  --prefix machines/MACHINE_ID/
```

Run this with read-only historical-version access and enough private disk for
the reconstructed repository. Use the independently retrieved kit and keys;
the command does not establish their off-host custody. Central verification time
comes from the verifier, not the source snapshot timestamp. A signed acceptance
proves only the recorded repository check. It does not prove clean-machine
recovery, application startup, B2 negative permission tests, or a guaranteed
historical retention window. `protection_qualified` remains false.

The small live workstation drill reconstructed and checked historical versions
both before and after the source key hid configuration and replaced a data
object. It restored the older snapshot with matching payload, hidden file and
symlink, using the encrypted kit for its password and a separate read-only
verifier identity. This is a simulated compromise on one workstation; it does
not establish off-host custody or replacement-machine recovery. See the
[qualification record](development/version-2.md#live-backblaze-b2-source-trial).

### Central noncurrent-version retention (unreleased)

The experimental `cloud-retention` commands reclaim **noncurrent object
versions only**. They do not run Restic `forget` or `prune`, compact packs, or
delete current objects and delete markers. Keeping all current packs avoids
breaking deduplicated snapshots. Full repository compaction and live provider
qualification remain outside this implementation slice of
[issue #25](https://github.com/CruxExperts/BestBackup/issues/25).

Run retention on the independently controlled management host with its own
deletion-capable credentials. Use an enrolled schema-3 repository binding whose
endpoint, bucket and prefix exactly match the private retention policy. The
shared repository operation ledger serializes deletion with local backup and
restore operations. Arrange a quiescent window across other hosts as well;
the local lease cannot exclude a writer on another machine.

The mode-0600 policy remains schema 1 for generic S3. It contains `policy_id`, a
64-character random hex `catalog_id`, positive `revision`, exact `endpoint_url`,
`bucket`, nonempty `prefix`, full `repository_id`, trusted central verifier
primary fingerprint, `retention_counts` (`daily`, `weekly`, `monthly`),
`grace_seconds` (at least 2592000), `plan_max_age_seconds`,
`max_apply_objects`, `max_apply_bytes`, private absolute `state_dir`, nonempty
`known_good_manifest_digests`, and `pending_manifest_digests`. Keep central state
independently backed up. Losing an established catalog stops planning;
enrollment cannot silently recreate it.

For Backblaze B2, schema 2 adds one exact native lock-metadata selector; every
other policy field remains required:

```json
{
  "schema_version": 2,
  "lock_metadata": {
    "provider": "b2-native",
    "bucket_id": "EXACT_BUCKET_ID"
  }
}
```

This is only the B2-specific policy fragment. The bucket ID must match the
selected bucket and the policy's canonical HTTPS Backblaze S3 endpoint. Planning
rejects target or reader mismatches; versions evaluated for deletion require
exact identity and authorized protection metadata. Changed evidence stops apply,
and extra lock-metadata policy fields are rejected. Existing policy CLI arguments select the
schema; there are no new flags. A schema change requires an incremented policy
revision, new policy digest, matching grant, and new plan. Preserve the enrolled
catalog and original first-seen times; do not re-enroll to reset grace.

The native metadata reader uses the same centrally configured standard B2
application-key ID and secret as S3 requests. It does not accept a session
token. A broader central key is not required. Keep central retention and
provisioner/master credentials off source machines; never put them in the S3
source process. The source key remains unchanged and restricted to source work.
Backblaze's [native `b2_get_file_info` response](https://www.backblaze.com/apidocs/b2-get-file-info)
exposes per-file retention and legal-hold authorization and values. The reader
accepts only authorized, explicit metadata: an authorized null retention mode
and null retain-until timestamp mean no retention, and the SDK maps an authorized
null legal-hold value to off. Missing or unauthorized metadata remains unknown.
The S3 `ObjectLockConfigurationNotFoundError` response is ambiguous for this B2
binding and is not treated as proof that lock is absent. See Backblaze's
[native Object Lock API guide](https://www.backblaze.com/docs/cloud-storage-enable-object-lock-with-the-native-api).

The separate mode-0600 recurring grant binds `schema_version: 1`, `policy_id`,
`catalog_id`, `policy_revision`, the exact file's `policy_sha256`, matching
target and repository fields, `action: "delete_noncurrent_versions"`, an
`expires_at` timestamp, and positive `max_apply_objects`/`max_apply_bytes` limits.
Its `stop_conditions` must be exactly `cancelled`, `input_drift`,
`lock_unknown_or_changed`, `mutation_outcome_uncertain`, and
`policy_or_grant_expired`. A broad management key does not expand this grant.

Keep `records.json` private; its JSON array lists the absolute directories of
all central acceptance records for this catalog. Each directory contains the
signed acceptance **and** original signed `checkpoint.jsonl`/`checkpoint.sig`.
Signatures and exact dependencies are verified. Daily/weekly/monthly selection
uses central verification time; previously known records cannot be omitted.
Selected checkpoints, known-good and pending pins, and newly accepted checkpoints
within the grace period remain protected. Missing or wrong-size protected object
versions stop planning and application.

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-retention catalog-enroll --repository cloud \
  --policy-file /secure/retention/policy.json --grant-file /secure/retention/grant.json
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-retention plan --repository cloud \
  --policy-file /secure/retention/policy.json --grant-file /secure/retention/grant.json \
  --records-file /secure/retention/records.json --quiescent-window-confirmed
```

Catalog enrollment is a one-time action. Inspect the plan's sanitized counts
and digests and private immutable file before applying its exact ID and SHA-256.
The noncurrent grace clock starts at central first observation, rather than the
upload timestamp. Enabled lifecycle expiration rules are refused. Versioning,
bucket locking, per-version retention and legal holds are checked conservatively.
The live verifier read succeeded after the separate central identity received
`readBucketLifecycleRules`; versioning was enabled and no lifecycle expiration
rules applied. No lifecycle-write capability was added. Separate canaries
confirmed denial of source retention/hold administration and exact deletion of
one unprotected version by the central retention identity. Separate fresh
governance-locked and legally held canaries each refused permanent deletion by
that identity without bypass capability. Independent readback confirmed survival,
and the native reader observed both protections. The provisioning identity
subsequently removed protection and deleted only those canaries. These checks do
not qualify aged-version reclamation or the promised historical recovery window.

A separate two-object workstation probe used the restricted source key for S3
deletion **without** a version ID. Both the governance-locked current object and
the legally held current object received one hide marker and became absent by
current-name lookup. Independent native inspection confirmed that each exact
original version and its protection survived. Read-only reconciliation found no
uncertain operations; the provisioning identity then removed protection and
deleted only those two canaries and their markers. This tests name hiding and
historical-version survival, not aged reclamation or a guaranteed recovery window.

A live central-retention rehearsal enrolled a catalog, planned with
a fresh thirty-day first-seen grace period, ran a guarded zero-apply, and
reconciled an empty plan. The plan identified 10 protected versions among 31
inventory versions. Before/after inventory matched and no S3 delete request was
sent. Empty reconciliation made zero provider reads, so it does not demonstrate
recovery from an uncertain deletion. The S3 lock-configuration response was an
ambiguous `ObjectLockConfigurationNotFoundError`. The native reader separately
verified the exact original configuration and data versions, including authorized
null lock settings, while the S3 lock flag was false. A subsequent production CLI
run completed setup, deliberate schema-3 enrollment, schema-2 planning,
zero-candidate apply and empty reconciliation using the existing catalog and
repository lease. It preserved established first-seen times, the original
policy/grant files and all 31 versions, including 10 protected versions. No aged
deletion or uncertain mutation was exercised, and no cost or savings were
measured.

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-retention apply --repository cloud \
  --policy-file /secure/retention/policy.json --grant-file /secure/retention/grant.json \
  --records-file /secure/retention/records.json --plan-id EXACT_PLAN_ID \
  --plan-sha256 EXACT_PLAN_SHA256 --quiescent-window-confirmed
```

Application revalidates authority, expiry, dependencies, inventory and locks,
then journals intent before each exact-version delete. Mutation retries are
disabled. Cancellation stops new dispatch; an uncertain response blocks replay.
Inspect the target and reconcile the original journal using read-only requests:

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-retention reconcile --repository cloud \
  --policy-file /secure/retention/policy.json \
  --plan-file /secure/retention/state/retention-plans/EXACT_PLAN_ID.json
```

Fixture tests include real signed recovery records, dependency loss, lock and
lifecycle changes, cancellation, contention, expiry, and lost responses before
and after deletion. They do not establish B2/AWS semantics or production savings.
`protection_qualified` remains false.

### Central snapshot maintenance (unreleased)

The separate experimental `cloud-maintenance` commands retire obsolete visible
snapshots before historical-version reclamation. Run them on the central
management machine. They preserve every checkpoint still protected by the
central daily/weekly/monthly selection, known-good and pending pins, or acceptance
grace. They forget only explicit full IDs that are visible, independently accepted
and no longer protected. Unknown snapshots remain untouched. Full repository
manifests can conservatively pin data shared with older checkpoints.
The current production maintenance path requires a canonical Backblaze B2 S3
endpoint; it does not qualify generic S3 or AWS maintenance.

Maintenance runs Restic `forget` with the exact planned IDs, then
`prune --max-repack-size 0`. This avoids repacking shared packs, while still
allowing removal of wholly unused packs and index rewrites. B2 versionless
removal hides names; it does not permanently remove historical versions.
Permanent reclamation remains the separate `cloud-retention` workflow after
its real first-observed noncurrent grace. Never reset a catalog or backdate its
records to make a version eligible.

A schema-3 central policy retains the existing target, retention and protection
fields, including the B2 native `lock_metadata` selector when used, and adds:

```json
{
  "schema_version": 3,
  "maintenance": {
    "mode": "forget-and-prune-no-repack",
    "resource_budget": {
      "schema_version": 1,
      "staging_max_bytes": 1048576,
      "minimum_free_bytes": 65536,
      "cache_max_bytes": 0
    },
    "timeout_seconds": 3600
  }
}
```

This is a policy fragment, not a complete policy. A schema-2 recurring grant
retains the existing authority fields and sets
`action: "maintain_current_repository"`. A maintenance grant cannot authorize
permanent-version deletion; a deletion grant cannot authorize maintenance.
Plans bind the exact policy, grant, accepted records, protected versions, current
inventory, Restic repository identity, visible snapshots and private bindings.
Current object inventory, excluding transient Restic `locks/` entries, is bounded
by the policy and grant limits, including retained or held current objects.
Those limits do not promise a maximum prune request count or memory use.

Keep a separate private maintenance bindings file. It references a distinct
application key for the Restic child, rather than the central version-deletion
credential pair:

```json
{
  "schema_version": 1,
  "staging_dir": "/secure/maintenance-staging",
  "access_key_id_env": "BBACKUP_B2_MAINTENANCE_KEY_ID",
  "secret_access_key_env": "BBACKUP_B2_MAINTENANCE_KEY",
  "credential_profile": "restricted"
}
```

Use an owned private, dedicated mounted filesystem within the staging budget;
cache remains disabled and free-space reserves are observed continuously. The
budget belongs to central policy and does not require a capture job on this
machine. The standard restricted key is bound to the exact bucket and machine
prefix with read/list/write permissions and no permanent deletion or management
capability. An explicitly selected `management` profile permits a broader key;
it does not expand the reviewed target or grant. Do not put inline secrets in
bindings or pass provisioning/deletion credentials to Restic.

For a new catalog, enroll it once, then explicitly register independently signed
acceptance records with `cloud-maintenance records-enroll`. Registration uses
the existing catalog owner and preserves established acceptance and first-seen
state. Logical maintenance never silently accepts new records. Existing catalogs
must be preserved, including when changing policy schema or grant action.

```bash
# New catalog only; skip this command for an established catalog.
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-maintenance catalog-enroll --repository cloud \
  --policy-file /secure/maintenance/policy.json \
  --grant-file /secure/maintenance/grant.json
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-maintenance records-enroll --repository cloud \
  --policy-file /secure/maintenance/policy.json \
  --grant-file /secure/maintenance/grant.json \
  --records-file /secure/maintenance/records.json
bbackup production --config config.v3.json --bindings bindings.v3.json \
  cloud-maintenance plan --repository cloud \
  --policy-file /secure/maintenance/policy.json \
  --grant-file /secure/maintenance/grant.json \
  --records-file /secure/maintenance/records.json \
  --maintenance-bindings-file /secure/maintenance/bindings.json \
  --quiescent-window-confirmed
```

Inspect the returned target IDs and immutable plan before applying its exact
`--plan-id` and `--plan-sha256` with `cloud-maintenance apply`; retain the same
policy, grant, records, bindings and quiet-window arguments. If the attempt is
uncertain, use `cloud-maintenance reconcile` with those exact arguments and the
original plan ID and digest. Do not run apply again to discover what happened.

Plan and reconcile only while repository writers remain stopped for the entire
operation, with `--quiescent-window-confirmed`. Their Restic inspections use
`--no-lock --no-cache` and observe repository locks before and after reads.
These observations cannot prevent another host from starting a writer. Apply
uses the shared local repository lease and normal Restic mutation locks.

Each mutation has a durable phase intent. An interrupted or uncertain attempt
blocks replay of the plan; inspect and reconcile the original attempt instead.
Healthy repository checks alone do not prove that prune completed. Reconciliation
also verifies protected historical versions and records only what its evidence
establishes. No automatic replay or implicit repository initialization occurs.

## Bind the cloud repository

Follow [the quick start](../QUICKSTART.md) to create your local repository, source,
job, and independent passwords. Add a replica in portable `config.json`:

```json
{"name": "cloud", "role": "replica"}
```

Add `"cloud"` to the job's `replicas` array. In private mode-0600 `bindings.json`,
bind that name to your selected endpoint:

```json
{
  "repository": "s3:https://s3.REGION.backblazeb2.com/YOUR_BUCKET/restic",
  "password_file": "/etc/bbackup/cloud.password"
}
```

For Amazon S3, replace the repository URL with the Amazon endpoint from the
connection table. AWS profiles and temporary credentials are resolved by the
underlying storage clients; make those credentials available to the systemd
process as well as your interactive shell.

Initialize the replica from the local repository to match chunking parameters:

```bash
bbackup production --config config.json --bindings bindings.json \
  repositories init --name cloud --source local
bbackup production --config config.json --bindings bindings.json jobs run --name daily
bbackup production --config config.json --bindings bindings.json repositories check --name cloud
```

The run result distinguishes local completion from cloud completion and records
both snapshot identities. Use `destination_snapshot_id` from the cloud copy to
restore from cloud into a new directory:

```bash
bbackup production --config config.json --bindings bindings.json restore \
  --repository cloud --snapshot FULL_DESTINATION_SNAPSHOT_ID --target /srv/cloud-drill
```

Keep local snapshots awaiting required replication. A job run does not prune
them automatically. A failed copy remains a failure even when local capture
succeeded; reconcile uncertain operations before retrying.

## Retries, backoff, and interruption

### Remote primary with bounded staging (unreleased)

An experimental `remote-primary` repository can capture ordinary files directly
through Restic into an HTTPS S3 endpoint. It does not create a second local
repository or archive. Local-first capture and replication remain available.
This implementation has local regression evidence and a small live B2
workstation source trial, recorded in the [version 2 development qualification
record](development/version-2.md#live-backblaze-b2-source-trial). Final
remote-server and provider-protection qualification remain deferred.

For example, the portable repository and job declarations are:

```json
{
  "repositories": [{"name": "cloud", "role": "remote-primary"}],
  "jobs": [{
    "name": "daily", "repository": "cloud", "sources": ["files"],
    "resource_budget": {
      "schema_version": 1, "staging_max_bytes": 67108864,
      "minimum_free_bytes": 8388608, "cache_max_bytes": 0
    }
  }]
}
```

These are fragments of a schema-3 policy, not a complete configuration. Bind the
repository to its HTTPS S3 URL and private password file as above, and set
`staging_dir` in private host bindings. The staging directory must be an owned,
mode-0700, dedicated mounted filesystem whose entire capacity is at most the
configured staging budget. It must contain only the stable bbackup staging lock
before a new capture starts. Read-only reconciliation can inspect validated
retained exports on that mount. Arrange the mount separately, including persistence
for scheduled jobs; bbackup neither mounts it nor deletes unrelated content.
Choose the limits from actual available capacity and export requirements.

This first profile requires `cache_max_bytes: 0` and uses Restic `--no-cache`.
Explicit `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` credentials must be in
the supervised process environment. Inherited Restic options and AWS profiles
cannot redirect the operation. The default schedule remains `daily` (24 hours).
Database sources retain consistent native exports or the SQLite backup API;
their staging writes are checked before writing, and unsupported combinations
fail rather than fall back to copying a live database file. Native Docker and
manual or scheduled replica copies involving a remote primary are currently
refused because they lack the same bounded export/copy support.

A staging-wide nonblocking lease serializes users of that volume. The runner
checks disk pressure even while a child is silent; it terminates a child on a
pressure failure and preserves uncertain-operation reconciliation. Results
include `primary_destination`, separate local/cloud completion, and resource
observations. Failures use `resource_budget_exceeded`. The dedicated filesystem
provides a hard staging-capacity ceiling. Free-space reserve checks are observed
thresholds, not a guarantee against unrelated writers between checks; results
report `free_space_reserve_hard_guaranteed: false`. Observed changes in filesystem
use are not attributed exclusively to the backup process.

Completed remote database exports are unique recovery artifacts, not disposable
cache. Their operation-owned staging files and private hash records survive a
failed or uncertain upload, cancellation, or receipt failure. New captures stay
blocked while exports remain. Successful reconciliation checks the exact remote
capture tag and manifest before repairing a receipt and releasing the matching
stage; it never repeats the upload. A reviewed failed outcome retains the data.
Before reconciling a remote capture, stop repository writers for the entire
inspection and explicitly pass `--quiescent-window-confirmed` alongside
`--repository-reviewed`. Inspection uses Restic's `--no-lock` to avoid writing
cloud lock objects and refuses observed repository locks before and after its
reads. These observations cannot prevent another machine from starting a writer;
the operator must maintain the quiet window until reconciliation returns.

Discover retained capture IDs and current artifact-record digests, including
captures stopped before dispatch:

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  runs retained-exports --repository cloud --limit 20 --offset 0
```

The counts are declared metadata, not file-hash verification. After independently
recovering the needed export or deliberately accepting its loss, record the
reviewed failed outcome through `runs reconcile` and inspect the updated record
digest. Explicit disposal is then bound to that exact record:

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  runs discard-exports --repository cloud --operation-id EXACT_OPERATION_ID \
  --artifact-record-sha256 EXACT_CURRENT_RECORD_SHA256
```

The command refuses stale records or foreign files and keeps its disposal audit.
Do not remove staging files or edit private records by hand.

For survival across host reboot or power loss, use persistent dedicated staging.
On `tmpfs`, exports survive process exit but disappear with the host's memory;
the persistent record cannot recreate them, and reconciliation fails closed.

### Credential roles

Least-privilege source credentials are the standard production profile: read,
list, and new writes within one machine's encrypted repository, with historical
backup versions protected against permanent deletion or alteration. Management
credentials are also supported when explicitly supplied for provisioning,
verification, retention, or recovery. Keep those roles and their process
environments separate. A management key's failure to satisfy the restricted
source profile does not make it unusable for its intended management operation.
Supplying a broad key never expands the reviewed target or retention grant.

For Backblaze's S3 route, use a standard application key with the capabilities
needed for that role. A master key can remain in the native-API provisioning
environment; do not transfer it into S3 credential variables. Provisioning an
application key requires `writeKeys`. See Backblaze's
[application-key roles](https://www.backblaze.com/docs/cloud-storage-application-keys)
and [key-creation API](https://www.backblaze.com/apidocs/b2-create-key).

For local development, distinct names such as `BBACKUP_B2_PROVISIONER_KEY_ID`
and `BBACKUP_B2_PROVISIONER_KEY` avoid collisions with source credentials. Transfer
only the intended role's values into the provider's standard environment names
inside that operation's private process. Never put provisioning keys on a source
host by default. B2 `writeFiles` permits hiding names and uploading replacement
versions; strict append-only current names are not claimed. Tests must prove
that protected historical recovery points remain unchanged and recoverable.

Native B2 inspection uses pinned Backblaze SDK retries and runs in a supervised
child process with a 120-second deadline. Cancellation stops and reaps the
process group, including SDK backoff waits. Credentials remain in memory.
Tests exercise SDK backoff for throttling and transient service failures.

S3 inspection and recovery use botocore's standard retry mode with five total
request attempts, a ten-second connection timeout, and a thirty-second read
timeout. Recovery checks cancellation between objects and during streamed reads.
An individual in-flight SDK request may wait for its timeout before cancellation
is observed. Restic transfer processes use the shared timeout and cancellation
boundary. The application does not layer blind mutation retries on top of either
storage engine.

A broken object stream leaves partial recovery output for inspection; transparent
mid-object resume is not claimed. Never silently retry an uncertain destructive
operation. Follow the [operation reconciliation guide](development/version-2.md#operational-limits).

## Storage support and recovery protection

A working destination establishes where a backup can be stored, not production
qualification. The limited B2 source-path drill is recorded, but B2 and AWS
remain outside the selected stable-release support boundary. AWS still has no
live capture/check/restore result. Thirty-day recovery after host compromise
additionally depends on credentials, version retention, lifecycle overlaps, an
independent signed checkpoint, and an actual historical-version restore drill.
The workstation historical-version restore passed, while a guaranteed thirty-day
window, independent custody and replacement-machine recovery remain unqualified.

Keep current repository objects indefinitely: old deduplicated packs can still
be required by recent snapshots. Retain hidden or overwritten versions for at
least thirty days after they become noncurrent. Inspect every overlapping rule.
Backup-host credentials must not permanently delete versions or administer the
bucket, lifecycle rules, keys, or retention. Test lock cleanup with the exact
restricted credentials before scheduling backups.

Native provider checks report `protection_qualified: false` until the necessary
evidence exists. They do not claim immutable storage or unrestricted ransomware
protection.
See [independent recovery](development/version-2.md#independent-recovery-artifacts)
for checkpoint creation and recovery without the original host. Off-host custody
is also deferred; a local escrow drill is not independent custody.
<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
