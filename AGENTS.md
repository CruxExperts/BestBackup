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
or `localsetup detach`. Discover the active adapter path from the command above;
do not hand-edit managed `.agents/skills/` or `.codex/skills/` projections.

## Shared operations and generated surfaces

- CLI, dashboard, and timers dispatch through `production_operations.py` so
  exact configuration snapshots, host enrollment, leases, and reconciliation
  remain consistent. Do not dispatch mutations through cached service objects.
- Schema-3 policy is portable; concrete source selectors and credentials belong
  in private bindings. Migration preserves schema-2 originals and requires
  deliberate local enrollment. Never automatically approve a copied declaration.
- Generate CLI catalogs, schemas, and package copies from `cli_metadata.py` with
  `uv run python scripts/generate_cli_skills.py`; use `--check` in validation.
- Active generated artwork is recorded in `docs/assets/provenance.json`; run
  `uv run python scripts/check_assets.py` after asset changes.
- Keep actual qualification evidence and support limits in
  `docs/development/version-2.md`. Preserve historical release evidence and
  distinguish unreleased behavior from published artifacts.

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
