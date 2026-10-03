# Cloud storage: Backblaze B2 and Amazon S3

Keep your first recovery point on local storage. Then copy that exact successful
snapshot into an independently encrypted Backblaze B2 or Amazon S3 repository.
Either repository can supply a restore, so a failed host does not have to be the
machine that brings your files back.

**B2 and S3 backup/restore support is implemented.** Live provider qualification
and the thirty-day host-compromise protection checks are separate release gates.
The local Garage S3 test has passed capture, copy, full-data checking, and restore.

## Choose a connection

| Storage | Repository binding | Credential variables | Recovery checkpoints |
|:--|:--|:--|:--|
| Backblaze B2, recommended S3 route | `s3:https://s3.REGION.backblazeb2.com/BUCKET/restic` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Implemented S3 inventory path |
| Amazon S3 | `s3:https://s3.REGION.amazonaws.com/BUCKET/restic` | AWS credential provider chain | Implemented S3 inventory path |
| Backblaze B2, native restic route | `b2:BUCKET:restic` | `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY` | Native B2 inspection; use S3 route for checkpoint creation |

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

Keep local snapshots awaiting required replication. The preview does not prune
them automatically. A failed copy remains a failure even when local capture
succeeded; reconcile uncertain operations before retrying.

## Retries, backoff, and interruption

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

A working destination establishes where a backup can be stored. Thirty-day
recovery after host compromise additionally depends on credentials, version
retention, lifecycle overlaps, an independent signed checkpoint, and an actual
historical-version restore drill.

Keep current repository objects indefinitely: old deduplicated packs can still
be required by recent snapshots. Retain hidden or overwritten versions for at
least thirty days after they become noncurrent. Inspect every overlapping rule.
Backup-host credentials must not permanently delete versions or administer the
bucket, lifecycle rules, keys, or retention. Test lock cleanup with the exact
restricted credentials before scheduling backups.

The preview reports `protection_qualified: false` until the necessary evidence
exists. It does not claim immutable storage or unrestricted ransomware protection.
See [independent recovery](development/version-2.md#independent-recovery-artifacts)
for checkpoint creation and recovery without the original host.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/best-backup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
