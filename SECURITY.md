# Security policy

This policy covers security issues in bbackup's source code and the packaged commands.

## Version status

Version 2.0.5 includes the reviewed safety and schema-3 enrollment changes and the configurable central-retention grace fix. Supported scope and historical release boundaries are recorded in [release evidence](docs/development/version-2.md); reports for released and development code are welcome.

Security fixes are prepared for the latest published release. The project does not promise backports to older versions.

## Report a vulnerability

Do not post security vulnerabilities in a public issue. Use [GitHub's private vulnerability reporting](https://github.com/CruxExperts/BestBackup/security) and include the affected version or commit, impact, and a safe reproduction when available. Redact secrets and customer data. The maintainers will acknowledge reports within five business days and coordinate any public disclosure with the reporter.

## Scope

This policy covers bbackup's first-party code. It does not provide security support for the host operating system, restic, Docker, database servers, or third-party cloud providers and services.

Do not commit encryption keys, restic passwords, cloud credentials, tokens, host bindings, backup archives, or unredacted production configuration. Private files should have owner-only access. Keep independently recoverable copies of repository passwords and recovery-kit keys outside the host being protected.

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
