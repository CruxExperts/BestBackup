# Scripts

> Utility scripts for testing, development, and native package generation. Package users do not need to run these scripts.

---

## Available scripts

### Repository integrity checks

`MANIFEST.in` owns the portable source archive contents: runtime code, locked
requirements, documentation/artwork, tests, packaging templates, and maintenance
scripts. Private agent state, environments, keys, and logs are excluded. Build
source archives with `uv build`; source archives and installed wheels have
different purposes, so check both before a release.

`uv run python scripts/generate_dashboard_preview.py` captures the real dashboard
headlessly after a disposable capture/copy/check/restore drill. Capacity is
explicitly illustrative; no host disk measurements or private paths are published.
It requires restic and never touches user backup repositories.

- `uv run python scripts/check_assets.py` verifies active PNG checksums, dimensions,
  required embedded rights metadata, and the provenance manifest.
- `uv run python scripts/generate_cli_skills.py --check` checks generated command
  discovery against the shared command registry. Run without `--check` to refresh.
- `scripts/verify_release_source.py` verifies the signed annotated tag, its exact
  default-branch commit, and successful trusted push CI before release builds and
  again before publication. The publisher uploads to a draft, verifies every
  uploaded asset digest, then publishes the complete release.
- `scripts/generate_release_provenance.py` binds verified source/CI evidence to
  the exact assembled release artifacts, lockfile, and artwork provenance. The
  tag build runs it before creating `SHA256SUMS`, so both source evidence and
  `release-provenance.json` are covered by the release signature.


### Test container and helper images

