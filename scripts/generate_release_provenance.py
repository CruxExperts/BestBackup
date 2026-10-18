"""Create a portable, checksum-covered manifest for one validated release build."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tomllib

TARGETS = {
    ("24.04", "amd64"),
    ("24.04", "arm64"),
    ("26.04", "amd64"),
    ("26.04", "arm64"),
}
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TAG = re.compile(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+_-]+)?")


class ProvenanceError(ValueError):
    """The release inputs do not form one safe, verified build."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular_file(root: Path, name: str) -> bytes:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ProvenanceError(f"Expected a regular release input: {name}")
    return path.read_bytes()


def archive_sources(archive: bytes, version: str) -> dict[str, bytes]:
    """Read the small set of provenance inputs without extracting the sdist."""
    prefix = f"bbackup-{version}"
    required = {
        "VERSION",
        "pyproject.toml",
        ".python-version",
        "uv.lock",
        "docs/assets/provenance.json",
        "docs/development/release-dependencies.json",
        "scripts/build_deb.py",
        "scripts/generate_release_provenance.py",
        "scripts/verify_release_source.py",
        ".github/workflows/release-notes.yml",
    }
    selected: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            for member in bundle.getmembers():
                raw_name = member.name
                if (not raw_name or "\\" in raw_name or raw_name.startswith("/")
                        or "\x00" in raw_name):
                    raise ProvenanceError("Source archive contains an unsafe member path")
                parts = raw_name.split("/")
                if member.isdir() and parts[-1] == "":
                    parts.pop()
                if not parts or any(part in ("", ".", "..") for part in parts):
                    raise ProvenanceError("Source archive contains an unsafe member path")
                name = "/".join(parts)
                if name in seen:
                    raise ProvenanceError("Source archive contains duplicate member paths")
                seen.add(name)
                if parts[0] != prefix:
                    raise ProvenanceError("Source archive has an unexpected top-level directory")
                if any(part in {".agent", ".agents", ".codex", ".env"} for part in parts[1:]):
                    raise ProvenanceError("Source archive contains private or runtime state")
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ProvenanceError("Source archive contains a link or special member")
                if member.isfile():
                    relative = "/".join(parts[1:])
                    if relative in required:
                        stream = bundle.extractfile(member)
                        if stream is None:
                            raise ProvenanceError(f"Cannot read source archive member: {relative}")
                        selected[relative] = stream.read()
    except (tarfile.TarError, OSError) as exc:
        raise ProvenanceError("Source archive is invalid") from exc
    if selected.keys() != required:
        missing = sorted(required - selected.keys())
        raise ProvenanceError(f"Source archive is missing provenance inputs: {', '.join(missing)}")
    if selected["VERSION"].decode("utf-8").strip() != version:
        raise ProvenanceError("Source archive version does not match the release tag")
    project = tomllib.loads(selected["pyproject.toml"].decode("utf-8"))
    if project.get("project", {}).get("version") != version:
        raise ProvenanceError("Source project version does not match the release tag")
    return selected


