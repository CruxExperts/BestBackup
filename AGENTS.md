# bbackup Agent Instructions

This repo contains the source for `bbackup`, a Python backup utility and its
maintenance tooling.

## Operating Model

- Treat the selected repository checkout as the active development checkout.
- Treat `origin` as `https://github.com/CruxExperts/BestBackup.git`.
- Keep repo policy in this file and durable user-facing docs under `docs/` or
  root entry points such as `README.md`, `INSTALL.md`, and `QUICKSTART.md`.
- Keep runtime state, logs, caches, and agent run ledgers out of Git.
- Use Localsetup-native commands before editing Codex adapter state manually.

## Scope Control

- Make surgical changes that trace directly to the task.
- Do not refactor unrelated backup, restore, encryption, Docker, or TUI code
  while doing maintenance setup.
- Do not commit secrets, keys, backup archives, local service logs, generated
  runtime state, or machine-specific configuration.
- Preserve the existing `.agent/` content unless the user explicitly asks to
  migrate or remove it.

## Localsetup

This checkout uses a Localsetup-managed Codex skills adapter. Inspect it with:

```bash
localsetup adapters --target-directory . --platforms codex
```

Run the repo-level doctor with:

```bash
localsetup doctor --target-directory . --global-preset core --repo-preset core --platforms codex --dependency-mode uv-sync --json
```

If adapter shape needs to change, use `localsetup install`, `localsetup plan`,
or `localsetup detach` before manual edits under `.codex/`.

## Development Commands

```bash
git config core.hooksPath .githooks
```

```bash
uv sync --locked
```

```bash
uv run python scripts/check_version_sync.py
```

```bash
uv run python scripts/check_publishing_ready.py
```

```bash
uv run python -m py_compile bbackup.py bbman.py bbackup/*.py bbackup/data/*.py bbackup/management/*.py scripts/*.py
```

```bash
uv run pytest
```

```bash
git diff --check
```

## Documentation

- Update docs in the same task when behavior, commands, install steps, or
  maintenance workflow changes.
- Prefer updating existing docs over creating new documents.
- Keep command examples copy-ready and avoid documenting machine-specific
  secrets or private paths unless they are explicitly local runtime examples.

## Agent Client Compatibility

- Codex CLI and OpenCode are both supported agent clients for this repo.
- Root `AGENTS.md` is the shared instruction surface for both tools.
- Keep Codex-only behavior in Codex-native surfaces and OpenCode-only behavior in OpenCode-native surfaces.
