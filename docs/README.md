# Documentation

bbackup is for Linux administrators, homelab and self-hosted operators, and small infrastructure teams who need encrypted local backups, verified file restores, and visible scheduled jobs. Choose a guide by the work you are doing.

These guides describe v2.0.5. The [qualification record](development/version-2.md) is the authority for the exact qualified scope, version-specific evidence, and remaining limits; an implemented path alone does not establish support. The v2.0.5 release retains the v2.0.4 tested core scope, while new cloud and Linux workflows are experimental and unqualified. Operators still using v2.0.2 can follow its [versioned documentation](https://github.com/CruxExperts/BestBackup/tree/v2.0.2/docs) until they migrate.

The [release dependency evidence](development/release-dependencies.json) records the reviewed versions, source timestamps, lock hash, and audit limits.

## Choose a starting point

| If you need to... | Start here |
|:--|:--|
| Install bbackup and run your first backup | [Installation](../INSTALL.md) and the [quick start](../QUICKSTART.md) |
| Migrate an existing schema-2 setup | [Quick start: migrate an existing configuration](../QUICKSTART.md#migrate-an-existing-schema-2-configuration) |
| Maintain an older Docker/YAML workflow | [Legacy management](management.md) and [encryption compatibility](encryption.md) |
| Schedule jobs or integrate with scripts and agents | [Quick start: apply a schedule](../QUICKSTART.md#apply-a-schedule-deliberately), [agent integration](AGENT_INTEGRATION.md), and the [production command reference](production-cli.md) |
| Check v2.0.5 scope and issue status | [Version 2 qualification record](development/version-2.md) (issues #23–#26 were canceled and closed as not planned; they remain unqualified) |
| Evaluate experimental cloud recovery, retention or Linux coverage | [Experimental program and limits](development/version-2.md#experimental-cloud-and-machine-recovery-features) and [cloud storage](cloud-storage.md) |

## Operator references

| Reference | Use it for |
|:--|:--|
| [architecture.md](architecture.md) | Shared operations, durable state, and safety boundaries |
| [cloud-storage.md](cloud-storage.md) | Implemented B2/AWS paths and their qualification limits |
| [encryption.md](encryption.md) | Restic password custody, deferred off-host custody, and legacy file encryption |
| [cli-skills.md](cli-skills.md) | Generated command and agent-skills catalog |

## Maintainer references

| Reference | Use it for |
|:--|:--|
| [VERSIONING.md](VERSIONING.md) | Canonical package version, release tags, and validation |
| [PUBLISHING_CHECKLIST.md](PUBLISHING_CHECKLIST.md) | Community files and release readiness checks |
| [development/version-2.md](development/version-2.md) | Qualification evidence, candidate results, and deferred assurance |
| [development/release-dependencies.json](development/release-dependencies.json) | Reviewed package versions and dependency audit evidence |
| [assets/README.md](assets/README.md) | Documentation asset inventory and visual standards |

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