def verified_ci(evidence_bytes: bytes, *, tag: str, commit: str) -> dict[str, object]:
    try:
        evidence = json.loads(evidence_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("Source verification evidence is invalid JSON") from exc
    if (not isinstance(evidence, dict)
            or set(evidence) != {"tag", "commit", "ci_run_id", "verified"}
            or evidence.get("verified") is not True
            or evidence.get("tag") != tag
            or evidence.get("commit") != commit
            or not isinstance(evidence.get("ci_run_id"), int)
            or isinstance(evidence.get("ci_run_id"), bool)
            or evidence["ci_run_id"] <= 0):
        raise ProvenanceError("Source verification does not bind the release tag and commit")
    return evidence


def pinned_build_inputs(source: dict[str, bytes]) -> dict[str, object]:
    workflow = source[".github/workflows/release-notes.yml"].decode("utf-8")
    sections = re.split(r"(?m)^ {10}-(?= release: )", workflow)
    matrix: list[dict[str, str]] = []
    for section in sections[1:]:
        values: dict[str, str] = {}
        for key, pattern in {
            "release": r'^"?(24\.04|26\.04)"?\s*$',
            "architecture": r"^(amd64|arm64)$",
            "runner": r"^(ubuntu-24\.04(?:-arm)?)$",
            "container": r"^(ubuntu:(?:24\.04|26\.04)@sha256:[0-9a-f]{64})$",
            "restic_archive_sha256": r"^([0-9a-f]{64})$",
            "uv_sha256": r"^([0-9a-f]{64})$",
        }.items():
            match = re.search(rf"(?m)^\s+{key}:\s*(.+?)\s*$", section)
            if match:
                value = match.group(1).strip()
                found = re.fullmatch(pattern, value)
                if found:
                    values[key] = found.group(1)
        if values:
            if len(values) != 6:
                raise ProvenanceError("Pinned release build matrix is incomplete or malformed")
            matrix.append(values)
    targets = {(item["release"], item["architecture"]) for item in matrix}
    if len(matrix) != 4 or targets != TARGETS:
        raise ProvenanceError("Pinned release build matrix does not cover the four supported targets")
    return {
        "source_files": [
            {"path": path, "sha256": sha256_bytes(source[path])}
            for path in (
                ".github/workflows/release-notes.yml",
                "scripts/build_deb.py",
                "scripts/generate_release_provenance.py",
                "scripts/verify_release_source.py",
                "pyproject.toml",
                ".python-version",
            )
        ],
        "targets": sorted(matrix, key=lambda item: (item["release"], item["architecture"])),
    }


def artifact_record(root: Path, name: str) -> dict[str, str]:
    data = read_regular_file(root, name)
    return {"filename": name, "sha256": sha256_bytes(data)}


def package_artifacts(root: Path, *, version: str, wheel: dict[str, str], lock_hash: str):
    expected_names: set[str] = set()
    records: list[dict[str, object]] = []
    for release, architecture in sorted(TARGETS):
        deb_name = f"bbackup_{version}_ubuntu{release}_{architecture}.deb"
        manifest_name = deb_name.removesuffix(".deb") + ".manifest.json"
        checksum_name = deb_name.removesuffix(".deb") + ".sha256"
        expected_names.update((deb_name, manifest_name, checksum_name))
        deb_bytes = read_regular_file(root, deb_name)
        try:
            manifest = json.loads(read_regular_file(root, manifest_name))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvenanceError(f"Package manifest is invalid: {manifest_name}") from exc
        deb_hash = sha256_bytes(deb_bytes)
        target = manifest.get("target", {})
        package = manifest.get("package", {})
        inputs = manifest.get("inputs", {})
        artifact = manifest.get("artifact", {})
        if (manifest.get("manifest_schema") != 1
                or package.get("name") != "bbackup"
                or package.get("version") != version
                or target.get("distribution") != "Ubuntu"
                or target.get("release") != release
                or target.get("architecture") != architecture
                or manifest.get("builder", {}).get("architecture") != architecture
                or inputs.get("wheel") != wheel
                or inputs.get("uv_lock_sha256") != lock_hash
                or artifact != {"filename": deb_name, "sha256": deb_hash}):
            raise ProvenanceError(f"Package manifest has mismatched source or target bindings: {manifest_name}")
        checksum = read_regular_file(root, checksum_name)
        if checksum != f"{deb_hash}  {deb_name}\n".encode("ascii"):
            raise ProvenanceError(f"Package checksum sidecar does not bind its package: {checksum_name}")
        records.append({
            "target": {"release": release, "architecture": architecture},
            "deb": {"filename": deb_name, "sha256": deb_hash},
            "manifest": artifact_record(root, manifest_name),
            "checksum": artifact_record(root, checksum_name),
        })
    return records, expected_names


def build_provenance(
    release_inputs: Path,
    *,
    repository: str,
    tag: str,
    commit: str,
    build_run_id: str,
    build_run_url: str,
) -> dict[str, object]:
    if (not REPOSITORY.fullmatch(repository) or not TAG.fullmatch(tag)
            or not COMMIT.fullmatch(commit) or not re.fullmatch(r"[1-9][0-9]*", build_run_id)):
        raise ProvenanceError("Invalid release repository, tag, commit, or build run identity")
    expected_url = f"https://github.com/{repository}/actions/runs/{build_run_id}"
    if build_run_url != expected_url:
        raise ProvenanceError("Build run URL does not match the GitHub build run identity")
    if release_inputs.is_symlink() or not release_inputs.is_dir():
        raise ProvenanceError("Release input directory is missing or unsafe")
    version = tag[1:]
    source_evidence = read_regular_file(release_inputs, "source-verification.json")
    evidence = verified_ci(source_evidence, tag=tag, commit=commit)
    source_archives = list(release_inputs.glob("bbackup-*.tar.gz"))
    wheels = list(release_inputs.glob("bbackup-*.whl"))
    if len(source_archives) != 1 or len(wheels) != 1:
        raise ProvenanceError("Expected exactly one source archive and one wheel")
    source_archive = source_archives[0]
    wheel_path = wheels[0]
    if (source_archive.name != f"bbackup-{version}.tar.gz"
            or wheel_path.name != f"bbackup-{version}-py3-none-any.whl"):
        raise ProvenanceError("Source archive or wheel filename does not match the release tag")
    source_archive_bytes = read_regular_file(release_inputs, source_archive.name)
    source = archive_sources(source_archive_bytes, version)
    wheel = artifact_record(release_inputs, wheel_path.name)
    lock_hash = sha256_bytes(source["uv.lock"])
    advisory_path = "docs/development/release-dependencies.json"
    advisory = json.loads(source[advisory_path])
    if (advisory.get("release", {}).get("version") != version
            or advisory.get("lockfile", {}).get("sha256") != lock_hash):
        raise ProvenanceError("Dependency advisory evidence does not bind the release version and lockfile")
    packages, package_names = package_artifacts(
        release_inputs, version=version, wheel=wheel, lock_hash=lock_hash
    )
    expected_names = {
        source_archive.name,
        wheel_path.name,
        "source-verification.json",
        "release_notes.txt",
        *package_names,
    }
    actual_names: set[str] = set()
    for path in release_inputs.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ProvenanceError("Release inputs contain a link or non-file member")
        actual_names.add(path.name)
    if actual_names != expected_names:
        raise ProvenanceError("Release inputs contain unexpected or missing artifacts")
    return {
        "schema_version": 1,
        "source": {
            "repository": repository,
            "tag": tag,
            "commit": commit,
            "build_run": {"id": int(build_run_id), "url": build_run_url},
            "trusted_ci_run": {
                "id": evidence["ci_run_id"],
                "url": f"https://github.com/{repository}/actions/runs/{evidence['ci_run_id']}",
                "verification_sha256": sha256_bytes(source_evidence),
            },
        },
        "source_artifacts": {
            "archive": artifact_record(release_inputs, source_archive.name),
            "wheel": wheel,
        },
        "build_inputs": {
            "uv_lock_sha256": lock_hash,
            "dependency_advisory_evidence": {
                "path": advisory_path,
                "sha256": sha256_bytes(source[advisory_path]),
                "report": advisory,
            },
            "asset_provenance": {
                "path": "docs/assets/provenance.json",
                "sha256": sha256_bytes(source["docs/assets/provenance.json"]),
            },
            **pinned_build_inputs(source),
        },
        "debian_packages": packages,
        "release_notes": artifact_record(release_inputs, "release_notes.txt"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-inputs", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--build-run-url", required=True)
    args = parser.parse_args()
    try:
        manifest = build_provenance(
            args.release_inputs,
            repository=args.repository,
            tag=args.tag,
            commit=args.commit,
            build_run_id=args.build_run_id,
            build_run_url=args.build_run_url,
        )
        destination = args.release_inputs / "release-provenance.json"
        if destination.exists() or destination.is_symlink():
            raise ProvenanceError("Release provenance output already exists")
        destination.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Release provenance generation failed: {exc}") from None


if __name__ == "__main__":
    main()
