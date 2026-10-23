# Version 2 development

> Stable v2.0.5 production qualification retains the tested core scope recorded
> below. Experimental additions remain unqualified. The dated records retain
> historical evidence and limits without expanding that scope.

## Unreleased layout and dependency maintenance

The source checkout includes compact-terminal layout repairs for the production
dashboard, guided setup and retained Rich dashboard. Forms scroll within the
available viewport, setup navigation stays reachable, and compact status gives
failures and cancel hints priority. Resource summaries retain each resource
category when individual names cannot all fit. The README and documentation
index put task navigation ahead of detailed support history.

The maintenance batch also reviews the Python updates proposed in
[#28](https://github.com/CruxExperts/BestBackup/pull/28) and pinned GitHub Actions
updates in [#20](https://github.com/CruxExperts/BestBackup/pull/20). Ruff's prior
rule profile is explicit so a tool upgrade does not silently change the
repository's lint policy. The [dependency evidence](release-dependencies.json)
binds the reviewed source dependency graph and records advisory-query limits.
The published v2.0.5 report remains available in its
[versioned source](https://github.com/CruxExperts/BestBackup/blob/v2.0.5/docs/development/release-dependencies.json).

The B2 SDK update honors a server's `Retry-After` delay on 5xx service errors.
The retry regression covers both 429 and 503 responses, including the fallback
when that header is absent. Inspection deadlines and cancellation still bound
the SDK subprocess; this offline check adds no live provider qualification.

These are unreleased source changes. They do not publish new artifacts or
expand Docker, cloud-provider, off-host recovery or complete-machine support.

On 2026-10-23, the final local maintenance candidate passed 1,295 tests with
one skipped test in 114.24 seconds using three workers. This includes the
47 dashboard and TUI cases and the 45 B2 cases covering the upgraded SDK.
Headless captures confirmed the populated/error-state Rich layout at 60x20
and 80x24, narrow dashboard navigation and restore controls at 60x20, and the
normal 112x36 dashboard preview. Documentation previews were inspected at
narrow and wide widths. Independent review found no remaining blocking issues.
The skipped legacy Docker volume roundtrip still does not qualify that path.
Ruff, Python compilation, version synchronization, generated CLI/schema checks,
Markdown, artwork provenance, publishing readiness and whitespace checks passed.
The wheel and source archive built and passed isolated installed-artifact smoke
checks. These checks validate the source candidate without publishing a release.

## Unreleased maintenance (issue #30)

The source checkout implements the reviewed portions of
[#30](https://github.com/CruxExperts/BestBackup/issues/30): bounded repository
lock inspection and explicitly approved stale unlock, optional `local_last`
and `local_weekly` counts, strict parsing of valid Restic keep-all output,
read-only B2 lifecycle capability compatibility, and consistent cache policy
for nested Restic commands in minimal environments.

These changes use the existing enrolled production dispatcher, leases and
reconciliation barriers. Unlock is limited to the reviewed own-host, same-user
dead-process lock set and requires an externally coordinated quiet window.
Local retention preserves recorded required-replica obligations and replans
before applying exact snapshot IDs. See the
[maintenance quick start](../../QUICKSTART.md#review-local-retention-and-stale-locks-unreleased)
for configuration and commands.

On 2026-10-23, the local Python 3.12 suite passed with 1,287 tests and one
skipped test, including integration tests with Restic 0.19.1 and an available
Docker daemon. The disposable lock drill preserved an existing snapshot,
planned keep-all retention with 12 last/14 daily/8 weekly counts, refused a live
lock, and removed its reviewed stale lock after the owned process was killed.
Ruff, compilation, version and generated-command checks, Markdown, artwork
provenance, publishing readiness and whitespace checks passed. The wheel and
source archive built and passed installed-artifact smoke checks. Independent
review found no remaining blocking correctness findings. These are development
results, not new published-artifact or production qualification.

The existing legacy Docker volume restore roundtrip skipped after its backup
step returned failure. A focused rerun confirmed that skip; this validation
does not qualify that restore path.

The live-lock integration fixture waits for a stable inventory containing its
owned Restic process. It tolerates only the explicit inventory-change refusal
during bounded startup; the production inspector still rejects changing lock
sets, and uncertain unlocks are never retried. This covers slower hosted lock
acquisition without weakening the maintenance safety checks.

This is unreleased source work, not acceptance of private candidate builds or
permission to modify deployed installations. The package version remains
2.0.5 until a separate release. No B2/AWS protection, off-host custody,
thirty-day recovery window or complete-machine recovery qualification is added;
the canceled scopes of #23-#26 remain canceled and unqualified.

## v2.0.5 release scope and known limits

The v2.0.5 stable qualified scope is unchanged from v2.0.4: local file capture
and restore; live capture and restore for PostgreSQL 17.11, MySQL 8.4.12, and
MariaDB 11.8.9; headless dashboard happy paths; and AMD64 Ubuntu 24.04/26.04
packages. The separate user-timer reboot qualification remains limited to the
components and conditions recorded in its dated evidence below.

This patch accepts a positive, explicitly configured `grace_seconds` value for
experimental central retention; thirty days remains the recommended baseline.
Shorter grace reduces the grace-based historical protection period and does not
establish a thirty-day recovery guarantee. B2/AWS protection, aged reclamation,
off-host custody, and production recovery remain unqualified. Issues #23–#26
were canceled and closed as not planned; cancellation does not qualify their
experimental workflows. This release adds no new live provider qualification;
the historical workstation evidence below remains unchanged.

## v2.0.4 release scope and known limits (historical)

The v2.0.4 stable qualified scope is unchanged from v2.0.3: local file capture
and restore; live capture and restore for PostgreSQL 17.11, MySQL 8.4.12, and
MariaDB 11.8.9; headless dashboard happy paths; and AMD64 Ubuntu 24.04/26.04
packages. The separate user-timer reboot qualification applies only to the
components and conditions recorded in its dated evidence below.

This release also ships experimental restricted B2 source-key assessment and
central recovery acceptance, central maintenance and noncurrent-version
retention, HTTPS S3 remote-primary capture with bounded staging, and opt-in
reviewed Linux file and SQLite coverage with a manual PostgreSQL covered-capture
path. These additions are implemented workflows, not new production support.
They do not establish B2 or AWS protection, a guaranteed thirty-day recovery
window, off-host custody, final remote-server qualification, complete-machine
recovery, or general automated PostgreSQL coverage. `protection_qualified`
remains false. Issues #23–#26 were canceled and closed as not planned; their experimental
workflows remain unqualified. This release does not expand the tested support scope.

On 2026-10-21, the v2.0.4 candidate passed the full local suite with 1,242
passed tests and one skipped test. The exact wheel and source archive built
and passed isolated installed-artifact smoke tests. Ruff, Python compilation,
version alignment, generated CLI catalogs, Markdown standards, publishing
readiness, and artwork provenance checks passed. The dependency audit found
and corrected the affected `certifi` pin to 2026.10.19; exact-version lookups
for the corrected dependency returned no advisory records at retrieval time.
These checks do not expand the production qualification boundary above.

## Direction and delivery order

The v2.0.4 stable release scope, retained in v2.0.5, is local/file capture and restore,
live capture-restore drills for PostgreSQL 17.11, MySQL 8.4.12, and MariaDB
11.8.9, headless dashboard happy paths, and AMD64 Ubuntu 24.04/26.04 packages.
The CLI and Textual dashboard share one operations service. Existing 1.x commands
remain available. This is a bounded tested-scope release, not qualification of
every implemented feature.

Earlier qualification assessed the wider implemented feature scope, including
B2/AWS, native Docker, ARM64 outputs, ordinary cross-application upgrades and
rollback, and systemd boot/reboot behavior. Those paths did not have the
required acceptance evidence and are not included in the selected stable
support boundary.

Delivery proceeds through core/configuration, capture/restore, cloud protection
and independent escrow, CLI/TUI, then packages/documentation/release qualification.
An implemented capability does not establish completion of its entire delivery slice.

## Implemented capabilities

### Experimental cloud and machine recovery features

Issue [#23](https://github.com/CruxExperts/BestBackup/issues/23), for restricted
B2 recovery, was canceled and closed as not planned. The implementation and live
trials remain experimental and do not qualify B2 protection. Central retention
([#25](https://github.com/CruxExperts/BestBackup/issues/25)) was also canceled and
closed as not planned; its validated configurable-grace fix is retained.
Remote-primary disk budgets
([#26](https://github.com/CruxExperts/BestBackup/issues/26)) were also canceled and
closed as not planned, retaining the existing experimental implementation.
Integrated privileged Linux recovery
([#24](https://github.com/CruxExperts/BestBackup/issues/24)) was likewise canceled
and closed as not planned. All four requests are now closed by cancellation,
not qualification. Existing implementations and historical evidence are retained.
Any future qualification requires focused regressions, independent review and
final source/artifact validation against the claimed scope. A mocked provider
result cannot qualify B2; the live trials below do not establish new support.

The experimental B2 slice adds a restricted source-key metadata assessment to
native B2 inspection and an independent `recovery accept` command. Acceptance
reconstructs signed exact versions using the escrow kit, checks the repository
and snapshot, and signs a central record with a separate verifier fingerprint.
It does not establish independently controlled key custody by itself.

A small live B2 source-path trial and seven denied-operation checks passed on
2026-10-20; details and limits are recorded below. Historical-version recovery
after simulated source compromise also passed on this workstation. A separate
live lifecycle-overlap guard trial verifies only the configured guard and its
central-retention refusal; elapsed lifecycle expiry, physical reclamation, the
thirty-day grace period, measured transfer costs, and independently controlled
off-host escrow remain outstanding. No live AWS result is recorded.
Complete privileged Linux capture and replacement-machine recovery also remain
unqualified. Do not close these issues or expand the supported release scope
from fixture tests.

### Reviewed Linux coverage (experimental)

An opt-in schema-3 job can declare `coverage_policy` and map its job name to a
private `coverage_declarations` path in host bindings. Ordinary jobs keep their
existing source-selection behavior. Coverage discovery records bounded Linux
account/group, mount, package, application-unit and selected-tree metadata in
private state. Public results contain counts, digests and reason codes only.
`coverage_status: complete` means the exact declared scope is accounted for;
it is not a claim that every machine file or application is recoverable.

Restic service operations use an explicit cache-free fallback when their child
environment provides neither `HOME` nor `XDG_CACHE_HOME` and no cache option was
selected. This supports verified restore from minimal system-service
environments without relying on an interactive login environment.

For example, this is a portable job-field fragment:

```json
{
  "coverage_policy": {
    "schema_version": 1,
    "approved_roots": ["system", "homes"],
    "require_privileged": true,
    "filesystem_boundaries": "explicit",
    "metadata_expectations": {
      "numeric_owner": true, "mode": true, "acl": true,
      "xattr": true, "links": true
    }
  }
}
```

The separate mode-0600 declaration is schema 1. `roots` maps each approved root
ID to an absolute `path`, configured `source_id` and `adapter`. It also requires
`exclusions`, `mount_decisions`, `identity_decisions`, `applications`,
`package_reconstruction`, and `reconstruction_evidence`. Empty arrays are
permitted, but unresolved inventory gaps block approval. See the maintained
[fixture declarations](../../tests/test_linux_coverage.py) for the exact shapes.
Covered capture supports ordinary files and individually transactional SQLite
exports. Each SQLite root requires an application declaration whose state root
is that exact database path and whose reconstruction procedure has verified
private evidence. An experimental manual PostgreSQL extension is described
below. MySQL/MariaDB and Docker remain unsupported for covered jobs; ordinary
jobs retain their existing adapters. Existing schema-1 private declarations
and the portable schema-1 coverage policy keep their current shapes. The
PostgreSQL extension uses a schema-2 private declaration and does not change
ordinary native PostgreSQL capture or restore.

Application declarations use service-unit stems without the `.service` suffix.
Ordinary names, templates and instances such as `serial-getty@ttyS0`, including
valid systemd hex escapes, are supported. Names remain literal inventory keys;
malformed escapes, path separators and overlength names fail discovery.

SQLite review binds the database's device/inode, numeric owner, mode, link count
and bounded extended attributes, including filesystem ACL attributes when
present. The database must be a readable regular file without symlink ancestors
or additional hard links. Replacement or metadata changes require fresh review;
ordinary database writes and journal-file churn do not. Unsafe journal links and
user exclusions covering the database prevent approval.

Capture uses the existing SQLite backup API and omits the live database and its
`-wal`, `-shm` and `-journal` files from file-tree capture. The encrypted capture
manifest binds the export hash to the reviewed root, source identity, original
metadata, reconstruction recipe and baseline. Generic restore returns that
export and manifest; the reviewed reconstruction procedure must place the
database and restore its original metadata. This does not provide automatic
in-place application recovery or a shared transaction across files or databases.

#### Experimental PostgreSQL covered capture

The private schema-2 declaration adds `database_roots` for selected logical
databases and one `postgresql_clusters` entry. Each captured database maps to
the exact portable PostgreSQL source and private connection alias; the alias
and source selector must agree with the private host bindings. Here is only the
logical-root fragment, not a complete declaration; see the maintained
[PostgreSQL coverage fixture](../../tests/test_postgresql_coverage.py) for the
full valid shape:

```json
{
  "database_roots": [
    {"id": "application_db", "adapter": "postgresql",
     "source_id": "application_source", "cluster_id": "primary",
     "connection": "private_connection_alias", "database": "application"}
  ]
}
```

The declaration accounts for every database in the selected cluster,
including templates and databases that reject connections. Each gets an
explicit `capture` or `reconstruct` decision tied to its current name and OID;
nonconnectable databases must be reconstructed. A reconstructed database needs
actual procedure-execution and result-verification evidence bound to the
reviewed database and cluster inventory. The local administrative connection
must have the native inspection privileges needed to read cluster, database,
role, membership, and configuration metadata. `pg_controldata` must be
available on `PATH` at the matching major version. Its control-file system ID
must equal the SQL-reported ID, and SQL `data_directory` must exactly match the
declared absolute local path in the same filesystem namespace. Review also
binds that directory's device, inode, numeric owner and group, and mode, plus digests for
the private connection and password files, database and global inventories,
roles, authentication files, and configuration.

Only one local primary cluster is supported. `pg_wal` must be inside the same
filesystem as the declared data directory; non-default tablespaces, external
WAL, external configuration or authentication includes, authentication
`@file` references, SSL-enabled arrangements, standbys, and unsupported
extensions, languages, or foreign objects are rejected. This is a bounded
local path, not general PostgreSQL cluster backup support.

The administrator must coordinate a quiet window for role, database,
configuration, and schema changes throughout discovery, review, checks, and
capture. Normal row writes may continue. The boolean confirmation defaults to
false and does not stop external changes; scheduled covered PostgreSQL jobs
remain blocked because their timer dispatch cannot assert that window. Set the
flag only while that external coordination is active. A capture uses one
transactional native `pg_dump` custom archive per selected database. It does
not create one shared transaction across databases or between database and
file roots.

After database accounting and restoration evidence pass, the literal reviewed
`data_directory` (including its live raw files and `pg_wal`) is excluded from
file-tree capture. Recovery procedure, evidence, and actual recovery-material
files must be private regular files inside the same job's approved file roots,
outside the data directory and declared exclusions. Individual file roots are
allowed. Their bytes are included in the encrypted snapshot. Its encrypted
manifest binds the reviewed approval, each database export hash, and each
recovery-artifact hash. Covered PostgreSQL restore verifies the archive and
all referenced recovery-material bytes from that snapshot before its first
target-database mutation. Existing trusted-archive acknowledgement and new
target database safeguards still apply. The source connection alias is
provenance only: restore uses a fresh destination-host connection alias and
does not require the original source binding.

Captured databases also require a reviewed reconstruction procedure for
database-level metadata. Native restore creates a new database with the
captured encoding and locale, then runs `pg_restore`; it does not restore the
database owner, database ACL, or connection limit. The recovered procedure
must apply and verify those settings for the chosen destination name,
including PUBLIC revocations and grant options, and account for other reviewed
database attributes. Its execution evidence must
compare the reviewed source metadata with a reconstructed database before
coverage review and capture. Repeat that procedure from the recovered materials
after the final restore. The native `database_validated` result means that
`SELECT 1` succeeded after archive restore; `application_validated` remains
false. Neither result establishes completion of the reconstruction procedure.

Use the manual commands only during the coordinated window; replace the
candidate digest, snapshot ID, and evidence path with the reviewed values:

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage discover --name application-stack --quiet-administrative-window-confirmed
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage review --name application-stack --candidate-digest FULL_CANDIDATE_SHA256 \
  --quiet-administrative-window-confirmed
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage check --name application-stack --quiet-administrative-window-confirmed
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage recovery-evidence --name application-stack --snapshot FULL_SNAPSHOT_ID \
  --evidence-file PRIVATE_EVIDENCE_FILE --quiet-administrative-window-confirmed
bbackup production --config config.v3.json --bindings bindings.v3.json \
  jobs run --name application-stack --quiet-administrative-window-confirmed
```

On 2026-10-21, a local PostgreSQL 17.11 rehearsal passed in two isolated
containers with distinct machine identities, data directories, and recovery
workspaces. The source passed native enrollment, coverage discovery, review,
readiness checks, and manual capture. Its final encrypted snapshot contained
16 files totaling 10,006 logical bytes, including a 2,307-byte database archive
and five digest-bound recovery files; no raw PostgreSQL data-directory nodes
were present. A full repository read-data check passed over four snapshots
and eight packs. Adding an unaccounted database separately blocked coverage
and manual capture while preserving the accepted snapshot.

The replacement restored from a copy of the encrypted repository and separate
test password escrow, using a fresh connection alias and a previously absent,
differently named target database. The native restore verified all five recovery
file hashes and completed in 6.82 seconds; this measures that command alone,
not application reconstruction or a production recovery deadline. The corrected
reconstruction procedure was executed and verified before final coverage review
and capture, then recovered from that snapshot and executed again. Separate SQL
comparisons matched database ownership, all three ACL entries, connection limit
7, table ownership and reader permissions, and three committed UTF-8 rows.
An application-role query returned all three rows. The CLI still reported
`application_validated: false`; these separate fixture checks supply the
application evidence.

This is local emulation of the bounded manual path. It does not qualify
off-host custody, provider access, whole-machine recovery, or unattended
PostgreSQL coverage. The path ships experimentally in v2.0.4 and adds no
qualification to the v2.0.3 core scope; native PostgreSQL support for ordinary
jobs is unchanged.

The full regression run reported 1,241 passed, one known Docker skip, and one
dashboard assertion failure. That assertion was updated to expect the new
quiet-window field's false default; all seven dashboard tests then passed.
Runtime code was unchanged between those runs. Version, publishing-readiness,
generated-interface, compilation, Markdown, lint, and diff checks passed.

Each exclusion or reconstruction decision names a reason, consequence, recipe
ID and procedure SHA-256. A referenced private procedure and private evidence
record must match their digests and identify verified local execution. These
records account for local scope decisions; they do not independently establish
machine recovery. Symlinked homes or application state outside approved scope,
unreadable data, unaccounted users/services/mounts and missing reconstruction
evidence leave coverage incomplete. Declared exclusions become literal Restic
exclusion patterns during capture, including paths containing glob characters.

```bash
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage discover --name system
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage review --name system --candidate-digest EXACT_CANDIDATE_SHA256
bbackup production --config config.v3.json --bindings bindings.v3.json \
  coverage check --name system
bbackup production --config config.v3.json --bindings bindings.v3.json \
  jobs run --name system
```

Discovery never approves coverage. Review binds an exact complete candidate to
the enrolled machine, user, configuration, bindings, declaration and scope.
Capture checks the current baseline again and refuses changed mappings or
structural drift. Coverage receipts are separate per declaration/scope. Secrets
inside an explicitly reviewed file root may enter the encrypted snapshot;
runtime state, repositories and staging remain excluded from source overlap.

`service render-system` renders root service/timer units without installing
them or elevating privileges. It requires root identity and root-controlled
executable, configuration, bindings and path ancestors. The system timer is
persistent and the service dispatches through the same production command.
Review and install those units deliberately using the system administrator's
normal procedure. User-level `service install` remains unchanged.

Units for opt-in covered jobs use `PrivateTmp=no` so scheduled discovery and
capture see the same reviewed `/tmp` and `/var/tmp` sources. Ordinary jobs retain
`PrivateTmp=yes`; their existing hardening is unchanged. Coverage still refuses
runtime-state/repository recursion and detects real scoped mount changes.

`coverage recovery-evidence` assesses a private record against the exact current
baseline and snapshot. Local fixture records can produce `recovery_status:
emulated`; absent, incomplete or unverified independent records stay `untested`.
No command marks complete-machine recovery qualified. Current tests include
small real Restic capture/restore with enforced exclusions, account and service
drift, private evidence integrity and unprivileged readability. A local WAL-mode
SQLite/files drill restores committed rows, omits uncommitted rows and raw
database/journal files, validates export provenance, and reconstructs original
metadata with the reviewed recipe. Privileged
complete-machine recovery and final remote-server qualification remain
unproven; #24 was subsequently canceled and closed as not planned.

On 2026-10-21, the opt-in `covered-root` scenario in
[`qualify_systemd.py`](../../scripts/qualify_systemd.py) passed a separate
workstation rehearsal in two fresh Ubuntu 24.04 AMD64 guests. Both installed
the exact wheel built from signed commit `3755ee0`; its SHA-256 was
`7106094a242975cc51365d45c72f5518fb76eb2a0feadaee3a4211edd85748da`.
The source used actual Linux inventory, root-owned enrollment and rendered
system units, then captured through the root timer and performed a verified
production restore. The replacement used a distinct machine identity and OS
overlay; it received the dedicated recovery disk and candidate inputs, without
access to the source OS overlay or a plaintext application-data fallback.

All twelve evidence checks passed: committed-only SQLite export and manifest
hashes, raw database/sidecar exclusion, restored synthetic secret and application
output, reconstructed fixture account, and numeric ownership, mode, hard/symbolic
links, extended attributes and an actual named-user ACL. Reconstruction executed
the reviewed procedures and started the restored application through systemd;
the fixture and backup units were verified afterward. The logical fixture contained
56,293 bytes on a 256 MiB recovery disk. The two-guest run took 90.1 seconds;
replacement completion appeared 18.5 seconds after its kernel boot, including
5.8 seconds in its qualification stage. These small-fixture measurements do not
estimate production recovery time. The harness's 39 focused tests passed.

The result is `local-emulation-only`. Shared local recovery media does not
establish independently controlled off-host escrow, provider protection, or
complete-machine recovery. It does not extend the published v2.0.3 support
scope or complete the final remote-server acceptance.

### Remote-primary and central retention (experimental)

The experimental remote-primary implementation adds HTTPS S3 primary repositories,
versioned resource budgets, a dedicated capacity-bounded staging mount, disabled
cache, a shared staging lease, export write checks, and continuous runner
pressure checks. Completion results distinguish local and remote captures;
status exposes remote-primary capture separately from local capture. Free-space
reserves remain observed thresholds, and unsupported Docker/replica combinations
are refused. These implementation checks do not qualify B2 or whole-machine
recovery. A small live B2 workstation source trial is recorded below; the final
remote-server drill and production recovery qualification remain deferred.

Remote database captures preserve completed exports in operation-owned staging
with private size/hash records until a successful receipt or verified read-only
reconciliation permits cleanup. Failed or uncertain uploads, receipt failures
and later-export failures retain completed artifacts and block new captures.
`runs retained-exports` exposes bounded IDs, declared counts and record digests,
including pre-dispatch captures without a ledger row; `runs list` remains a
ledger event stream. `runs discard-exports` requires the exact current record
digest and a persisted reviewed failed outcome, verifies owned files under the
staging lease, and keeps a disposal audit. It does not upload or replay a capture.
Persistent dedicated staging is required for reboot survival; `tmpfs` retains
exports only across process exit. Missing or changed retained data fails closed.
Remote capture reconciliation requires an explicitly acknowledged quiet window
and uses Restic `--no-lock` reads to avoid creating cloud lock objects. Lock
observations before and after inspection reject active locks but cannot enforce
quiescence across machines; repository writers must remain stopped throughout.

The experimental central retention slice adds explicit catalog enrollment and
private recurring grants, signed central acceptance selection, exact manifest
dependency preservation, central observation-based noncurrent grace, conservative
lock/lifecycle checks, and single-request journaled deletion with read-only
reconciliation. It uses the enrolled repository's shared lease. Missing protected
versions or catalog records stop deletion. A schema-2 B2 policy can select a
native lock-metadata reader; schema-1 generic S3 policy behavior is unchanged.
This reclaims only noncurrent object versions; aged B2 reclamation, AWS behavior
and production savings remain outstanding for #25. Focused tests
include actual GPG-signed acceptance records and uncertainty before/after deletion.

The separate experimental `cloud-maintenance` workflow uses the same central
catalog and independently accepted records to retire only visible accepted
snapshots that are no longer protected. It leaves unknown snapshots untouched,
uses a separate explicit Restic credential profile, and runs no-repack prune
before any later historical-version reclamation. Policy schema 3 and action-bound
schema-2 grants bind resource limits and immutable plans; explicit record
enrollment preserves established acceptance and first-seen state. Read-only
inspection requires operator-maintained quiescence and uses `--no-lock`, while
mutations keep native locks and journal intent without automatic replay.
Healthy reconciliation alone does not certify completed prune. Repacking,
aged live deletion and the promised recovery window remain unqualified.
See the [central maintenance workflow](../cloud-storage.md#central-snapshot-maintenance-experimental).

#### Issue #25 configurable grace (v2.0.5)

The v2.0.5 central retention policy accepts a positive `grace_seconds`
value; thirty days is the recommended baseline, not a hard-coded minimum.
Eligibility is measured from central observation of a noncurrent version rather than its
upload timestamp. The recent live trials do not establish an elapsed configured
grace interval. Their zero-forget maintenance, stale-plan rejection and
interruption results do not demonstrate aged permanent deletion or physical
reclamation.

[Issue #25](https://github.com/CruxExperts/BestBackup/issues/25) was canceled and
closed as not planned at the user's request. Any future qualification must
preserve the original observation catalog and accepted recovery dependencies
through the policy's configured grace period. Then use an exact-target
authorized plan to exercise eligible aged B2 versions, verify locked and held
versions survive, and restore the retained recovery points. Record the exact
source revision, policy and grant revisions, plan identity, provider
observations and outcomes without publishing credentials. Do not backdate
observations, bypass provider locks, or replay an uncertain deletion to
manufacture acceptance evidence. No new live deletion or recovery result is
claimed by this review.

The configurable-grace candidate passed the full local suite on 2026-10-22 UTC
with 1,253 passes and one skip using three workers. The focused retention file
contains 59 tests, including actual apply at the configured observation boundary
and held-version rejection. Ruff, version synchronization, publishing readiness,
Markdown standards and generated CLI checks passed. Independent review found no
material issue with policy/grant binding or the existing deletion safeguards.
These checks do not establish live B2 reclamation or provider qualification.

Local validation includes a real three-snapshot Restic repository: the exact
obsolete accepted ID was forgotten, no-repack prune completed, and both the
protected and unknown snapshots restored afterward. That drill uses a test
double for the central cloud session; it is not live B2 maintenance evidence.
At maintenance-slice acceptance, the runtime suite passed 1,158 tests with two
skips. The Unix-socket case skipped
because the test temporary path exceeded the platform limit, then passed in a
separate short-path run. The legacy Docker roundtrip still skipped after its
backup returned false and supplies no Docker recovery qualification.

#### Live B2 central-maintenance trial

On 2026-10-21, development code at `d33354a` passed a live B2
workstation trial using the previously measured two-snapshot fixture. Both
snapshots received genuine, separately signed central acceptance records.
The reviewed immutable plan protected both snapshots and their ten dependency
versions, covering 201,834 current payload bytes, and selected no snapshots to
forget. Production CLI execution completed all six phases, including an actual
`prune --max-repack-size 0`. A zero-forget plan still performs native mutations.

A subsequent full read-data check and restores of both snapshots passed.
Payload, hidden-file, symlink, and initial/incremental contents matched the
fixture. All 32 versions observed before maintenance remained present, and the
central catalog digest was unchanged. The final inventory added 16 lock-object
versions and 16 hide markers, with no new index versions; permanent version
deletion was not observed. These changes include maintenance and its subsequent
verification, rather than measuring prune alone.

The isolated central process used distinct read-only verifier and inspection
keys and a bucket/prefix-restricted Restic read/list/write key. Provisioning and
permanent-deletion credentials were absent. The maintenance phase used a 1 MiB
staging mount, a configured 65,536-byte free-space threshold, and disabled cache.
Checkpoint preparation and independent acceptance used a separate central
workspace; that preparation is not covered by the maintenance disk budget.

A separate changed-plan scenario created a fresh zero-forget plan, then wrote
one eight-byte probe under the same restricted repository prefix. Its single
stale apply was rejected before maintenance-ledger entry, forget, or prune.
All 65 previously observed versions, including the probe, remained exactly
unchanged. Normal pre-validation Restic inspection created and released two
temporary locks, adding two object versions and two hide markers. This is not
a zero-object-mutation guarantee.

The original fault helper stopped because it required an unchanged version
inventory even for inspection locks. Its blocked result remains preserved.
A read-only continuation bound the exact plan, rejection receipt, before/after
inventories and four new lock versions, without repeating apply. A no-lock
full read-data check passed, both protected snapshot identities remained
visible, all 69 post-inspection inventory rows stayed unchanged, and the
catalog digest was unchanged. The eight-byte probe was deliberately retained.

This qualifies only the small, zero-forget maintenance exercise. It does not
demonstrate aged reclamation, an elapsed protection window, independently held
off-host escrow, or final remote-server recovery. `protection_qualified` remains
false. The feature ships experimentally in v2.0.4; #25 was subsequently
closed as not planned without expanding qualification.

#### Live B2 central-maintenance interruption trial

A separate 2026-10-21 live workstation trial exercised interruption recovery
with one zero-forget plan and one small, approximately 200 KiB fixture; it made
no additional backup sets. A temporary wrapper limited downloads to 1 KiB/s
only for native `prune`, making that phase easier to observe with the small
fixture. One SIGINT was
sent to the owned CLI only after the actual native prune process and its pinned
launch identity, durable prune intent, and a fresh current-lock witness were
verified. The native process stopped and the CLI exited with status 3. The
operation ledger and production prune phase both recorded `uncertain`; the
drill does not claim prune completed.

After Restic processes were confirmed quiescent, one production read-only
reconciliation recorded `partial_abandoned` and `reconciled_abandoned`, while
the prune outcome remained uncertain. The apply was not replayed. A no-lock full
read and fresh restores of both protected snapshots matched payload,
hidden-file, symlink, and incremental-file expectations. All 69 previously observed provider objects
and markers survived, the catalog stayed unchanged, and no permanent version
deletion was observed. This is evidence for one small interrupted maintenance
operation and recovery check; it does not qualify prune completion, aged
reclamation, the protection window, off-host recovery, or production recovery.

#### Live Backblaze B2 source trial

On 2026-10-20, the workstation checkout at commit
`4378e6efbb04de35ad41d377e461fe5d170fab1d` passed a small live B2 S3 source-path
trial in a retained isolated Docker container without the whole checkout or
central state mounted and without central or provisioner credentials.
The 196,682-byte source fixture contained a 192 KiB deterministic payload, a
small hidden synthetic credential file, a symlink, and an incremental file.
The drill used a 1 MiB tmpfs staging mount and a zero-byte Restic cache budget.

The source key was restricted to one exact bucket and machine prefix. The SDK
profile reported exactly `listBuckets`, `listFiles`, `readFiles`, and
`writeFiles`. Repository initialization, two captures, a repository check, and
restores of both snapshots passed. The older restore omitted the incremental
file; the newer restore contained it. Both restores preserved the hidden file
and symlink. Separate live canaries uploaded, read, and listed an object, then
replaced and hid it while confirming old versions remained available. Seven
forbidden-operation checks were denied and read back: permanent-version delete,
cross-prefix read, cross-prefix write, key administration, bucket
administration, file-retention administration, and legal-hold administration.
The last two used a fresh, explicitly unprotected disposable canary. The central
retention key then deleted that exact 37-byte version, and independent readback
confirmed its absence. This tests authority on an unprotected object; it does
not test the retention planner's aged candidates. Separate protected-object
checks are recorded below.

This is narrow workstation evidence, not production qualification. No large
backup set was measured. Read-only Restic raw-data statistics reported 197,784
bytes for the initial snapshot, 197,943 bytes for the incremental snapshot, and
198,997 bytes for their union. The union is smaller than the two per-snapshot
totals combined, demonstrating shared repository data across these snapshots.
These raw-data figures do not measure network transfer bytes, bandwidth, or cost
savings.

A fresh isolated fixture at commit
`f34945826bdb177dfa0858dd1dbe9470464980ed` subsequently passed initialization,
both captures, a full repository check and both restores. Its final fixture size
was 196,682 bytes. The source container used an archived copy of that exact
commit, the restricted source credential, a 1 MiB staging mount and no cache.
The controller verified the mount allowlist and unchanged source files; central
and provisioning credentials were absent from its mounts and configured
environment. Initialization observed an empty exact-prefix version inventory.

- Initial capture: interface TX/RX was 221,039/28,033 bytes; provider-reported
  stored-version bytes grew by 199,080, including retained noncurrent versions.
  Observed additional staging use peaked at 208,896 bytes.
- Incremental capture: interface TX/RX was 22,065/29,811 bytes; stored-version
  bytes grew by 2,457. Observed additional staging use peaked at 12,288 bytes.

Interface counters bracketed each capture, excluding the surrounding inventory
requests, and include protocol and any concurrent interface traffic. Stored-byte
totals sum provider-reported object payload sizes, including noncurrent lock
versions; delete markers are counted separately. Neither measure establishes
provider billing or costs. Local filesystem observations remain unattributed.
The smaller incremental growth is fixture evidence, not a large-dataset or
long-term savings estimate. This run did not inject database-export failures or
qualify reboot recovery, remote-server deployment or aged retention.

Initial SDK-wrapped retention errors and rejected canary setup attempts remain
preserved separately; they were not counted as passes or replayed. The successful
fresh canary required observed retention `NONE` and legal hold `OFF` before its
authority checks. A verifier lifecycle request initially received HTTP 403;
after adding only `readBucketLifecycleRules` to the separate central identities,
readback confirmed enabled versioning and no applicable lifecycle expiration
rules. The source key stayed unchanged. Backblaze's
[S3-compatible app-key capability guide](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys)
maps that capability to `Get Lifecycle Configuration`; no
`writeBucketLifecycleRules` capability was added.

A separate central verifier authenticated the source-signed encrypted kit,
reconstructed the exact historical repository versions, checked all data, and
signed an acceptance with a distinct key. The source identity then hid the
configuration object and replaced one data object with invalid bytes. Using the
kit and read-only historical-version access, the central verifier reconstructed
and fully checked the repository again, verified repository/snapshot identity,
and restored the older snapshot with matching payload, hidden file, symlink,
and no later incremental file. Ten repository objects totaling 201,853 bytes
were reconstructed. The named temporary source-password copy was removed only
after baseline acceptance; recovery read its password from the encrypted kit.

The drill exposed a receipt bug after the kit was written: a checkpoint without
an expiry date could not be inserted into the legacy non-null expiry field.
Checkpoint receipts now permit absent expiry, and an existing owning schema is
migrated transactionally after a private compressed backup. Existing rows are
preserved; schemas with additional constraints are refused. The failed attempt
remains recorded, and its authenticated kit was accepted separately without
resealing or replaying the failed operation. No expiry date is invented, and
absence of an expiry does not qualify a guaranteed protection window.

Separate fresh disposable versions tested governance retention and legal hold.
The routine central retention identity, which has no governance-bypass
capability, was denied permanent deletion of each exact version. Independent
readback confirmed both survived, and the native metadata reader observed the
future governance expiry and active legal hold. The provisioning identity then
removed protection and deleted only those test canaries. Earlier inconclusive
attempts remain separate and are not counted as successful denial checks. These
checks do not demonstrate aged candidate deletion or a thirty-day recovery window.

A separate probe created two further 39-byte protected canaries and issued a
versionless S3 delete with the restricted source key. Both governance retention
and legal hold allowed current-name hiding: each acquired exactly one marker,
while independent native inspection verified the original exact version and
unchanged active protection. Current-name reads reported absence. Read-only
reconciliation recorded no uncertain operations, and exact scoped provisioning
cleanup removed only those two uploads and their markers. This confirms that
Object Lock protects historical versions without making current names append-only;
it does not establish a thirty-day window or aged planner behavior.

Thirty-day elapsed grace, overlapping lifecycle behavior, aged retention,
off-host custody, clean replacement, final remote-server qualification, and all
live AWS behavior remain unproven. Central acceptance still records
`protection_qualified: false`. The v2.0.4 stable support scope remains the
v2.0.3 tested core scope.

#### Central B2 retention planning rehearsal

A live rehearsal enrolled a catalog, generated a plan with a fresh
thirty-day first-seen grace period, ran a guarded zero-apply, and reconciled an
empty plan. It counted 10 protected versions in a 31-version inventory; the
before/after inventory matched and no S3 delete was sent. Empty reconciliation
made zero provider reads and does not test uncertain-delete recovery. The S3
Object Lock configuration read returned ambiguous
`ObjectLockConfigurationNotFoundError`. The new native reader separately passed
live reads of both exact original configuration/data versions, normalizing only
authorized null settings and running even when the S3 lock flag was false.
The production CLI then completed setup and deliberate local schema-3
enrollment, followed by schema-2 native-metadata planning, zero-candidate apply,
and empty reconciliation against the same existing catalog and repository
lease. It again protected 10 versions among 31, preserved established first-seen
times and the original policy/grant files, and left the version inventory
unchanged. This verifies the production integration with fresh grace; it does
not exercise an aged deletion or an uncertain mutation. No cost/savings,
off-host, or remote-server qualification is established.

#### Live B2 lifecycle-overlap guard trial

On 2026-10-21, a new `allPrivate` B2 bucket was created with File Lock enabled,
default retention `NONE`, and two overlapping native lifecycle rules. The
broader prefix hides files after 3,650 days; the nested prefix deletes
noncurrent versions after 3,651 days. Backblaze documents that native rules may
overlap and combine the smallest non-null value for each action
([native lifecycle rules](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules)).
Its S3-compatible `Get Lifecycle Configuration` returned the corresponding
three actions: hide after 3,650 days, remove orphan markers, and delete
noncurrent versions after 3,651 days
([S3 Get Lifecycle Configuration](https://www.backblaze.com/apidocs/s3-get-lifecycle-configuration)).
S3 `Put Lifecycle Configuration` rejects overlapping prefixes, so this test
configured the rules natively at bucket creation
([S3 Put Lifecycle Configuration](https://www.backblaze.com/apidocs/s3-put-lifecycle-configuration)).

A one-hour bucket-scoped reader had exactly `listBuckets`, `readBuckets`,
`readBucketLifecycleRules`, and `readBucketRetentions`; it had no object
read/list/write/delete or administration capabilities. Verification ran as a
nonroot process with a read-only root and fixture mounts, a separate writable
work mount, and no Envman or provisioner credentials. Native readback confirmed
the bucket type, File Lock setting, default retention, and both rules; S3
readback confirmed enabled versioning and the three mapped actions. The actual
central-retention bucket inspection raised
`CloudError: Enabled bucket lifecycle expiration blocks central retention`.
The read-only inventory observation found no versions in its first entry; that
receipt is bounded to the first page. No object was uploaded, no bucket setting
was updated, and no deletion was attempted.

This establishes only the configured overlap guard and central-retention
refusal. It does not test elapsed lifecycle expiry, physical reclamation, or
any file-level lock/retention window; no actual file objects were created.
Central acceptance remains `protection_qualified: false`. At the time of this
2026-10-21 trial, requests #23–#26 were open; #23 has since been closed as not
planned, as have #24–#26. Their closure records cancellation, not qualification. The v2.0.4 stable scope remains the v2.0.3
tested core scope.

On 2026-10-20 a workstation-only Garage S3 drill captured a 2,184,000-byte file
fixture directly to the emulator using a 1,048,576-byte staging mount, ran an
incremental capture and full repository check, and restored the older snapshot
including a hidden synthetic credential file and a symlink. Initial/incremental
PUT payloads were 4,979/4,113 bytes; internet backup traffic was zero. The source
was intentionally repetitive and small, so these numbers are fixture evidence,
not performance or cost estimates. This does not qualify provider permissions,
independent disaster recovery or an entire Linux machine.

A second small local drill also refused a SQLite export larger than the 1 MiB
staging capacity before uploading any export data. A subsequent full repository
check passed, preserving the earlier recovery points after the budget failure.
The export source was about 2 MiB; no large backup set or internet transfer was
used.

#### Aggregate-capacity and active-pressure trial

On 2026-10-21, code at `d33354a` captured a small, compressible
2,245,463-byte logical baseline to a local Garage S3 emulator. The isolated
source had a read-only root filesystem and read-only fixture inputs. Its total
writable filesystem capacity was 1,769,472 bytes: 1,048,576 for staging,
524,288 for private state, 131,072 for temporary files, and 65,536 for shared
memory. The baseline exceeded that combined capacity, not just staging.
Restic cache was disabled. Capture used a loopback HTTPS proxy; internet backup
traffic was zero.

A second capture included a new 32-byte trigger. While its native Restic child
was sending a data PUT, a task-owned filler crossed the configured
131,072-byte free-space threshold; the observed minimum was 126,976 bytes.
The runner stopped the child and reported `resource_budget_exceeded`, with
both completion flags false and no replay. A subsequent capture was blocked.
After checked filler removal, read-only reconciliation recorded the failed
capture without repeating upload; its recovery artifacts remained preserved.
The operation ledger also records a successful full read-data check.

The original harness then stopped at a symlink metadata assertion because
Restic's JSON listing omitted the link target. That blocked result remains
unchanged. Preserved state and staging archives bound a separate read-only
continuation in a new verification container. Its no-lock full read-data check
and verified restore passed. All three files matched source sizes and SHA-256
digests, including the hidden synthetic credential file and 2,184,000-byte
data file. The restored symlink targeted `data.txt`; the pressure-only trigger
was absent. The continuation did not replay capture or reconciliation.

This qualifies only the small local scenario. It does not measure normal peak
storage, incompressible-data scale, host memory, persistent staging across
reboot, off-host recovery, or production recovery timing. Reserve checks remain
observed thresholds rather than a hard guarantee. Final remote-server
qualification for #26 was subsequently canceled; the issue is closed as not
planned and the experimental support limit is unchanged.

### Workstation implementation checks - 2026-10-20

The initial implementation suite passed **1,057 tests with one skip**, using
eight workers under the shared CPU budget. The subsequent nullable-receipt fix
passed **1,067 tests with one skip**; the native B2 metadata follow-up passed
**1,087 tests with one skip**. Focused schema-2 tests cover authorized-null and
held/retained versions, target and credential binding, generic S3 compatibility,
and changed or unknown native evidence before deletion. Focused regressions and independent source
review covered restricted-key assessment, separately signed central acceptance,
retention dependency loss and uncertain deletion, remote-primary resource
failure, enforced Linux exclusions and mount-namespace drift. Generated CLI
catalogs/schemas, version synchronization, publishing readiness, Markdown and
asset provenance checks passed. Changed Python paths passed Ruff and compilation.

The subsequent reviewed SQLite coverage and remote-export lifecycle fixes at
`f34945826bdb177dfa0858dd1dbe9470464980ed` passed **1,122 tests with one skip** in
99.31 seconds using eight workers. The skipped legacy Docker restore round trip
suppressed a failed fixture backup; its underlying cause was not identified, so
it is not restore evidence. Focused regressions cover raw sidecar refusal,
transactional restore and metadata reconstruction, retained exports after
interruption or receipt failure, streamed remote-byte verification, crash-window
recovery, explicit digest-bound disposal and legacy reconciliation compatibility.
Generated catalogs/schemas, version, publishing, compilation, Markdown, Ruff and
diff checks passed.

An offline wheel/source build passed portable source-archive checks and an
isolated installed-wheel smoke test. These are historical checks of a
development checkout still carrying package version 2.0.3; they are not the
v2.0.4 release-candidate validation or published release artifacts, and they do
not claim that the open live qualification work is complete. All tracked README
entry points were reviewed, with current-state links and support boundaries
aligned while preserving historical release records.
The wheel and portable source archive built from the same `f349458` commit also
passed source-content checks and an isolated installed-wheel smoke test. The
private wheel SHA-256 is
`ecef6617633a6547b460109dca7d9264aaa5d0a1607901331842856c9dc83a59`.

A fresh Ubuntu 24.04.5 KVM guest passed all ten existing user-timer checks across
three boots in 333.9 seconds. The tested development wheel SHA-256 was
`e9ad97c9d9575bc1ca1ca0a0bb1efaf43f6512fc75aa8377d69f00e19df94bbc`.
It exercised enrollment, capture/copy/byte-matched restore, missed-timer
catch-up, disabled timers, overlap refusal, interrupted-operation uncertainty
and no replay. Both repositories remained in that guest. This does not qualify
root covered jobs, live B2 or off-host recovery. The subsequent covered-timer
temporary-source-view correction passed all 29 focused scheduling/coverage
checks; ordinary timer rendering remained unchanged.

### Existing version-2 behavior

- Strict version-2 JSON configuration rejects duplicate keys, unknown fields,
  unsupported source types, and inconsistent repository references. Private host
  bindings and password files are separate from portable policy.
- Typed operation plans, bounded process output, timeouts, process-group
  cancellation, sanitized SQLite lifecycle records, and repository locks provide
  a shared execution boundary. Interrupted mutations remain uncertain; they are
  never automatically replayed. Receipts commit before operations finish.
- File trees go directly to restic. SQLite databases use the backup API and a
  private temporary export; uncommitted writes are excluded. PostgreSQL streams
  custom-format native exports; MySQL/MariaDB require an explicit read-lock policy.
  Native adapters have also passed bounded live concurrent-write recovery drills
  described below. Live file trees remain best-effort captures, not atomic
  filesystem snapshots.

### Native Docker capture and recovery

This implemented path is not production-qualified and is outside the current
stable support scope, which is unchanged from v2.0.3. Docker capture is opt-in
and requires a local Docker Unix socket in the private
host bindings. The portable source policy names one container and requires an
explicit stop-and-restart window:

```json
{"name":"application","kind":"docker","container":"web","connection":"local_docker","stop_policy":"stop-restart"}
```

```json
{"docker_bindings":{"local_docker":{"kind":"docker","socket":"/var/run/docker.sock"}}}
```

The capture records the pinned image ID, container configuration, environment,
labels, restart policy, daemon-provided `MaskedPaths` and `ReadonlyPaths`, and
persistent local bind/volume paths in private metadata inside the encrypted
restic snapshot. Mask and read-only paths are validated as absolute container
paths and preserved from inspection rather than hard-coded. Environment values
and labels may contain secrets. Only selected containers are stopped; already
stopped containers remain stopped. Capture is refused if a running, unselected
container shares any persistent source path. With a rootful daemon, the bbackup
process also needs access to selected source paths and every path in the running
container sharing inventory; an inaccessible unrelated mount source blocks
preflight before any container is stopped. Selected running containers are
restarted before the capture receipt or successful ledger outcome is committed.
If restart is uncertain, no successful receipt is created and a private durable
intention remains in service state.

The unqualified native Docker path refuses deployment features and container state it cannot restore
faithfully. It ignores only added writable-layer entries exactly at persistent
mount destinations, whose mountpoint directories are recreated by the restored
mounts; siblings, descendants, modified/deleted entries, and other writable-layer
changes remain unsupported. It also rejects remote Docker endpoints, non-local
volume drivers or options, custom networks, published ports, privileged access,
added capabilities/devices, non-default resource limits, and unknown host
settings. Explicit bind and local-volume entries in `Binds` or typed
`HostConfig.Mounts` are accepted only when they match Docker's inspected mounts
and use supported default options.
Restore requires the exact captured image ID to already exist locally; it
never pulls an image. Restored application data is staged under a new target
directory, and a new container name is required. Local-volume data is restored
as a bind mount under that directory, retaining the original container target
and read-only mode. Restic verifies restored files; the operation also waits
for the new container to run and, when the image defines a health check, become
healthy. These checks do not establish business-level application-data validity
or production qualification.

For example, restoring source `application` into a new container and directory:

```sh
bbackup production docker restore --repository local --snapshot FULL_SNAPSHOT_ID \
  --source application --connection local_docker --target /srv/restore/web \
  --container web-recovered --trusted-archive
```

The target directory must not already exist. For an uncertain create or start,
inspect the exact target first, then record the reviewed ledger outcome without
replaying the operation:

```sh
bbackup production docker reconcile --connection local_docker --container web-recovered \
  --operation-id OPERATION_ID --outcome succeeded --docker-reviewed
```

After reviewing the exact daemon and container, use
`bbackup production docker recover --connection local_docker --reviewed` to
resolve durable capture restart intentions. Recovery acts only on the exact
container ID and daemon identity recorded by capture. A pre-stop intention
does not start a container that is now stopped. `docker reconcile` only records
a reviewed ledger outcome; it never starts, stops, removes, or retries Docker
resources. Inspect and resolve any retained target or intention before another
capture or restore.
- Local capture records complete successful snapshot IDs. Copies use independent
  passwords and matching chunking parameters, then verify the destination identity.
  A failed or incomplete capture is not eligible for replication.
- Restore requires a new directory outside source paths, repositories, credentials,
  and runtime state, and asks restic to verify restored files. This does not prove
  application startup or database recovery.
- The v2 CLI emits a version-2 JSON envelope, accepts strict JSON or flags,
  rejects duplicate inputs, and exposes bounded pagination. The Textual dashboard
  supports mouse/keyboard job selection, search, table sorting, and systemd start
  and cancel requests. Its Restore tab accepts a repository, full snapshot ID,
  and new destination directory, and performs the same verified file restore
  as the CLI without blocking navigation. Selecting a snapshot fills its identity.
  Cancellation retains partial files; closing the dashboard cancels and waits
  for its interactive restore while detaching from installed systemd jobs.
- Generated systemd timers use daily scheduling and persistent missed-run handling.
  Rendering does not install or enable units.

![Textual dashboard with no configured jobs](../assets/bbackup-dashboard-preview.svg)

## Trying version 2 locally

Use a disposable local directory and private password files. Keep state and
repositories outside every source tree. The following paths are examples, not
installed defaults.

Portable configuration (`config.json`):

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

Private host bindings (`bindings.json`, mode 0600):

```json
{
  "schema_version": 2,
  "state_dir": "/var/lib/bbackup",
  "repositories": {
    "local": {"repository": "/srv/backup-local", "password_file": "/etc/bbackup/local.password"},
    "replica": {"repository": "/srv/backup-replica", "password_file": "/etc/bbackup/replica.password"}
  }
}
```

Create password files yourself with independent passwords and mode 0600. They
must be available without interactive prompts. State must have mode 0700.
An S3 binding can use restic's `s3:https://ENDPOINT/BUCKET/PREFIX` syntax;
provider credentials belong in the supervised process environment, never in JSON
policy or repository URLs.

```bash
bbackup production --config config.json --bindings bindings.json doctor
bbackup production --config config.json --bindings bindings.json repositories init --name local
bbackup production --config config.json --bindings bindings.json repositories init --name replica --source local
bbackup production --config config.json --bindings bindings.json jobs run --name daily
bbackup production --config config.json --bindings bindings.json repositories check --name replica
bbackup production --config config.json --bindings bindings.json snapshots list --limit 100
bbackup production --config config.json --bindings bindings.json tui
```

Use the exact destination snapshot ID from the copy result for restore:

```bash
bbackup production --config config.json --bindings bindings.json restore \
  --repository replica --snapshot FULL_DESTINATION_SNAPSHOT_ID --target /srv/new-restore
```

Every registered operation provides `--schema`. The generated
[command reference](../production-cli.md) and packaged
`bbackup/data/production-schemas.json` come from `bbackup/cli_metadata.py` through
`scripts/generate_cli_skills.py`. Exit 2 indicates invalid input, exit 3 an
operational failure. Production command syntax errors use the same sanitized envelope. `runs stream` explicitly emits bounded JSON-lines lifecycle events. Raw subprocess errors
and commands are not included in the public envelope.

## Setup and configuration editing

The dashboard opens first-run setup when both explicitly selected files are new.
Its two JSON editors accept the same portable policy and private host-binding
schemas shown above. Existing files are never overwritten by setup. A partial
pair is an error that needs inspection, not permission to replace the other file.
Setup validates matching repository and database bindings, creates private files,
and opens the configured dashboard. It does not initialize repositories, capture
files, or install or enable services.

```bash
bbackup production --config new-config.json --bindings new-bindings.json tui
bbackup production --config new-config.json --bindings new-bindings.json setup \
  --configuration-file reviewed-config.json --bindings-file reviewed-bindings.json
```

These are alternative setup methods; do not run both against the same new files.
Keep credential values in separate private files or the supervised environment.
Host-binding JSON contains references, not passwords or access tokens.

The Settings tab edits the complete portable JSON policy without exposing host
bindings. Reload before editing. Save validates the new policy against current
bindings and refuses a stale revision. The previous exact bytes are kept in a
dated, mode-0600 gzip file under the private state's `configuration-backups/`
directory before replacement. The command-line equivalent is:

```bash
bbackup production --config config.json --bindings bindings.json config show
bbackup production --config config.json --bindings bindings.json config save \
  --document-file reviewed-config.json --revision REVIEWED_SHA256_REVISION
```

Use the `revision` returned by `config show`, not a revision from another edit.
Both interfaces share validation and persistence. Saving changes neither
credentials nor running system services. Regenerate and review installed timer
units after schedule changes. The current dashboard reloads policy after a
successful save; already running jobs retain their original operation plans.
External editors do not participate in the application's advisory write lock;
do not edit the same file concurrently while saving through either interface.

### Shared administration and progress

Jobs, Repositories, Snapshots, and Recovery offer typed operation forms from
the same strict registry used by the command interface. Unknown fields are
rejected; destructive approvals and trust acknowledgements default to false.
Operations run away from the interface thread, support explicit cancellation,
and retain uncertain or partial state rather than retrying automatically.
The Activity tab refreshes sanitized lifecycle events without raw commands,
credentials, or native database output.

Overview and `doctor` read the same durable observations: last successful local
capture, independent/cloud copy, full repository check, verified file restore,
native database/container restore, and recorded outcome counts. Local capacity
comes from the repository filesystem or its nearest existing parent; remote
quota is explicitly unavailable. Next runs are reported only for observed
loaded and active systemd timers, not inferred from configured calendars.
Signed checkpoint metadata is tied to the current repository binding and shows
expired, unexpired, or unknown expiry. Neither an unexpired checkpoint nor a
successful file/native restore proves off-host custody, current provider
protection, or application recovery.

Before an existing receipt database changes schema, the application holds its
SQLite write reservation and takes a consistent backup of the committed old
database. A validated, dated mode-0600 gzip copy is flushed under the private
state directory's `state-backups/` before schema changes begin. Failure to
preserve that backup stops migration. The backup restores receipt metadata,
not repository contents or the whole machine. Stop all writers and retain
the matching application version before an operator restores an older schema.

The default command contract remains exactly one JSON result envelope. Opt in
to numeric newline-delimited progress only when the consumer supports a stream:

```bash
bbackup production --config config.json --bindings bindings.json --progress-json \
  jobs run --name daily
```

The stream ends with the usual result envelope. Progress includes only bounded
numeric counters and percentages; paths, filenames, credentials, and native
dump contents are excluded. Cancellation still requires inspecting uncertain
mutations before a later retry.


## Operational limits

Use one shared state directory for all bbackup writers for a repository. The
local ledger lock cannot coordinate an independently invoked restic process or
another host using a different ledger. Restic provides its own repository locks,
but independent recovery checkpoints additionally need a coordinated quiescent
window across all writers.

The local snapshot-retention workflow has no automatic deletion or pruning schedule. It can
forget explicitly reviewed snapshots using restic's daily selection policy,
grouped by host and filtered by the job tag. Seven local daily recovery points
is the default; this is not a count of the last seven captures. Cloud defaults
of thirty daily, eight weekly, and twelve monthly points remain policy, not a
qualified provider lifecycle or pruning implementation.

Experimental central cloud retention is a separate catalog/grant and reviewed
plan/apply workflow for noncurrent object versions. It never runs Restic
`forget`/`prune` or compacts current packs. See the
[central retention guide](../cloud-storage.md#central-noncurrent-version-retention-experimental).

```bash
bbackup production --config config.json --bindings bindings.json retention plan --job daily
bbackup production --config config.json --bindings bindings.json retention apply \
  --job daily --plan-token REVIEWED_PLAN_TOKEN --approved
```

Review `target_snapshot_ids` and every protected snapshot before approving the
exact returned token. A plan binds the repository inventory, immutable
capture-time replica requirements, successful copy receipts, policy, and exact
snapshot IDs. Apply recomputes the plan under the local repository lease and
rejects stale approval. Missing legacy evidence, foreign or unrecorded captures,
and incomplete required replication are held rather than guessed safe.
Confirmed deletion removes matching local receipts. Neither command prunes
packs: forgetting snapshots does not reclaim their shared physical data.
Failed or unconfirmed mutations require inspection before reconciliation.

Local capture stops on a minimum-space check or restic failure; it does not
reclaim pending recovery points. The fixed 64 MiB preflight threshold is not a
capacity forecast. Capture may still run out of space, in which case no
successful receipt is written.

Uncertain operations require administrator reconciliation against repository
state and the recorded capture operation tag. Do not clear the ledger or remove
restic locks to make a retry proceed. `runs reconcile` records an explicitly administrator-reviewed outcome without
replaying the command. Review the repository first; the command does not
synthesize missing snapshot receipts or remove repository locks. Failed restore output is retained for inspection; empty targets
from a refused dispatch are removed when possible.

## Native database capture and restore

PostgreSQL sources use `kind: "postgresql"`, a safe `database` name, and a
`connection` reference instead of `paths`. The corresponding private
`database_bindings` entry contains `kind`, an absolute `service_file`, a
`service_name`, and optionally an absolute `password_file`. The service and
password files must be private regular files. Native output must have the
PostgreSQL custom-archive signature before it becomes eligible for backup.

MySQL and MariaDB sources use `kind: "mysql"` or `"mariadb"`, `database`,
`connection`, and explicitly `quiesce: "read-lock"`. Their private binding uses
matching `kind` and an absolute `options_file`. The native MySQL/MariaDB path refuses non-InnoDB
tables and uses a native global read lock for the dump, blocking writes while it
runs. This explicit policy prevents concurrent schema changes from invalidating
the export. It requires the database account's corresponding lock privileges;
it is not a zero-interruption capture mode. MySQL login-path overrides are disabled.

Native database clients must be installed separately on the capture and restore
hosts. New database captures include a versioned manifest inside the snapshot.
PostgreSQL restore uses that snapshot-time manifest, so the original source
configuration and ledger are not required. Select a destination connection from
private host bindings and a new database name:

```bash
bbackup production --config config.json --bindings bindings.json databases restore \
  --repository replica --snapshot FULL_DESTINATION_SNAPSHOT_ID \
  --source DATABASE_SOURCE_NAME --connection RECOVERY_CONNECTION \
  --target-database recovered_app --trusted-archive
```

PostgreSQL restore requires PostgreSQL 15 or newer and libc locale databases. Capture
records the source encoding, collation, and character classification; restore
creates the destination from the pristine `template0` with those exact settings.
The matching locales must be installed on the destination server. ICU and other
locale providers are refused until their version-specific metadata is supported.

The archive must come from a trusted source: database restores execute archived
SQL. The command refuses existing databases, stops on restore errors, and checks
read-only connectivity afterward. It never drops a database, and an interrupted
restore retains the partial destination for inspection. Use `databases reconcile`
only after inspecting that destination. Coordinate all writers through one state
directory and one destination binding; differently named service aliases are not
proof of different servers. Connectivity success does not establish application
startup or application-specific data correctness.

MySQL and MariaDB restoration uses the same snapshot manifest, but requires the
**original database name on a separate, empty recovery server**. Only built-in
schemas may exist there; create neither the target database nor application
accounts before the restore. The administrative connection must support native
DEFINER views, routines, triggers, and events. Do not point this operation at an
existing production server or permit other writers during recovery:

```bash
bbackup production --config config.json --bindings bindings.json databases restore \
  --repository replica --snapshot FULL_DESTINATION_SNAPSHOT_ID \
  --source DATABASE_SOURCE_NAME --connection ISOLATED_RECOVERY_CONNECTION \
  --target-database ORIGINAL_DATABASE_NAME --trusted-archive \
  --isolated-server-confirmed
```

Only supported versioned native exports are accepted; arbitrary or legacy SQL
files are refused. Connection options are pinned privately across all commands,
and unsafe overrides, included configuration files, login paths, reconnects,
and local-file imports are disabled or refused. An ambiguous import retains its
partial state and blocks further MySQL-family restores in that state directory.
Inspect the entire recovery server before reconciliation; changing the binding
name or target is not permission to retry. Database restore neither drops a failed
database nor provisions or switches application traffic. A Unix-socket-only
server may report port zero; configured TCP connections still require a valid
nonzero port.

## Independent recovery artifacts

The cloud adapter streams exact current versions and delete markers into a
signed, GnuPG-encrypted checkpoint kit containing an independent restic password.
The signed inventory binds the storage endpoint, bucket, prefix, repository ID,
and complete snapshot ID. Recovery requires the exact trusted OpenPGP fingerprint;
revoked, expired, invalid, and unverifiable signatures are refused. Keep the
signing and decryption keys independently accessible and renew checkpoints before
key expiry. No key is generated or imported by the product.

`recovery checkpoint` first runs a full restic check and verifies repository and
snapshot identity under the local operation lease. An administrator must also
coordinate a quiescent window across every external writer. It rejects missing
versioning, current-object expiration, and noncurrent expiration under thirty
days. An observed lifecycle policy does not prove backup credential restrictions
or provider behavior, so the unqualified cloud-recovery path never returns a production protection claim.

`recovery open` authenticates a kit into a new private directory. It works without
the original host configuration or ledger. `recovery restore` authenticates the
checkpoint and downloads its exact historical object versions into an empty
local repository, leaving the damaged source untouched. Transient restic locks
are excluded, delete markers remain absent, and failures leave partial output
for inspection. Expired checkpoints permit best-effort recovery with expiry
reported. `recovery restore` also requires the independent password file, verifies the
rebuilt repository identity, runs a full-data restic check, and confirms the
requested snapshot exists. An application recovery drill remains necessary.

Review the generated [recovery command schemas](../production-cli.md) before
running these administrator operations. Keep kits outside the protected host;
creating a kit on that host does not establish independent custody.

## Qualification and remaining work

### Qualification record — 2026-10-17

**The selected stable v2.0.2 support boundary is** local/file capture and
restore; live PostgreSQL 17.11, MySQL 8.4.12, and MariaDB 11.8.9 capture/restore
drills; headless dashboard happy paths; and AMD64 Ubuntu 24.04/26.04 packages.

The prior `v2.0.1` source candidate passed all 842 pytest tests with eight
workers. `uv sync --locked`, version and publishing checks, Markdown standards,
generated CLI validation, Ruff, Python compilation, `uv build`, and isolated
smoke of the exact `2.0.1` wheel passed. Those checks did not include a
successful tag build.

The `v2.0.1` tag run `39044344832` passed source validation and both Ubuntu
24.04 native package jobs, but failed both Ubuntu 26.04 jobs in `uv export`.
The builder ran native Python 3.14 while `uv export` followed the repository
`.python-version` pin to Python 3.12; automatic Python downloads were disabled
and Python 3.12 was unavailable in those containers. Release assembly and
publication were skipped; no release assets were produced.

Candidate v2.0.2 passed all 843 pytest tests with eight workers, including the
native-interpreter export regression. `uv sync --locked`, version and
publishing readiness, Markdown standards (31 public files), generated CLI docs,
Ruff, Python compilation, `uv build`, and smoke of the exact `2.0.2` wheel
passed. A local uv 0.13.1 smoke exported locked requirements with Python
downloads disabled while `.python-version` requested unavailable Python 3.99,
using the builder's explicit interpreter. Publication requires hosted validation
with the release workflow's pinned uv 0.11.21 and successful builds of all four
Ubuntu 24.04/26.04 native packages.

The signed `v2.0.2` tag points to
`ce36c39a9bf1733d27f4f2e1f1901a9b753ba8f7`. Candidate CI run
`39047101675` passed all eight jobs. Tag-build run `39047303176` passed source
validation, all four native package build/install jobs, and release assembly.

Publication run `39047619253` stopped before creating a release because the key
server could not supply an importable public certificate. Publication-only
commit `98285816e56eb9970723c6341cd2e7d04213905f` imports the checked-in public
certificate while retaining fixed-fingerprint verification. Its exact
verification shell passed locally with a fresh keyring and the original build
inventory. The tag and build artifacts were not changed.

Publication run `39047848453` succeeded. The stable
[v2.0.2 release](https://github.com/CruxExperts/BestBackup/releases/tag/v2.0.2)
contains 16 assets: a wheel, source archive, four native packages with
manifests/checksums, combined `SHA256SUMS`, and its detached signature. A fresh
download matched all 14 build-asset hashes and the original checksum inventory;
the public detached signature verified against the required fingerprint.

Separate main-branch CI run `39047757396` on the publication-only commit failed
the Python 3.14 dashboard test `test_mouse_keyboard_search_and_small_screen`
with `NoMatches` for `#event-table`; Python 3.12/3.13 unit jobs and all lint
jobs passed. This does not replace the successful evidence for the immutable
release commit; the dashboard code and test were unchanged by the publisher fix.

Historical `v2.0.0` tag run `39037767831` passed source validation but failed all
four native package jobs: the isolated uv-tools environment's Python was first
on `PATH` and could not import apt-installed `packaging`. The v2.0.1 fix uses
system Python for the Debian builder and its uv environment. The existing
`v2.0.0` and `v2.0.1` tags remain unchanged.

The earlier full-feature assessment recorded the checkout at
`ddaede3230a86463d1ebe4efe2caedad30775c5d`, one commit ahead of remote `main`
at `8f2807887fd90771f57107a436c62eb485ea6947`. At that assessment the local
`VERSION` was `2.0.0-alpha.1`; the published `v2.0.0-alpha.1` release was a
prerelease, `v1.8.6` was the latest stable release, and the remote had no
non-alpha 2.x tag. This is historical release-state evidence and does not
change the selected-scope support boundary documented here.

The release assets include `SHA256SUMS` and detached `SHA256SUMS.asc`. GitHub's
release API reports the wheel digest as
`c24236b65c7f35076a39ae8e806e18ea6c1943c7cad46da10262189ca885e26e`; the
published checksum manifest lists the same digest. The existing local record
below reports successful checksum and OpenPGP signature verification.
This signature authenticates the checksum list, not build provenance. On
October 17, an authenticated `gh api` query to
`/repos/CruxExperts/best-backup/attestations/sha256:<digest>` returned HTTP
404. No artifact attestation could be retrieved or verified; this response
does not prove that none exists. The tag-triggered workflow builds checksummed
artifacts; publication requires the separate signed `workflow_dispatch`. Neither
step establishes production qualification or a build-provenance attestation.

The attestation lookup is recorded for transparency, not as a functional release
blocker.

The earlier assessment covered the full implemented feature scope, not the
user-selected tested-scope release. Its local suite, wheel, and installed smoke
were functional checks, not live B2/AWS or native Docker acceptance. The
previously assessed wider scope also included cloud protection, ARM64, ordinary
cross-application upgrade/rollback, and systemd lifecycle guarantees; those
remain excluded or deferred as specified below.

The stable-release bar for the selected scope includes the normal test suite and
installed-artifact smoke; standard command and dashboard paths; the listed live
database recovery drills; and fresh-install plus capture/check/restore paths on
AMD64 Ubuntu 24.04 and 26.04. Paths without that evidence remain outside
production-qualified support.

### Resumed implementation

The October 15, 2026 recovery review recovered the original production-release
plan and the October 3 implementation session. That session ended after the
alpha release; no interrupted version-2 command or crash handoff was found.
The alpha is a completed checkpoint, not completion of the production plan.
The user explicitly selected continuation of that unfinished production work.

The recovery baseline at commit `8f28078` passed all 753 tests with eight workers,
source compilation, focused lint, version and publishing checks, and generated
command/schema checks. A fresh wheel and source distribution built successfully;
the wheel passed the isolated installed-artifact smoke check. The preserved
alpha artifacts passed checksum and OpenPGP signature verification.
A disposable real-restic capture, independent-password replication, full-data
check, and restore preserved exact file bytes, mode 0640, and a symbolic link.
These local checks do not qualify a cloud provider or application recovery.

No B2/AWS production qualification is recorded. The small live B2 source trial
above does not qualify provider protection, and a local Garage S3-compatible
test does not qualify either provider. B2/AWS remain outside the stable support
scope. Do not replay uncertain backup mutations or change live provider policy
during a recovery.

The signed [2.0.0-alpha.1 prerelease](https://github.com/CruxExperts/BestBackup/releases/tag/v2.0.0-alpha.1) was published on October 3, 2026 from commit `b10ed84`. GitHub CI passed on Python 3.12, 3.13, and 3.14, and the release workflow built and smoke-tested its wheel and source archive. `SHA256SUMS` and its OpenPGP signature cover the exact downloadable artifacts. The release remains explicitly marked as a prerelease; it does not establish production qualification.

On October 3, 2026, the combined checkout passed 753 tests with eight workers,
including native-client fixtures and a real restic snapshot-manifest round trip
without the original source configuration or ledger. The built wheel passed an isolated installation smoke
test using hash-locked dependencies, including version-2 schema discovery.
Source compilation, focused lint, generated CLI/schema checks, version alignment,
Markdown standards, and diff whitespace checks also passed. These checks include
the preserved pre-existing working-tree changes; they are not release acceptance
for Ubuntu packages or cloud protection.

A quota-limited local Garage S3 test also passed capture, copy, full-data check,
and verified restore with independent passwords. Its private bucket and
password record are retained outside Git for follow-up. Garage is not qualified
for the thirty-day cloud protection claim.

Local tests cover independent repository initialization, capture, explicit copy,
full-data check, and verified file restore including modes and symbolic links.
SQLite tests cover capture while a writer holds uncommitted changes. Subprocess
tests exercise bounded output, cancellation, timeout and process-group cleanup;
ledger tests exercise contention, interruption, privacy and pagination. Headless
Textual tests exercise mouse tabs, keyboard selection, search and terminal resize.

### Completed local drills during recovery

Live PostgreSQL 17.11, MariaDB 11.8.9, and MySQL 8.4.12 drills captured while a
source writer held uncommitted changes, then restored with fresh configuration
and state rather than the original capture ledger. Recovered balances matched
the pre-commit transaction boundary; UTF-8 data, a 160 KB text digest, and native
collation metadata were checked. MySQL-family drills also checked DEFINER
views, routines, triggers, and events on empty isolated recovery servers.
These are specific application-data checks, not general application startup,
failover, or complete database-server recovery qualification.

A million-file run used 1,000,000 files of 1,024 bytes each, containing a unique
eight-byte index followed by zero padding. This is deliberately highly
compressible data, not a representative terabyte-scale workload. Four restic
workers and a 1,536 MiB soft memory setting were used:

| Stage | Elapsed seconds |
| --- | ---: |
| Generate fixture files | 30.343 |
| Encrypted local capture | 17.632 |
| Independent-password repository copy | 13.907 |
| Replica full-data check | 5.301 |
| Verified file restore | 54.063 |

The run checked the complete restored file count plus sampled bytes and modes.
Logical content was 1,024,000,000 bytes; the first repository occupied 189,783,358
bytes. Peak reported child resident memory was 1,155.17 MiB. Interface overhead
was excluded. These measurements are drill results, not promised restore times.

An offline escrow drill created a signed and encrypted kit with disposable
administrator keys, retained a separate filesystem copy of the encrypted
repository, and removed the original source, repository, and password. Opening
the kit verified the signature and manifest digest; reconstructed repository
identity, snapshot availability, full-data consistency, restored bytes, mode
0640, and a symbolic link were checked without original configuration or ledger.
The signing key was not the release key. This simulates custody on one machine:
it does **not** demonstrate actual off-host custody or provider historical
version recovery.

Real local retention checks held snapshots awaiting replication, refused a stale
approval after deleting the destination snapshot, and deleted only the reviewed
oldest local snapshot after replication was restored. Headless dashboard drills
created private setup files, saved and backed up settings, restored actual file
bytes, and completed a real repository check. Explicit command progress emitted
numeric records followed by its final result.

Native AMD64 package drills used Ubuntu 24.04 with Python 3.12.3 and Ubuntu
26.04 with Python 3.14.4 in isolated containers. Both targets built locked
packages and exercised the installed commands through encrypted capture,
full-data check, and verified restore after removing the source files.
Fresh-install checks passed. An older half-configured candidate was recovered by
ordinary package installation; that repair drill does not qualify ordinary
cross-application upgrades.

The live installed dashboard held the package lock while dpkg refused an
upgrade. That drill exposed a real abort-hook error: refusal before a transition
left no marker, but the old hook required one and left dpkg half-configured.
The corrected hook verifies the intact payload under a shared lock in that case.
Repeating the real refusal from the corrected installed package preserved
`install ok installed`. Older installed hooks require stopping clients and
repairing the package first.

Interrupted-unpack checks blocked both launchers. Deliberately damaging only
the disposable package payload made abort verification refuse to clear its
marker. Reinstalling the verified same-version package restored executable
integrity without changing configuration, password files, or SQLite state;
another restore preserved exact bytes and mode 0640. Private pre-upgrade state
archives were checked. Unit syntax passed and no service-enable links appeared.
These are same-version payload-repair checks, not cross-application-version
rollback or a boot/reboot qualification. The test containers were removed.

The recovered runtime suite passed 839 tests with eight workers. The final
package abort correction separately passed 15 focused lifecycle tests, and the
rebuilt wheel passed isolated installed-command checks. Release-workflow YAML,
twelve shell steps, and fixed-revision action references passed local structural
checks; no hosted workflow or publication was dispatched.

### Implemented paths outside the selected release scope

The local file, selected database, dashboard, and AMD64 package happy paths have
recorded passing evidence. The following paths are not production-qualified or
remain deferred; their implementation does not expand the selected support
boundary:

- **B2/AWS:** A small live B2 source capture/check/restore trial and denied
  forbidden-operation checks are recorded above. Provider protection and
  production recovery remain unqualified; AWS has no live result. These paths
  remain outside the selected stable support boundary. Garage S3 testing is not
  provider qualification.
- **Native Docker:** Implemented, but no qualifying capture/restore happy path is
  recorded. It is not production-qualified and remains outside the selected
  stable support boundary.
- **Packages:** Fresh-install and capture/check/restore drills passed on AMD64
  Ubuntu 24.04 and 26.04. ARM64 package outputs are unqualified. Ordinary
  cross-application upgrades and rollback are deferred; systemd boot/reboot is
  also deferred.
- **Database and dashboard:** Live PostgreSQL 17.11, MariaDB 11.8.9, and MySQL
  8.4.12 data-recovery checks passed. Headless setup, settings save/backup, file
  restore, repository check, and progress paths passed. Full application
  startup/failover and exhaustive UI-state coverage are not release gates.

### Deferred assurance, not release blockers

Thirty-day cloud-lifecycle boundaries, aged or overwritten object cases,
protected pruning, provider historical-version recovery, off-host custody,
representative 1–10 TB runs, broad failure-injection, ordinary cross-application
rollback, systemd boot/reboot qualification, and an exhaustive security audit
are deferred. They remain unverified; do not make the corresponding guarantees.
Fix any discovered data-loss or security defects before release.

The offline recovery-kit drill and local retention tests passed, but do not prove
off-host custody or cloud-protected retention. No legacy implementation has
been removed before its replacement is ready. Restore deadlines must come from
measured drills.

## Version 2.0.3 candidate: repository and recovery hardening

This development work follows the published v2.0.2 artifacts and does not replace
their release evidence. Portable schema 3 policy uses logical source names;
private host bindings contain selectors and credential-file references. Explicit
migration preserves the original files. Enrollment binds the exact reviewed pair
to the machine and user, and concurrent edits cannot refresh that approval.
Operations hold the shared policy lease throughout execution; settings saves
refuse busy or stale inputs. Partial replica failures return nonzero while
retaining local-capture and per-destination results. Native capture and restic
stages receive the configured time limit.

The guided setup and labeled dashboard forms use the shared command dispatcher,
contextual field help, inline validation, and retained input after errors. Settings
loads the current document before editing job schedule/time limits. Interactive
job controls run or cancel that exact guarded operation; installed systemd timers
continue independently. User and system timer observations are distinguished,
and duplicate active timers are reported as a conflict.

Legacy encrypted tree format 3 encrypts the authenticated inventory and uses
opaque payload identifiers. Source metadata that cannot be preserved is refused
before finalization. Decryption failures do not publish a successful output;
Docker restore refuses occupied targets and proves ownership of a newly created
volume before writing or cleanup. Archive filters retain normalized safe link
targets and numeric ownership. SFTP requires an already trusted host key.

The repository now contains generated banner/social assets with embedded rights
metadata and checked provenance, plus a real headless dashboard preview using
disposable data and illustrative capacity. Shared CLI discovery is regenerated
from the owning registry. Release publication verifies signed tags and successful
trusted CI for the exact default-branch commit, uploads a draft, checks all remote
asset digests, and only then publishes it.

Live GitHub protections enabled during this work include secret scanning and push
protection, weekly Python/Actions code scanning, approval for all outside-fork
workflows, signed commits, pull requests with zero required human approvals,
resolved discussions, strict current CI checks, and protected release tags.
The update-branch button is enabled. Non-provider secret-pattern enablement was
requested through the documented API but remained disabled in readback; it is
not claimed as enabled. Action SHA enforcement, the narrower action allowlist,
the new aggregate required check, and immutable releases are staged for after
the matching workflow is published, avoiding a broken existing release path.
Source changes and new artwork are not yet published by these settings changes.

The final broad suite passed **938 tests with one skip**. The isolated installed
wheel smoke check passed. The test image built from an 8.3 MB allowlisted context
and collected the tests successfully; its existing uv bootstrap now accepts both
registry-verified amd64 and arm64 wheel hashes. Only the amd64 image was built
locally. Focused checks additionally cover UI/CLI/status/management, encrypted
inventory and archive tampering, source ownership, and enrollment/save races.
Live restic-dependent dashboard and database drills are marked for the CI
integration job so an unavailable engine cannot silently omit their coverage.
These results do not qualify cloud providers, ARM64, full-machine recovery, or
application startup. Rebootable guest evidence for the development candidate
is recorded below; it does not retroactively qualify the published release.

### Development candidate: rebootable systemd qualification

On October 17, 2026, the isolated AMD64 Ubuntu 24.04.5 LTS guest
(systemd 255.4-1ubuntu8.17, native Python 3.12, restic 0.19.1) passed the
complete rebootable drill. The Canonical image's signed checksum was verified;
the image SHA-256 was
`6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2`.

The same complete drill also passed on AMD64 Ubuntu 26.04.1 LTS with native
Python 3.14 and systemd 259.5-0ubuntu3.4. Its signature-verified Canonical image
SHA-256 was
`8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2`.
Each final run exited successfully with all ten checks true, in 328.4 seconds
on Ubuntu 24.04 and 329.4 seconds on Ubuntu 26.04.

The drill installed schema-3 policy and private bindings, enrolled the job,
verified generated units with `systemd-analyze verify`, explicitly activated a
user timer, and waited for a successfully completed scheduled backup. It verified
an additional repository copy and byte-matched a restored file. Disabled timers
stayed disabled. A stopped timer missed its deadline; after a real guest reboot,
`Persistent=true` caught up successfully. An overlapping operation was refused as
busy. A second, abrupt guest reset interrupted an in-flight mutation; the next
boot recorded uncertainty and refused retry without dispatching restic again.
Three distinct guest boot identifiers per guest were checked and retained privately.

The installed qualification wheel SHA-256 was
`286fbd25bb346a403e448b0ad2c6b6008c9cd78bb77519b927e3e3c389b106f7`.
The reviewed 2.0.2 development delivery wheel SHA-256 was
`72a9a7a5757a749a00ee5f09eb4f2183c2e1610cc5f9e472b01be377b74ce31a`.
A ZIP-member comparison found only the reviewed `dashboard_setup.py` enrollment
screen fix and wheel `RECORD` changed. All engine, enrollment, and scheduling
bytes match, so the lifecycle evidence is reused for those unchanged components;
the final wheel separately passed isolated installation smoke checks.

Both repositories lived in the same disposable guest with separate passwords.
This proves the tested scheduled file-backup and interruption behavior, not
independent host-loss recovery, off-host password custody, cloud-provider recovery,
application startup, native ARM64 behavior, or package upgrade choreography. The
harness never invoked host systemd. See the [qualification script instructions](../../scripts/README.md#rebootable-systemd-qualification)
for reproduction; private logs, guest disks, and machine identifiers are excluded
from source archives and Git.

### Version 2.0.3 release provenance

Version 2.0.3 was published from the reviewed guided-setup and recovery work above;
the publication record below identifies the exact source and verified artifacts.
Publication required the protected pull request to merge, successful trusted CI
on the exact resulting default-branch commit, and an annotated tag signed by the
fixed project key. An unverified GitHub signing identity blocks the merge; local
cryptographic verification alone does not override that requirement.

The tag build records `source-verification.json` and produces
`release-provenance.json`, binding the source commit, tag, trusted CI run, build
run, portable source, wheel, native package manifests, lockfile and active artwork
provenance. Both JSON files and every package are included in `SHA256SUMS`.
The operator's detached signature authenticates that inventory; the publisher
checks the complete draft's remote file sizes and SHA-256 values before making
the release public. A prepared candidate, a pushed tag, or a successful build
alone is not a published release.

The local v2.0.3 candidate wheel SHA-256 is
`364a758123b1848a9ad63b74c2cab05ddaa13827898376ec8f51510b5c1dc177`.
Compared with the reviewed 2.0.2 development delivery wheel above, runtime
changes cover version text, legacy archive/filesystem/Docker handling, and the
user-unit installation path. Service execution, enrollment, and guarded operation
modules remain byte-identical; timer-rendering function syntax trees also match.
Post-review regressions cover the changed legacy and installer behavior. The
complete current wheel has not repeated the earlier real-guest drill.
Release-builder output digests are recorded separately in the published manifest;
local wheel bytes are not assumed to equal a later hosted build.

The initial local v2.0.3 candidate suite passed **947 tests with one skip** in
41.52 seconds. The wheel passed isolated installed-command smoke checks and the
source archive passed its portable-input/private-state guard. All nine provenance
regressions passed, including the actual workflow matrix, stale advisory-lock
binding, unsafe archive members, and changed release-note digests. Ruff, syntax,
generated documentation, Markdown, asset metadata, and version checks passed.
Hosted candidate CI and the exact default-branch/tag builds remain separate
release prerequisites; these local results do not imply publication.

Post-review fixes retain an owned mode-0700 wrapper for every legacy archive extraction,
including archive root aliases, and preserve payload root metadata. Native rsync
selection applies the same exclude file to an empty-target dry run and decodes
escaped filenames exactly once. Custom absolute XDG configuration paths are
honored, with trusted-directory and traversal checks. Non-root Docker volume
backups use ownership-checked tar; a successful `docker cp -a` is not treated as
numeric-ownership proof. The actual helper-image integration fixture was fixed
so its rsync path selection is effective. Mocked unit-test Docker daemons receive
separate real-lease namespaces to avoid cross-worker false contention.

After those fixes, the complete candidate suite passed **963 tests with one
skip** in 33.76 seconds. Focused Docker plus live integration passed **51 tests
with one skip**. The native rsync-selection regression is marked for integration,
and CI requires rsync to be present before collecting that job. Material source
review accepted the extraction, ownership, selection, and XDG changes. The final
wheel and updated source archive are checked separately before publication.

The dependency refresh on October 18, 2026 queried all 51 locked PyPI package
versions plus setuptools 84.0.0 and wheel 0.48.0. OSV, GitHub advisories, deps.dev,
and PyPI returned successful responses with no exact-version advisory hits at
03:51 UTC. Build requirements now pin those reviewed tool versions. The portable
[dependency evidence](release-dependencies.json) records package coordinates,
source timestamps and limits, and the final lockfile digest
`7cf0bfc2b287378e4fe893a2f2ffc20379d165ce55fe6a518bc749f5a701c9fb`.
The release provenance verifies that evidence against the source lockfile and
includes it in the signed inventory. This is not a complete OS, container, or
host security audit.

The October 19 release closeout repeated the complete candidate suite: **963
tests passed with one skip** in 34.28 seconds. The public issue and pull-request
queue contained no ordinary open issues and one release pull request, #19. Its
four review findings are covered by the extraction, rsync-selection, Docker
ownership, and XDG installation fixes and their regression tests described above.
The landing page and documentation index now route Linux administrators,
homelab operators, and small infrastructure teams to the appropriate tested
workflows without extending the qualification scope.

The October 19 dependency refresh repeated all 53 exact-version queries across
OSV, GitHub Advisory Database, deps.dev, and PyPI. All responses succeeded and
returned no advisory records for those versions at 09:40 UTC. The linked portable
dependency report contains the refreshed timestamps and evidence digest; the
lockfile and selected dependency versions are unchanged.

The earlier real-guest results apply only to the unchanged service/enrollment/
guarded-operation core and timer rendering verified by that comparison. Version metadata,
generated command documentation, and release provenance tooling receive their
own candidate validation. The release does not turn same-guest repository copies
into off-host recovery or broaden the recorded ARM64/cloud qualification.

### Version 2.0.3 publication record - 2026-10-19

[Version 2.0.3](https://github.com/CruxExperts/BestBackup/releases/tag/v2.0.3)
was published at 09:57:37 UTC from signed annotated tag `v2.0.3`, resolving to
`61750fd22158b00596262f108f8e9ba8dff4cca7`. The protected release pull request
[#19](https://github.com/CruxExperts/BestBackup/pull/19) merged after its checks
passed and all four source-verified review threads were resolved.

Trusted [default-branch CI](https://github.com/CruxExperts/BestBackup/actions/runs/39232761634)
passed on that exact commit. The [tag build](https://github.com/CruxExperts/BestBackup/actions/runs/39233324423)
passed source validation and all four Ubuntu 24.04/26.04 AMD64/ARM64 package
build and installation jobs. These jobs establish package availability and
installation evidence, without expanding the functional qualification above.

The release contains 18 assets: wheel, portable source archive, four native
packages with their manifests and checksum sidecars, source verification,
release provenance, `SHA256SUMS`, and its fixed-key OpenPGP signature. The
checksum inventory SHA-256 is
`4dad79ca1959581a5ffde776b52debdce9ea133f482f6e5ab5bf4cf5c8a068fd`.
All public downloads matched the verified build files and GitHub's asset
digests; the downloaded inventory signature and every listed checksum passed.
An unauthenticated public API read confirmed the published, stable release.
GitHub reports the release as immutable.

The [publication run](https://github.com/CruxExperts/BestBackup/actions/runs/39233730520)
stopped after uploading a complete draft because lookup by tag returned 404
for that draft. Read-only reconciliation located release ID `417137103`,
verified its exact tag, notes, and all 18 asset names, sizes, and digests, then
published that existing draft by ID. No draft, tag, or asset was recreated.
The subsequent workflow correction locates drafts through the paginated
release inventory, refuses duplicate draft or published tags before creation,
and verifies and publishes the exact selected release ID.

The README, documentation index, repository description, and topics now address
Linux administrators, homelab and self-hosted operators, and small infrastructure
teams. They retain the recorded cloud, Docker, ARM64, upgrade, and off-host
recovery limits rather than treating discoverability as additional qualification.

## Dependency evidence

The entries below record earlier candidates and released 2.0.x inputs. They
remain historical evidence. For the current unreleased dependency graph, use
the [source maintenance record](#unreleased-layout-and-dependency-maintenance)
and [exact lockfile review](release-dependencies.json).

The optional Google Drive client uses oauthlib 4.0.0 to incorporate upstream
fixes for CVE-2026-49264 and CVE-2026-49265. Exact-version OSV, GitHub advisory,
deps.dev, and PyPI checks found no affecting advisories; neither CVE appeared in
the CISA exploitation catalog at review time. Client compatibility was verified
offline without authorizing an account. These findings concern optional legacy
Google Drive support and do not change the B2/S3 storage workflow.

Native B2 inspection uses Backblaze b2sdk 2.13.0, the latest stable SDK checked
for the Python 3.12+ baseline on October 3, 2026. Its new dependencies are
annotated-types 0.8.0 and logfury 1.0.1. Exact resolved SDK and HTTP dependency
versions were checked against OSV, GitHub advisories, deps.dev, and PyPI; no
affecting advisories were returned. SDK retry behavior is bounded by the native
inspection subprocess deadline and cancellation. Throttling and transient-error
backoff tests passed. See [cloud storage](../cloud-storage.md) for the connection
and recovery boundaries.


Textual 8.2.8 and its newly resolved dependencies, and boto3 1.43.108 with its
resolved dependency closure, were checked against OSV, GitHub advisories and
PyPI metadata on October 3, 2026. Those exact selected versions had no matching
advisories in the queries. This is not a permanent safety guarantee or a complete
release supply-chain audit. urllib3 was moved from 2.7.0 to 2.8.0 to address the
[maintainer's disclosed fixes](https://github.com/urllib3/urllib3/releases/tag/2.8.0).
The lockfile owns exact versions and package hashes.

The October 15 engine check verified the official
[restic 0.19.1 release](https://github.com/restic/restic/releases/tag/v0.19.1)
checksum signature against the published release-signing fingerprint
`CF8F18F2844575973F79D4E191A6868BD3F7A907`. The decompressed Linux AMD64
executable matched the engine already installed for the local drills. Exact
Go-module queries to OSV, GitHub advisories, and deps.dev returned no matching
entries; malformed or failed queries were not counted as negative evidence.
Preserve restic's BSD-2-Clause notices when bundling it. These checks are not
complete transitive or operating-system vulnerability coverage.

Database drills used pinned PostgreSQL, MariaDB, and Oracle MySQL images.
MariaDB 11.8.9 includes the fix identified by
[GHSA-65jj-h265-3m7h](https://github.com/advisories/GHSA-65jj-h265-3m7h).
The MySQL image was the vendor's
[8.4.12 Docker-image security update](https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-12.html),
not an older similarly named image from another registry. Exact image
provenance, executable versions, and bounded advisory/catalog lookups were
recorded privately. They do not constitute base-image vulnerability closure.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
