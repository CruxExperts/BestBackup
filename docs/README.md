# Documentation

Start with the [README](../README.md), then the [guided quick start](../QUICKSTART.md). The v2.0.3 guides cover schema-3 enrollment, guided setup, safety improvements, and the separately recorded user-timer reboot qualification. Older v2.0.2 installations retain their [versioned documentation](https://github.com/CruxExperts/BestBackup/tree/v2.0.2/docs).

The [release dependency evidence](development/release-dependencies.json) records
the exact reviewed versions, source timestamps, lock hash, and audit limits.

## Start using bbackup

| Guide | Use it for |
|:--|:--|
| [QUICKSTART.md](../QUICKSTART.md) | Guided setup, schema-3 migration, private bindings, scheduled capture, and verified restore |
| [INSTALL.md](../INSTALL.md) | Prerequisites, pinned releases, current release status, and development setup |
| [cloud-storage.md](cloud-storage.md) | Implemented B2/AWS paths, not production-qualified; provider and protection limits |
| [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md) | Machine-readable schemas, JSON envelopes, bounded run records, and safe restore/reconciliation |
| [management.md](management.md) | Retained 1.x `bbman`, Docker, and YAML management commands; native v2 Docker unqualified |
| [encryption.md](encryption.md) | Restic password custody, deferred off-host custody, and legacy file encryption |
| [cli-skills.md](cli-skills.md) | Generated command and agent skills catalog |

## Architecture and release evidence

| Reference | Contents |
|:--|:--|
| [development/version-2.md](development/version-2.md) | Selected stable-release scope, qualification evidence, deferred assurances, and historical alpha1 record |
| [production-cli.md](production-cli.md) | Generated v2 command reference and input schemas |
| [architecture.md](architecture.md) | v2 operation service, tested support boundary, storage model, and retained 1.x implementation |
| [VERSIONING.md](VERSIONING.md) | Canonical package version, release tags, and validation |
| [PUBLISHING_CHECKLIST.md](PUBLISHING_CHECKLIST.md) | Community files and release readiness checks |
| [assets/README.md](assets/README.md) | Documentation asset inventory and visual standards |
| [prompts/bootstrap-planning-agent.md](prompts/bootstrap-planning-agent.md) | Maintainer planning prompt for repository work |

## Documentation standards

- [GitHub Markdown writing standard](standards/github-markdown/github-markdown-writing-standard.md)
- [GitHub Markdown capabilities reference](standards/github-markdown/github-markdown-capabilities-reference.md)
- [GitHub Markdown review checklist](standards/github-markdown/github-markdown-review-checklist.md)
- [GitHub Markdown source provenance](standards/github-markdown/github-markdown-source-provenance.md)
- [GitHub Markdown provenance manifest](standards/github-markdown/github-markdown-provenance-manifest.json)

Back to [README.md](../README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
