# bbackup

**Back up with confidence. Restore with clarity.**

Encrypted Linux backups with a visual dashboard, dependable local snapshots, and an automation interface that tells you what actually happened.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/) [![License: MIT](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE) [![Version 2.0.2](https://img.shields.io/badge/version-2.0.2-22c55e?style=flat-square)](https://github.com/CruxExperts/BestBackup/releases/latest)

![bbackup: encrypted snapshot layers, an additional copy, and a restored document](docs/assets/bbackup-banner-v2.png)

[Get started](QUICKSTART.md) · [Install](INSTALL.md) · [What is supported](docs/development/version-2.md) · [Automation and agents](docs/AGENT_INTEGRATION.md)

## Your data deserves a clear recovery path

A backup should leave you knowing **what was captured, where the copies are, and how to get your files back**. bbackup brings those decisions together around encrypted restic repositories.

- **Start with a useful local backup.** Capture file trees into encrypted snapshots and restore into a new folder with file verification.
- **Keep another copy.** Replicate the successful snapshot to a separately configured repository with its own password. Per-destination outcomes make partial failures visible.
- **See the next step.** The terminal dashboard supports mouse and keyboard. Guided setup, labeled forms, contextual help, and inline checks reduce configuration mistakes.
- **Automate with confidence in the result.** Named jobs, persistent systemd timers, repository locks, and structured JSON make scheduled and agent-driven work observable.
- **Keep private details private.** Portable policy describes intent; enrolled host bindings hold machine paths and credential-file references.
- **Recover deliberately.** New-target restores and explicit reconciliation protect existing data when an operation is interrupted.

This README describes the development checkout, including unreleased setup, policy, and safety improvements. **The latest published release is v2.0.2.** Use its [versioned quick start](https://github.com/CruxExperts/BestBackup/blob/v2.0.2/QUICKSTART.md) with that release.

![Real bbackup dashboard with disposable example data and illustrative capacity](docs/assets/bbackup-dashboard-preview.svg)

## Try the guided workflow

From a development checkout with Python 3.12+, uv, and restic installed:

```bash
git clone https://github.com/CruxExperts/BestBackup.git
cd BestBackup
uv sync --locked
uv run bbackup production tui
```

Choose a source folder, encrypted storage, and a schedule. Review and enroll once. The dashboard then guides you through initializing storage, running the first backup, and verifying a restore. JSON remains available under Advanced.

Already have version-two policy files? The dashboard offers an explicit migration that preserves the originals. See the [quick start](QUICKSTART.md) for private password custody, migration, CLI commands, and schedule activation.

## Prefer a published release?

```bash
uv tool install --force 'git+https://github.com/CruxExperts/BestBackup.git@v2.0.2'
```

Stable v2.0.2 is qualified for local file capture/restore, selected PostgreSQL/MySQL/MariaDB capture/restore, headless dashboard happy paths, and AMD64 Ubuntu 24.04/26.04 packages. The [release evidence](docs/development/version-2.md) records exact versions and limits.

> [!IMPORTANT]
> B2/AWS, native Docker, ARM64 operation, and complete host-loss recovery remain unqualified. A second directory on one host is not independent disaster recovery. Keep an independent copy of repository passwords and verify restores before relying on a backup.

## Explore the features

- [Installation](INSTALL.md): prerequisites, release packages, and development setup.
- [Quick start](QUICKSTART.md): guided first backup, policy migration, replica, and verified restore.
- [Automation and agent integration](docs/AGENT_INTEGRATION.md): strict schemas, progress, results, and reconciliation.
- [Cloud storage](docs/cloud-storage.md): implemented provider paths and their qualification limits.
- [Encryption](docs/encryption.md): password custody and legacy encryption compatibility.
- [Architecture](docs/architecture.md): shared operations, durable state, and safety boundaries.
- [Legacy management](docs/management.md): retained Docker/YAML commands and compatibility guidance.
- [All documentation](docs/README.md) · [Support](SUPPORT.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## License

Code is available under the [MIT License](LICENSE). Original documentation artwork carries separate [asset terms and provenance](docs/assets/README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
