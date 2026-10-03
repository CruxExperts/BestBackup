---
name: Bug report
about: Something is broken or behaving unexpectedly
labels: bug
assignees: ''
---

## What happened

<!-- Describe the expected and observed behavior. Identify legacy 1.x or the v2 production preview. -->

## Steps to reproduce

1. 
2. 
3. 

## Command and output

<!-- Paste the command and a sanitized result. Remove passwords, tokens, private paths, customer data, and credentials. Never paste bindings.json or secret files. -->

```text
bbackup ...

```

## Environment

- Linux distribution and version:
- Python version (`uv run python --version` or `python3 --version`):
- bbackup version (`bbackup --version`):
- Installation method (uv tool / uv sync / symlink / PATH):
- Workflow (legacy 1.x / `bbackup production` preview):
- Storage type (local / Backblaze B2 / Amazon S3 / other):
- Restic version, when using the production preview:

## Configuration

<!-- Paste only relevant portable config fields. Remove passwords, access keys, sensitive paths, and all private host bindings. -->

```yaml

```

## Additional context

<!-- Say whether this worked previously. For uncertain mutations, state whether you inspected the destination; do not retry solely to produce a second error. -->
