import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import textwrap

import pytest

from scripts.generate_release_provenance import ProvenanceError, build_provenance, pinned_build_inputs


TAG = "v2.0.3"
VERSION = TAG[1:]
COMMIT = "a" * 40
REPOSITORY = "CruxExperts/BestBackup"
BUILD_RUN_ID = "12345"
BUILD_RUN_URL = f"https://github.com/{REPOSITORY}/actions/runs/{BUILD_RUN_ID}"
UV_HASHES = {"amd64": "b" * 64, "arm64": "c" * 64}
RESTIC_HASHES = {"amd64": "d" * 64, "arm64": "e" * 64}


def archive_bytes(entries, extra=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        top = tarfile.TarInfo(f"bbackup-{VERSION}/")
        top.type = tarfile.DIRTYPE
        archive.addfile(top)
        for name, content in entries.items():
            info = tarfile.TarInfo(f"bbackup-{VERSION}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for name, info in extra:
            info.name = name
            archive.addfile(info)
    return output.getvalue()


def source_entries():
    rows = []
    for release in ("24.04", "26.04"):
        for architecture in ("amd64", "arm64"):
            rows.extend([
                f'          - release: "{release}"',
                f"            architecture: {architecture}",
                f"            runner: ubuntu-24.04{'-arm' if architecture == 'arm64' else ''}",
                f"            container: ubuntu:{release}@sha256:{'f' if release == '24.04' else '9'}{'0' * 63}",
                f"            restic_archive_sha256: {RESTIC_HASHES[architecture]}",
                f"            uv_sha256: {UV_HASHES[architecture]}",
            ])
    workflow = "strategy:\n  matrix:\n    include:\n" + "\n".join(rows) + "\n"
    return {
        "VERSION": f"{VERSION}\n".encode(),
        "pyproject.toml": f"[project]\nname = 'bbackup'\nversion = '{VERSION}'\n".encode(),
        ".python-version": b"3.12\n",
        "uv.lock": b"version = 1\n\n[[package]]\nname = 'bbackup'\n",
        "docs/assets/provenance.json": b'{"schema_version":1,"assets":[]}\n',
        "docs/development/release-dependencies.json": json.dumps({
            "release": {"version": VERSION},
            "lockfile": {"sha256": hashlib.sha256(
                b"version = 1\n\n[[package]]\nname = 'bbackup'\n"
            ).hexdigest()},
        }).encode(),
        "scripts/build_deb.py": b"# reviewed package builder fixture\n",
        "scripts/generate_release_provenance.py": b"# provenance generator fixture\n",
        "scripts/verify_release_source.py": b"# trusted source verifier fixture\n",
        ".github/workflows/release-notes.yml": workflow.encode(),
    }


def write_inputs(root: Path, *, evidence_commit=COMMIT, target_mismatch=False, archive_extra=()):
    root.mkdir()
    wheel_name = f"bbackup-{VERSION}-py3-none-any.whl"
    wheel_bytes = b"fixture wheel bytes"
    wheel = {"filename": wheel_name, "sha256": hashlib.sha256(wheel_bytes).hexdigest()}
    source = archive_bytes(source_entries(), archive_extra)
    (root / f"bbackup-{VERSION}.tar.gz").write_bytes(source)
    (root / wheel_name).write_bytes(wheel_bytes)
    evidence = {"tag": TAG, "commit": evidence_commit, "ci_run_id": 9876, "verified": True}
    (root / "source-verification.json").write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    (root / "release_notes.txt").write_text("Release note fixture text.\n", encoding="utf-8")
    lock_hash = hashlib.sha256(source_entries()["uv.lock"]).hexdigest()
    for release, architecture in sorted({
        ("24.04", "amd64"), ("24.04", "arm64"), ("26.04", "amd64"), ("26.04", "arm64")
    }):
        deb_name = f"bbackup_{VERSION}_ubuntu{release}_{architecture}.deb"
        deb = f"package {release} {architecture}".encode()
        deb_hash = hashlib.sha256(deb).hexdigest()
        (root / deb_name).write_bytes(deb)
        manifest_release = "26.04" if target_mismatch and release == "24.04" else release
        manifest = {
            "manifest_schema": 1,
            "package": {"name": "bbackup", "version": VERSION, "debian_version": f"{VERSION}-1"},
            "target": {"distribution": "Ubuntu", "release": manifest_release, "architecture": architecture},
            "builder": {"architecture": architecture},
            "inputs": {"wheel": wheel, "uv_lock_sha256": lock_hash},
            "artifact": {"filename": deb_name, "sha256": deb_hash},
        }
        stem = deb_name.removesuffix(".deb")
        (root / f"{stem}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / f"{stem}.sha256").write_text(f"{deb_hash}  {deb_name}\n", encoding="ascii")


def generate(root):
    return build_provenance(
        root,
        repository=REPOSITORY,
        tag=TAG,
        commit=COMMIT,
        build_run_id=BUILD_RUN_ID,
        build_run_url=BUILD_RUN_URL,
    )


def test_manifest_binds_source_ci_build_and_all_package_sidecars(tmp_path):
    release_inputs = tmp_path / "release-inputs"
    write_inputs(release_inputs)

    manifest = generate(release_inputs)

    assert manifest["source"]["commit"] == COMMIT
    assert manifest["source"]["trusted_ci_run"]["id"] == 9876
    assert manifest["source"]["build_run"]["url"] == BUILD_RUN_URL
    assert len(manifest["debian_packages"]) == 4
    for package in manifest["debian_packages"]:
        assert set(package) == {"target", "deb", "manifest", "checksum"}


def test_manifest_is_deterministic_and_excludes_private_local_state(tmp_path):
    release_inputs = tmp_path / "release-inputs"
    write_inputs(release_inputs)

    first = generate(release_inputs)
    second = generate(release_inputs)
    serialized = json.dumps(first, sort_keys=True)

    assert first == second
    assert str(tmp_path) not in serialized
    assert "/home/" not in serialized
    assert "PRIVATE_SENTINEL" not in serialized
    assert first["release_notes"]["filename"] == "release_notes.txt"
    assert "Release note fixture text." not in serialized


def test_source_evidence_commit_must_match_release_commit(tmp_path):
    release_inputs = tmp_path / "release-inputs"
    write_inputs(release_inputs, evidence_commit="b" * 40)

    with pytest.raises(ProvenanceError, match="does not bind"):
        generate(release_inputs)


def test_package_manifest_target_must_match_its_artifact_name(tmp_path):
    release_inputs = tmp_path / "release-inputs"
    write_inputs(release_inputs, target_mismatch=True)

    with pytest.raises(ProvenanceError, match="mismatched source or target"):
        generate(release_inputs)


@pytest.mark.parametrize("member_kind", ["traversal", "symlink"])
def test_source_archive_rejects_unsafe_paths_and_symlinks(tmp_path, member_kind):
    release_inputs = tmp_path / "release-inputs"
    if member_kind == "traversal":
        extra = [(f"bbackup-{VERSION}/../../outside", tarfile.TarInfo())]
    else:
        link = tarfile.TarInfo()
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        extra = [(f"bbackup-{VERSION}/private-link", link)]
    write_inputs(release_inputs, archive_extra=extra)

    with pytest.raises(ProvenanceError, match="unsafe member path|link or special"):
        generate(release_inputs)


def test_actual_release_workflow_matrix_is_supported():
    source = source_entries()
    source[".github/workflows/release-notes.yml"] = (
        Path(__file__).parents[1] / ".github/workflows/release-notes.yml"
    ).read_bytes()
    assert len(pinned_build_inputs(source)["targets"]) == 4


def release_publish_script():
    workflow = (Path(__file__).parents[1] / ".github/workflows/release-notes.yml").read_text(
        encoding="utf-8"
    )
    start = workflow.index("      - name: Publish signed release\n")
    section = workflow[start:]
    marker = "        run: |\n"
    body = section.split(marker, 1)[1]
    lines = []
    for line in body.splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines) + "\n"


def release_asset(name, content):
    return {
        "name": name,
        "size": len(content),
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


def release_record(*, release_id, tag=TAG, draft, assets=(), body=""):
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "assets": list(assets),
        "body": body,
    }


def run_release_publish(tmp_path, *, api_responses, patched_release=None):
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner-temp"
    release_inputs = workspace / "release-inputs"
    fake_bin = tmp_path / "fake-bin"
    workspace.mkdir()
    runner_temp.mkdir()
    release_inputs.mkdir()
    fake_bin.mkdir()
    files = {
        "bbackup-2.0.3.tar.gz": b"source archive",
        "bbackup-2.0.3-py3-none-any.whl": b"source wheel",
        "SHA256SUMS": b"checksums\n",
        "release_notes.txt": b"Candidate release notes.\n\n",
    }
    for name, content in files.items():
        (release_inputs / name).write_bytes(content)
    signature = b"detached signature"
    (runner_temp / "SHA256SUMS.asc").write_bytes(signature)
    asset_inventory = [
        release_asset(name, content)
        for name, content in files.items()
        if name != "release_notes.txt"
    ] + [release_asset("SHA256SUMS.asc", signature)]

    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "api_responses": api_responses,
        "api_calls": 0,
        "commands": [],
        "patched_release": patched_release,
    }), encoding="utf-8")
    gh = fake_bin / "gh"
    gh.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        from pathlib import Path

        state_path = Path(os.environ["GH_FIXTURE_STATE"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        args = sys.argv[1:]
        state["commands"].append(args)

        def save():
            state_path.write_text(json.dumps(state), encoding="utf-8")

        if args[:2] == ["api", "--paginate"]:
            index = state["api_calls"]
            state["api_calls"] += 1
            save()
            if index >= len(state["api_responses"]):
                print("unexpected releases API request", file=sys.stderr)
                raise SystemExit(90)
            response = state["api_responses"][index]
            if isinstance(response, dict) and "failure" in response:
                print(response["failure"], file=sys.stderr)
                raise SystemExit(91)
            for page in response:
                print(json.dumps(page, separators=(",", ":")))
        elif args[:2] == ["release", "create"]:
            save()
        elif args[:2] == ["api", "--method"] and args[2] == "PATCH":
            state["patched_id"] = args[3].rsplit("/", 1)[-1]
            save()
            print(json.dumps(state["patched_release"]))
        else:
            print("unexpected gh invocation: " + " ".join(args), file=sys.stderr)
            raise SystemExit(92)
        save()
    """), encoding="utf-8")
    gh.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        "GH_FIXTURE_STATE": str(state_path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKSPACE": str(workspace),
        "GH_TOKEN": "fixture-token",
        "RELEASE_TAG": TAG,
        "RUNNER_TEMP": str(runner_temp),
    })
    result = subprocess.run(
        ["bash", "-c", release_publish_script()],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(state_path.read_text(encoding="utf-8")), asset_inventory


def test_release_publish_resolves_paginated_draft_and_publishes_its_id(tmp_path):
    notes = "Candidate release notes."
    assets = [
        release_asset("bbackup-2.0.3.tar.gz", b"source archive"),
        release_asset("bbackup-2.0.3-py3-none-any.whl", b"source wheel"),
        release_asset("SHA256SUMS", b"checksums\n"),
        release_asset("SHA256SUMS.asc", b"detached signature"),
    ]
    draft = release_record(release_id=731, draft=True, assets=assets, body=notes)
    published = release_record(release_id=731, draft=False, assets=assets, body=notes)

    result, state, _ = run_release_publish(
        tmp_path,
        api_responses=[
            [[release_record(release_id=701, tag="v2.0.2", draft=False)], []],
            [[release_record(release_id=701, tag="v2.0.2", draft=False)], [draft]],
        ],
        patched_release=published,
    )

    assert result.returncode == 0, result.stderr
    assert state["api_calls"] == 2
    assert state["patched_id"] == "731"
    assert sum(command[:2] == ["release", "create"] for command in state["commands"]) == 1
    patch_calls = [command for command in state["commands"] if command[:2] == ["api", "--method"]]
    assert len(patch_calls) == 1
    assert patch_calls[0][2:4] == ["PATCH", f"repos/{REPOSITORY}/releases/731"]


@pytest.mark.parametrize("draft", [True, False], ids=["hidden-draft", "published-release"])
def test_release_publish_refuses_existing_tag_from_paginated_list(tmp_path, draft):
    duplicate = release_record(release_id=842, draft=draft)

    result, state, _ = run_release_publish(
        tmp_path,
        api_responses=[[[release_record(release_id=701, tag="v2.0.2", draft=False)], [duplicate]]],
    )

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert state["api_calls"] == 1
    assert not any(command[:2] == ["release", "create"] for command in state["commands"])
    assert "patched_id" not in state


def test_release_publish_refuses_ambiguous_drafts_after_creation(tmp_path):
    first = release_record(release_id=901, draft=True)
    second = release_record(release_id=902, draft=True)

    result, state, _ = run_release_publish(
        tmp_path,
        api_responses=[
            [[release_record(release_id=701, tag="v2.0.2", draft=False)]],
            [[first], [second]],
        ],
    )

    assert result.returncode != 0
    assert "Expected one release" in result.stderr
    assert state["api_calls"] == 2
    assert sum(command[:2] == ["release", "create"] for command in state["commands"]) == 1
    assert "patched_id" not in state


def test_release_publish_stops_on_paginated_api_failure(tmp_path):
    result, state, _ = run_release_publish(
        tmp_path,
        api_responses=[{"failure": "fixture releases API failure"}],
    )

    assert result.returncode != 0
    assert "fixture releases API failure" in result.stderr
    assert state["api_calls"] == 1
    assert not any(command[:2] == ["release", "create"] for command in state["commands"])
    assert "patched_id" not in state


@pytest.mark.parametrize("mismatch", ["body", "inventory"])
def test_release_publish_refuses_draft_with_changed_notes_or_assets(tmp_path, mismatch):
    assets = [
        release_asset("bbackup-2.0.3.tar.gz", b"source archive"),
        release_asset("bbackup-2.0.3-py3-none-any.whl", b"source wheel"),
        release_asset("SHA256SUMS", b"checksums\n"),
        release_asset("SHA256SUMS.asc", b"detached signature"),
    ]
    draft = release_record(
        release_id=948,
        draft=True,
        assets=assets[:-1] if mismatch == "inventory" else assets,
        body="Edited notes." if mismatch == "body" else "Candidate release notes.\n",
    )

    result, state, _ = run_release_publish(
        tmp_path,
        api_responses=[
            [[]],
            [[draft]],
        ],
    )

    assert result.returncode != 0
    assert ("release notes differ" if mismatch == "body" else "inventory or SHA256 differs") in result.stderr
    assert "patched_id" not in state


def test_release_note_changes_are_bound_without_copying_prose(tmp_path):
    root = tmp_path / "release-inputs"
    write_inputs(root)
    before = generate(root)
    (root / "release_notes.txt").write_text("Changed release note fixture.")
    after = generate(root)
    assert before["release_notes"]["sha256"] != after["release_notes"]["sha256"]
    assert "Changed release note fixture." not in json.dumps(after)


def test_dependency_evidence_must_match_source_lock(tmp_path):
    root = tmp_path / "release-inputs"
    write_inputs(root)
    source = source_entries()
    source["docs/development/release-dependencies.json"] = json.dumps({
        "release": {"version": VERSION}, "lockfile": {"sha256": "0" * 64},
    }).encode()
    (root / f"bbackup-{VERSION}.tar.gz").write_bytes(archive_bytes(source))
    with pytest.raises(ProvenanceError, match="Dependency advisory evidence does not bind"):
        generate(root)
