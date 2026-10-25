# Documentation

bbackup is for Linux administrators, homelab and self-hosted operators, and small infrastructure teams who need encrypted local backups, verified file restores, and visible scheduled jobs. Choose a guide by the work you are doing.

These guides describe the v2.0.7 documentation and release-readiness updates,
including targeted build-tool security maintenance. Production runtime
behavior and qualification remain unchanged from v2.0.6. Check the
[GitHub Releases page](https://github.com/CruxExperts/BestBackup/releases/latest)
for tag and download availability, and consult the
[qualification record](development/version-2.md) before selecting a workflow.

## Choose a starting point

- **Install and run a first backup:** [installation](../INSTALL.md) and the [quick start](../QUICKSTART.md).
- **Migrate a schema-2 setup:** [migration steps](../QUICKSTART.md#migrate-an-existing-schema-2-configuration).
- **Schedule jobs:** [apply a schedule deliberately](../QUICKSTART.md#apply-a-schedule-deliberately).
- **Automate commands:** [agent integration](AGENT_INTEGRATION.md) and the [production command reference](production-cli.md).
- **Maintain older Docker/YAML workflows:** [legacy management](management.md) and [encryption compatibility](encryption.md).
- **Evaluate cloud recovery or Linux coverage:** [experimental limits](development/version-2.md#experimental-cloud-and-machine-recovery-features) and [cloud storage](cloud-storage.md).

## Operator references

- [Architecture](architecture.md): shared operations, durable state and safety boundaries.
- [Cloud storage](cloud-storage.md): B2/AWS paths and their qualification limits.
- [Encryption](encryption.md): Restic password custody and legacy file encryption.
- [CLI skills](cli-skills.md): generated command and agent-skills catalog.

## Version and support boundaries

Version 2.0.7 includes documentation and release-readiness updates plus
targeted build-tool security maintenance. It does not change production
runtime behavior or qualification. The production-qualified scope remains the
v2.0.6 tested core scope; no B2/AWS, ARM64, or Linux recovery qualification is added.
Experimental cloud and Linux workflows remain unqualified; issues #23–#26 were
canceled and closed as not planned. An implemented path or a closed issue does
not establish support. The [qualification record](development/version-2.md)
retains the dated evidence and limits.

The v2.0.6 release includes [issue #30 maintenance](development/version-2.md#unreleased-maintenance-issue-30)
for reviewed stale locks, local retention counts, strict Restic parsing, B2
lifecycle-read compatibility and nested cache policy. Follow the
[maintenance quick start](../QUICKSTART.md#review-local-retention-and-stale-locks-unreleased)
for setup and reviewed use.

The [v2.0.6 layout and dependency batch](development/version-2.md#unreleased-layout-and-dependency-maintenance)
covers compact terminal forms, documentation navigation, and reviewed Python
and GitHub Actions maintenance.

Operators still using v2.0.2 can follow its
[versioned documentation](https://github.com/CruxExperts/BestBackup/tree/v2.0.2/docs)
until they migrate.

## Maintainer references

- [Versioning](VERSIONING.md): Release Please drafts, canonical package version, signed release tags and validation.
- [Publishing checklist](PUBLISHING_CHECKLIST.md): community files and release readiness.
- [Qualification record](development/version-2.md): validation results and deferred assurance.
- [Release dependency evidence](development/release-dependencies.json): reviewed versions, source timestamps, lock hash and audit limits.
- [Documentation assets](assets/README.md): asset inventory and visual standards.

## Documentation standards

- [GitHub Markdown writing standard](standards/github-markdown/github-markdown-writing-standard.md)
- [GitHub Markdown capabilities reference](standards/github-markdown/github-markdown-capabilities-reference.md)
- [GitHub Markdown review checklist](standards/github-markdown/github-markdown-review-checklist.md)
- [GitHub Markdown source provenance](standards/github-markdown/github-markdown-source-provenance.md)
- [GitHub Markdown provenance manifest](standards/github-markdown/github-markdown-provenance-manifest.json)

Back to the [project README](../README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
