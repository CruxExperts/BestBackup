# bbackup

**Back up with confidence. Restore with clarity.**

bbackup is an encrypted, restic-based backup tool for Linux system administrators, homelab and self-hosted operators, and small infrastructure teams. It brings local file backups, selected database workflows, verified file restores, and observable scheduled jobs into one operator workflow.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/) [![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE) [![Version 2.0.7](https://img.shields.io/badge/version-2.0.7-22c55e?style=flat-square)](https://github.com/CruxExperts/BestBackup/releases/latest)

![bbackup: encrypted snapshot layers, an additional copy, and a restored document](docs/assets/bbackup-banner-v2.png)

[Get started](QUICKSTART.md) · [Install](INSTALL.md) · [Qualification evidence](docs/development/version-2.md) · [Automation and agents](docs/AGENT_INTEGRATION.md) · [All documentation](docs/README.md)

## A practical backup and restore workflow

Use bbackup when you want to know what a job captured, whether each repository copy succeeded, and whether files can be restored into a clean destination.

- **Set up once.** The terminal dashboard guides source, encrypted storage, schedule, and enrollment. Portable policy records job intent; private host bindings hold paths and credential-file references.
- **Capture and copy.** Create encrypted restic snapshots of file trees and optionally copy a successful snapshot to a separately configured repository. Results show the outcome for each destination.
- **Restore and verify.** Restore files into a new directory and ask restic to verify them. This checks file data, not application startup or full-machine recovery.
- **Schedule and observe.** Run named jobs from the dashboard, the JSON CLI, or generated user-level systemd timers. Progress, results, repository locks, and interrupted-operation status are visible to operators and automation.
- **Recover deliberately.** An interrupted mutation can have an uncertain result. Inspect the destination and reconcile that operation explicitly; bbackup does not automatically replay it.

![Real bbackup dashboard with disposable example data and illustrative capacity](docs/assets/bbackup-dashboard-preview.svg)

## Start with the guided workflow

For packaged releases, start with the [latest release downloads](https://github.com/CruxExperts/BestBackup/releases/latest) and [installation guide](INSTALL.md).

Check the [release readiness and tested scope](#v207-readiness) before choosing a workflow. Cloud
protection and complete-machine recovery remain unqualified.

From a development checkout with Python 3.12+, uv, and restic installed:

```bash
git clone https://github.com/CruxExperts/BestBackup.git
cd BestBackup
uv sync --locked
uv run bbackup production tui
```

Choose a source folder, encrypted storage, and a schedule. Review and enroll once. The dashboard then guides you through initializing storage, running the first backup, and restoring into a new folder. Advanced settings and the command line remain available. The [quick start](QUICKSTART.md) covers migration, password custody, replica setup, and deliberate schedule activation.

<a id="v205-tested-scope"></a>
<a id="v207-readiness"></a>

## v2.0.7 release scope and tested paths

Version 2.0.7 includes documentation and publishing-guidance updates plus
targeted build-tool security maintenance. It makes no production runtime
changes and adds no qualification; the tested support scope remains unchanged
from v2.0.6.

The qualified scope is unchanged from v2.0.6: local file capture and restore;
live capture and restore for PostgreSQL 17.11, MySQL 8.4.12, and MariaDB 11.8.9;
headless dashboard happy paths; and AMD64 Ubuntu 24.04/26.04 packages. User-timer
reboot qualification covers only the components and conditions in the
[recorded guest drill](docs/development/version-2.md#development-candidate-rebootable-systemd-qualification).

B2/AWS protection, native Docker, ARM64 backup/restore, cross-application-version
upgrades and rollback, root-owned systemd package templates, and off-host recovery
remain unqualified. Package-build or install checks alone do not qualify those
paths. Keep an independently retrievable copy of repository passwords; a second
repository on the same host does not protect against host loss.

Implemented experimental cloud-provider, central recovery and retention,
remote-primary capture, and reviewed Linux workflows have
[documented limits](docs/development/version-2.md#experimental-cloud-and-machine-recovery-features).
Issues #23–#26 were canceled and closed as not planned. Historical B2
[maintenance interruption](docs/development/version-2.md#live-b2-central-maintenance-interruption-trial)
and [lifecycle guard](docs/cloud-storage.md#b2-lifecycle-overlap-guard-experimental)
trials do not prove prune completion, elapsed expiry, physical reclamation or
provider protection. The [qualification record](docs/development/version-2.md)
owns the complete evidence and support limits.

### Existing maintenance in v2.0.6

v2.0.6 includes [issue #30 maintenance](docs/development/version-2.md#unreleased-maintenance-issue-30):
reviewed stale-lock handling, optional local last/weekly retention, strict
keep-all parsing, B2 lifecycle-read compatibility and nested cache handling.
Follow the
[maintenance quick start](QUICKSTART.md#review-local-retention-and-stale-locks-unreleased)
for the new commands.

The [v2.0.6 layout and dependency batch](docs/development/version-2.md#unreleased-layout-and-dependency-maintenance)
also repairs compact-terminal layouts and refreshes reviewed Python and CI
dependencies. Release preparation uses the official Release Please CLI with
a reviewed lockfile and patched Node runtime; publication still requires the
signed candidate, tag, checksums and artifact verification described in
[versioning](docs/VERSIONING.md).

## Choose a guide

- [Install](INSTALL.md): prerequisites, release packages, and development setup.
- [Quick start](QUICKSTART.md): first backup, schema-2 migration, replica, verified restore, and user-level scheduling.
- [Automation and agent integration](docs/AGENT_INTEGRATION.md): schemas, progress, results, and uncertain-operation reconciliation.
- [Production command reference](docs/production-cli.md): generated command and input-schema reference.
- [Cloud storage](docs/cloud-storage.md): implemented provider paths and their qualification limits.
- [Encryption](docs/encryption.md): password custody and legacy encryption compatibility.
- [Architecture](docs/architecture.md): shared operations, durable state, and safety boundaries.
- [Legacy management](docs/management.md): retained 1.x Docker/YAML commands and compatibility guidance.
- [Support](SUPPORT.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## License

Code is available under the [MIT License](LICENSE). Original documentation artwork carries separate [asset terms and provenance](docs/assets/README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