The test container in [Dockerfile.test](../Dockerfile.test) uses the official
Python 3.12.15 slim Trixie multiarchitecture index
`sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`.
It installs `uv` 0.11.21 from the wheel and SHA-256 in
[uv-bootstrap.txt](../.github/uv-bootstrap.txt) with pip hash checking. Docker
backup, restore, and integration helpers share the official Alpine 3.23.6
multiarchitecture index
`sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0`
through `bbackup/container_images.py`. The corresponding registry manifests
were checked for amd64 and arm64; pinned indexes must be reviewed deliberately
when updating Python or Alpine. The shared bootstrap includes the registry-verified amd64 and arm64 wheel
hashes. The image build and collection were exercised on amd64; accepting the
arm64 wheel hash is not a native arm64 runtime qualification. `apt-get` and `apk add` still resolve current
distribution packages at build time, so these recipes are not byte-for-byte
reproducible. Sources: [Python official image](https://hub.docker.com/_/python),
[Alpine official image](https://hub.docker.com/_/alpine), and
[uv on PyPI](https://pypi.org/project/uv/0.11.21/).

### Rebootable systemd qualification

`scripts/qualify_systemd.py` installs an exact wheel and hash-locked dependencies
in a disposable Ubuntu AMD64 guest. It checks timer capture, an additional
same-guest repository copy, byte-matched file restore, disabled timers, persistent
catch-up after a missed schedule, overlap refusal, and an abrupt guest reset
without automatic replay of an uncertain operation. Both repositories share the
guest; this does not prove off-host disaster recovery.

The host needs Docker, KVM access, `genisoimage`, `gpgv`, and the Ubuntu cloud-image
public keyring. Review the script's pinned image/toolchain before running it.
Supply previously reviewed restic and uv executables, a target-compatible runtime
wheelhouse, and hash-locked requirements exported from `uv.lock`:

```bash
uv run python scripts/qualify_systemd.py \
  --artifact dist/bbackup-2.0.3-py3-none-any.whl \
  --wheel-sha256 '<SHA-256 of that exact wheel>' \
  --ubuntu-codename noble \
  --wheelhouse /path/to/reviewed-runtime-wheels \
  --requirements /path/to/hash-locked-requirements.txt \
  --restic /path/to/reviewed-restic \
  --uv /path/to/reviewed-uv
```

Use `resolute` and Python 3.14-compatible wheels for Ubuntu 26.04; `noble` uses
Python 3.12. The harness downloads a signature-verified Canonical cloud image and
runs QEMU in a task-owned container. It never invokes host systemd. Private logs,
passwords, guest disks, and machine identifiers remain under ignored
`.agents/state/repo-review/systemd/`; do not publish these runtime records.
See [recorded qualification scope](../docs/development/version-2.md) before
making support claims.

### Build a native Ubuntu package


The `build_deb.py` script builds a `.deb` from the supplied bbackup wheel, this repository's `uv.lock`, and a supplied restic 0.19.1 executable. The builder requires an already installed `uv`, `dpkg-deb`, and the `packaging` module in its Python interpreter; on supported Ubuntu/Mint hosts, install `python3-packaging` and invoke `/usr/bin/python3`. It does not download restic, install packages on the host, update `uv.lock`, or install Python packages into system Python. Run it as an unprivileged user.
The locked dependency export explicitly uses the same native interpreter that
ran the builder, so a repository-level `.python-version` pin cannot force a
different Python version.

The builder only accepts native environments: Ubuntu 24.04 with CPython 3.12, or Ubuntu 26.04 with CPython 3.14, on amd64 or arm64. The sole ABI-build exception is Linux Mint 22.3 based on noble for an Ubuntu 24.04-targeted candidate; it does not qualify Ubuntu runtime behavior. Cross-release, cross-Python, and cross-architecture builds are refused.

Provide the restic executable and its reviewed SHA-256 digest as separate inputs. Do not substitute a digest for a compressed archive; the builder checks the executable bytes and its ELF architecture:

```bash
RESTIC=/absolute/path/to/the-reviewed-restic-0.19.1
RESTIC_SHA256='<reviewed SHA-256 for that exact executable>'

/usr/bin/python3 scripts/build_deb.py \
  --target-release 24.04 \
  --wheel dist/bbackup-2.0.3-py3-none-any.whl \
  --restic "$RESTIC" \
  --restic-sha256 "$RESTIC_SHA256" \
  --output-dir dist/deb
```

Use `--target-release 26.04` only from a native Ubuntu 26.04/Python 3.14 environment. A pure-Python wheel can be reused, but the builder checks its name, version, Python tag, architecture compatibility, and normalized `Requires-Dist` metadata against the current `uv.lock` root package metadata before installing anything. Restic must be the executable built for the native target architecture.

The tag workflow builds all four target combinations on native x86_64/arm64 runners in matching, digest-pinned official Ubuntu containers, installs and checks each package in its disposable target container, and assembles the source archives, four `.deb` files, package manifests/checksums, and a combined `SHA256SUMS` into the immutable `release-inputs` artifact. It does not publish a GitHub release or sign anything. The in-package `/usr/share/doc/bbackup/build-manifest.json` records the package version, target release/architecture/Python, build host release, wheel hash, `uv.lock` hash, dependency-export hash, and bundled restic hash. The adjacent sidecar adds the final `.deb` hash, which cannot be embedded in the package without changing that hash. No hostname, user path, credentials, repository URL, or backup state is included. To review a package without installing it:

```bash
PACKAGE=dist/deb/bbackup_2.0.3_ubuntu24.04_amd64.deb
(cd "$(dirname "$PACKAGE")" && sha256sum -c "$(basename "${PACKAGE%.deb}.sha256")")
dpkg-deb --info "$PACKAGE"
dpkg-deb --contents "$PACKAGE"
REVIEW_DIR="$(mktemp -d)"
dpkg-deb --extract "$PACKAGE" "$REVIEW_DIR"
cat "$REVIEW_DIR/usr/share/doc/bbackup/build-manifest.json"
```

Builder regression coverage exercises locked requirement export with an unavailable `.python-version` pin and disabled Python downloads. Lifecycle tests cover active-client upgrade refusal, marker-based launcher refusal, successful configure cleanup, successful recovery after dpkg abort with restored files, and fail-closed behavior for incomplete restoration. These tests use isolated temporary package roots; they do not exercise Ubuntu's dpkg transaction choreography. Package installation and Ubuntu runtime behavior are not qualified merely by creating or inspecting a `.deb`. See [INSTALL.md](../INSTALL.md#debian-package-installation) for install, removal, upgrade, recovery, and rollback behavior.

Publishing is an explicit two-phase operation. After the tag workflow succeeds, download its exact immutable `release-inputs` artifact and verify it before signing:

```bash
read -r -p 'Successful tag-build workflow run ID: ' BUILD_RUN_ID
read -r -p 'Exact v-prefixed tag: ' TAG
gh run download "$BUILD_RUN_ID" --repo CruxExperts/BestBackup \
  --name release-inputs --dir release-inputs
(cd release-inputs && sha256sum --check SHA256SUMS)
FINGERPRINT=AF7968466B5B39C5E928FEFE716342D3EFBA5522
gpg --import .github/release-signing-key.asc
gpg --fingerprint "$FINGERPRINT"
gpg --local-user "$FINGERPRINT" --armor --detach-sign release-inputs/SHA256SUMS
gpg --verify release-inputs/SHA256SUMS.asc release-inputs/SHA256SUMS
jq -n --arg build_run_id "$BUILD_RUN_ID" --arg tag "$TAG" \
  --rawfile detached_signature release-inputs/SHA256SUMS.asc \
  '{build_run_id:$build_run_id,tag:$tag,detached_signature:$detached_signature}' |
  gh workflow run release-notes.yml --repo CruxExperts/BestBackup --ref main --json
```

The dispatched publisher accepts only the detached signature and successful build run ID. It verifies every checksum, the full pinned Crux signing fingerprint, the exact successful tag-build workflow, and the tag-to-commit binding before publishing. Keep the signing private key and passphrase in the operator's local key store; NEVER export, copy, upload, or enter them into GitHub Actions. Missing or invalid signatures fail closed; there is no unsigned release path.

The publisher imports the public certificate from the dispatch commit and checks
its fixed fingerprint. It does not depend on a key server or email-verification
state; the repository contains no private signing material.

```bash
uv run --locked pytest tests/test_deb_package_lifecycle.py
```


### `create_sandbox.py`

Generates a test filesystem with 13,000+ files across realistic directory structures (archives, projects, documents, media). Useful for testing backup, restore, and incremental backup behavior without touching real data.

```bash
uv run python scripts/create_sandbox.py --output /tmp/bbackup_sandbox
uv run python scripts/create_sandbox.py --output /tmp/bbackup_sandbox --quick   # Fewer files, faster
uv run python scripts/create_sandbox.py --output /tmp/bbackup_sandbox --file-count 5000
```

Options:

| Flag | Description |
|---|---|
| `--output, -o` | Output directory (default: `/tmp/bbackup_sandbox`) |
| `--config, -c` | YAML config file for advanced options |
| `--file-count` | Target file count |
| `--size-mb` | Target total size in MB |
| `--harvest-system` | Include read-only copies of real system files |
| `--quick` | Reduced file count for faster runs |
| `--verbose, -v` | Verbose output |

---

### `test_sandbox_backups.py`

Runs backup scenarios against the sandbox created by `create_sandbox.py` and logs results.

```bash
uv run python scripts/test_sandbox_backups.py
```

---

### `populate_postgres.sh`

Populates a PostgreSQL test container with sample data for testing backup/restore with a live database.

```bash
./scripts/populate_postgres.sh
```

---

### `test_backup_with_locks.sh`

Tests backup behavior when the database has active locks, verifying that backups complete cleanly under concurrent write load.

```bash
./scripts/test_backup_with_locks.sh
```

---

### `get_github_key.sh`

Fetches SSH public keys from a GitHub user profile.

```bash
./scripts/get_github_key.sh YOUR_USERNAME
```

---

### `upload_key_to_github.sh`

Uploads an encryption public key to a GitHub Gist.

```bash
./scripts/upload_key_to_github.sh /path/to/backup_public.pem "bbackup encryption public key"
```

The helper prints an explicit `github:USER/gist:GIST_ID` config value. Gist descriptions are labels only; username-only Gist-name discovery is not supported.

---

Back to [README.md](../README.md).

<!-- project-footer:start -->

<br><br>

<p align="center">
Slavic Kozyuk<br>
&copy; 2026 <a href="https://www.cruxexperts.com/">Crux Experts LLC</a> &mdash; <a href="https://github.com/CruxExperts/BestBackup/blob/main/LICENSE">MIT License</a>
</p>

<!-- project-footer:end -->
