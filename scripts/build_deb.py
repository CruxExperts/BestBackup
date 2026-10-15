#!/usr/bin/env python3
"""Build a native Ubuntu .deb from a wheel, uv.lock, and verified restic binary."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = ROOT / "packaging" / "debian"
TARGETS = {
    "24.04": {"python": "3.12", "codename": "noble"},
    "26.04": {"python": "3.14", "codename": "resolute"},
}
ARCHITECTURES = {"x86_64": ("amd64", 62), "aarch64": ("arm64", 183)}
MAX_LOCK_BYTES = 16 * 1024 * 1024
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_WHEEL_MEMBERS = 10000
MAX_WHEEL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024


class BuildError(Exception):
    """An actionable input or packaging failure."""


def uv_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    return environment


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BuildError(f"{label} is not readable at {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BuildError(f"{label} must be a regular file, not a symlink or special file: {path}")
    return path


def parse_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"Cannot read operating-system release metadata at {path}: {exc}") from exc
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key not in {"ID", "VERSION_ID", "VERSION_CODENAME", "UBUNTU_CODENAME"}:
            continue
        try:
            parts = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise BuildError(f"Malformed {key} value in {path}: {exc}") from exc
        if len(parts) != 1:
            raise BuildError(f"Malformed {key} value in {path}")
        values[key] = parts[0]
    return values


def host_architecture() -> tuple[str, int]:
    machine = platform.machine().lower()
    try:
        return ARCHITECTURES[machine]
    except KeyError as exc:
        raise BuildError(
            f"Unsupported build architecture {machine!r}; native amd64 or arm64 is required"
        ) from exc

def has_native_cpython_abi(soabi: str, major: int, minor: int, machine: str) -> bool:
    return soabi.startswith(f"cpython-{major}{minor}-{machine}-")



def validate_target(release: str, os_release: dict[str, str]) -> tuple[str, str]:
    target = TARGETS[release]
    architecture, _ = host_architecture()
    machine = platform.machine().lower()
    if sys.implementation.name != "cpython":
        raise BuildError("The package must be built with native CPython")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if python_version != target["python"]:
        raise BuildError(
            f"Ubuntu {release} packages require native Python {target['python']}; "
            f"the current interpreter is Python {python_version}"
        )
    soabi = sysconfig.get_config_var("SOABI") or ""
    if not has_native_cpython_abi(soabi, sys.version_info.major, sys.version_info.minor, machine):
        expected_soabi = f"cpython-{sys.version_info.major}{sys.version_info.minor}-{machine}"
        raise BuildError(f"The current interpreter does not have the expected CPython ABI {expected_soabi}")
    if struct.calcsize("P") != 8:
        raise BuildError("Only 64-bit amd64 and arm64 build interpreters are supported")
    if machine not in ARCHITECTURES:
        raise BuildError(f"Unsupported build architecture {machine!r}")

    host_id = os_release.get("ID", "").lower()
    host_version = os_release.get("VERSION_ID", "")
    if (
        host_id == "ubuntu"
        and host_version == release
        and os_release.get("VERSION_CODENAME") == target["codename"]
    ):
        pass
    elif (
        release == "24.04"
        and host_id == "linuxmint"
        and host_version == "22.3"
        and os_release.get("UBUNTU_CODENAME") == "noble"
    ):
        pass
    else:
        raise BuildError(
            f"Target Ubuntu {release} requires a native Ubuntu {release} environment; "
            "the only ABI-build exception is Linux Mint 22.3 based on noble for Ubuntu 24.04. "
            "Mint builds do not qualify Ubuntu runtime behavior."
        )
    return architecture, python_version


def normalized_requirement(value: str) -> tuple[str, tuple[str, ...], str, str]:
    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise BuildError(f"Invalid package requirement {value!r}: {exc}") from exc
    if requirement.url:
        raise BuildError(f"Direct URL requirement is not supported for package metadata: {value!r}")
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        str(requirement.specifier),
        str(requirement.marker) if requirement.marker else "",
    )


def read_lock(path: Path) -> tuple[str, str, tuple[tuple[str, tuple[str, ...], str, str], ...]]:
    require_regular_file(path, "uv.lock")
    if path.stat().st_size > MAX_LOCK_BYTES:
        raise BuildError(f"uv.lock exceeds the supported {MAX_LOCK_BYTES}-byte size limit")
    raw = path.read_bytes()
    try:
        lock = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BuildError(f"Cannot parse {path} as UTF-8 TOML: {exc}") from exc
    projects = [
        package
        for package in lock.get("package", [])
        if package.get("name", "").lower().replace("_", "-") == "bbackup"
        and package.get("source", {}).get("editable") == "."
    ]
    if len(projects) != 1 or not isinstance(projects[0].get("version"), str):
        raise BuildError("uv.lock must contain exactly one editable root package named bbackup")
    locked_metadata = projects[0].get("metadata", {})
    locked_requirements = locked_metadata.get("requires-dist")
    if not isinstance(locked_requirements, list):
        raise BuildError("uv.lock root package is missing metadata.requires-dist")
    expected = tuple(sorted(
        normalized_requirement(
            requirement["name"]
            + requirement.get("specifier", "")
            + (f"; {requirement['marker']}" if requirement.get("marker") else "")
        )
        for requirement in locked_requirements
    ))
    return hashlib.sha256(raw).hexdigest(), projects[0]["version"], expected


def inspect_wheel(
    path: Path,
    expected_version: str,
    python_version: str,
    architecture: str,
    expected_requires_dist: tuple[tuple[str, tuple[str, ...], str, str], ...],
) -> None:
    require_regular_file(path, "bbackup wheel")
    if path.stat().st_size > MAX_WHEEL_BYTES:
        raise BuildError(f"Wheel exceeds the supported {MAX_WHEEL_BYTES}-byte size limit")
    parts = path.name.split("-")
    if len(parts) not in {5, 6} or parts[0].lower().replace("_", "-") != "bbackup" or not parts[-1].endswith(".whl"):
        raise BuildError(f"Expected a bbackup wheel filename, got {path.name!r}")
    if parts[1] != expected_version:
        raise BuildError(f"Wheel version {parts[1]!r} does not match uv.lock version {expected_version!r}")

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_WHEEL_MEMBERS:
                raise BuildError(f"Wheel contains more than {MAX_WHEEL_MEMBERS} archive members")
            total_size = 0
            for member in members:
                name = Path(member.filename)
                member_mode = member.external_attr >> 16
                if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
                    raise BuildError(f"Wheel contains an unsafe archive path: {member.filename!r}")
                if stat.S_ISLNK(member_mode):
                    raise BuildError(f"Wheel contains a symbolic link: {member.filename!r}")
                total_size += member.file_size
                if total_size > MAX_WHEEL_UNCOMPRESSED_BYTES:
                    raise BuildError("Wheel's declared uncompressed content exceeds the 1 GiB safety limit")
            metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            wheel_files = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
            entry_files = [name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_files) != 1 or len(wheel_files) != 1 or len(entry_files) != 1:
                raise BuildError("Wheel must contain one METADATA, WHEEL, and entry_points.txt file")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
            wheel_metadata = archive.read(wheel_files[0]).decode("utf-8")
            entry_points = archive.read(entry_files[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BuildError(f"Cannot inspect wheel {path}: {exc}") from exc

    if (metadata.get("Name") or "").lower().replace("_", "-") != "bbackup":
        raise BuildError("Wheel metadata must identify the distribution as bbackup")
    if metadata.get("Version") != expected_version:
        raise BuildError(
            f"Wheel metadata version {metadata.get('Version')!r} does not match uv.lock version {expected_version!r}"
        )
    if metadata.get("Requires-Python") != ">=3.12":
        raise BuildError("Wheel Requires-Python metadata must match this project's >=3.12 requirement")
    wheel_requirements = tuple(sorted(
        normalized_requirement(value) for value in metadata.get_all("Requires-Dist", [])
    ))
    if wheel_requirements != expected_requires_dist:
        raise BuildError("Wheel Requires-Dist metadata does not match uv.lock root package metadata")
    required_entry_points = {
        "bbackup": "bbackup.cli:cli",
        "bbman": "bbackup.bbman_entry:cli",
    }
    parsed_entry_points: dict[str, str] = {}
    in_console_scripts = False
    for line in entry_points.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_console_scripts = stripped == "[console_scripts]"
        elif in_console_scripts and "=" in stripped:
            name, target = stripped.split("=", 1)
            parsed_entry_points[name.strip()] = target.strip()
    if parsed_entry_points != required_entry_points:
        raise BuildError("Wheel console entry points must define exactly the bbackup and bbman project commands")

    target_number = python_version.replace(".", "")
    target_arch = "x86_64" if architecture == "amd64" else "aarch64"
    tags = [line[4:].strip() for line in wheel_metadata.splitlines() if line.startswith("Tag: ")]
    if not tags:
        raise BuildError("Wheel WHEEL metadata does not declare a Tag")
    filename_tags = {
        f"{python_tag}-{abi_tag}-{platform_tag}"
        for python_tag in parts[-3].split(".")
        for abi_tag in parts[-2].split(".")
        for platform_tag in parts[-1].removesuffix(".whl").split(".")
    }
    if not set(tags).issubset(filename_tags):
        raise BuildError("Wheel filename tags do not agree with its WHEEL metadata")
    compatible = False
    for tag in tags:
        tag_parts = tag.split("-")
        if len(tag_parts) != 3:
            raise BuildError(f"Malformed wheel tag {tag!r}")
        python_tag, abi_tag, platform_tag = tag_parts
        python_ok = python_tag in {"py3", f"py{target_number}", f"cp{target_number}"}
        abi_ok = abi_tag in {"none", "abi3", f"cp{target_number}"}
        platform_ok = platform_tag == "any" or (
            platform_tag.startswith(("manylinux", "linux_")) and target_arch in platform_tag
        )
        if python_ok and abi_ok and platform_ok:
            compatible = True
            break
    if not compatible:
        raise BuildError(
            f"Wheel has no compatible Python {python_version}/{architecture} tag; refusing cross-architecture artifacts"
        )


def validate_restic(path: Path, expected_sha256: str, architecture: str) -> str:
    require_regular_file(path, "restic executable")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise BuildError("--restic-sha256 must be exactly 64 hexadecimal characters")
    info = path.stat()
    if not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise BuildError("The supplied restic file is not marked executable")
    digest = sha256_file(path)
    if digest.lower() != expected_sha256.lower():
        raise BuildError(f"restic SHA-256 mismatch: expected {expected_sha256.lower()}, received {digest}")
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise BuildError("restic must be a 64-bit little-endian ELF executable")
    machine = struct.unpack_from("<H", header, 18)[0]
    expected_machine = 62 if architecture == "amd64" else 183
    if machine != expected_machine:
        raise BuildError(f"restic ELF architecture does not match target {architecture}")
    return digest


def debian_version(version: str) -> str:
    match = re.fullmatch(
        r"(?P<release>\d+(?:\.\d+)*)(?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?"
        r"(?:\.post(?P<post_n>\d+))?(?:\.dev(?P<dev_n>\d+))?"
        r"(?:\+(?P<local>[a-z0-9]+(?:[.-][a-z0-9]+)*))?",
        version,
    )
    if match is None:
        raise BuildError(f"Unsupported PEP 440 project version for Debian packaging: {version!r}")
    result = match.group("release")
    if match.group("pre"):
        result += f"~{match.group('pre')}{match.group('pre_n')}"
    if match.group("dev_n"):
        result += f"~dev{match.group('dev_n')}"
    if match.group("post_n"):
        result += f"+post{match.group('post_n')}"
    if match.group("local"):
        result += "+" + match.group("local").replace("-", ".")
    return result


def render_asset(name: str, replacements: dict[str, str]) -> str:
    try:
        content = (ASSET_ROOT / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"Cannot read package asset {name}: {exc}") from exc
    for key, value in replacements.items():
        content = content.replace(f"@{key}@", value)
    unresolved = sorted(set(re.findall(r"@[A-Z0-9_]+@", content)))
    if unresolved:
        raise BuildError(f"Unresolved tokens in package asset {name}: {', '.join(unresolved)}")
    return content


def write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(mode)


def copy_third_party_licenses(site_packages: Path, docs: Path) -> int:
    copied = 0
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata_path = dist_info / "METADATA"
        try:
            metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise BuildError(f"Cannot read installed distribution metadata {metadata_path}: {exc}") from exc
        distribution = (metadata.get("Name") or dist_info.name.removesuffix(".dist-info")).lower().replace("_", "-")
        if distribution == "bbackup":
            continue
        version = metadata.get("Version", "unknown")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*", distribution) or not re.fullmatch(
            r"[A-Za-z0-9.+_-]+", version
        ):
            raise BuildError(f"Unsafe installed distribution identity in {metadata_path}")
        destination = docs / "third-party-licenses" / f"{distribution}-{version}"
        candidates: set[Path] = set()
        licenses_dir = dist_info / "licenses"
        if licenses_dir.is_dir():
            candidates.update(path for path in licenses_dir.rglob("*") if path.is_file())
        for item in dist_info.iterdir():
            if item.is_file() and re.match(r"^(license|licence|copying|notice|authors|copyright)(\.|$)", item.name, re.I):
                candidates.add(item)
        for source in sorted(candidates):
            relative = source.relative_to(dist_info)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o644)
            copied += 1
    return copied


def write_copyright(docs: Path) -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8").rstrip()
    content = (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: bbackup\n"
        "Source: https://github.com/CruxExperts/best-backup\n\n"
        "Files: opt/bbackup/*/site-packages/bbackup/* "
        "opt/bbackup/*/site-packages/bbackup-*.dist-info/* "
        "usr/bin/bbackup usr/bin/bbman\n"
        "Copyright: 2026 Slavic Kozyuk, Crux Experts LLC\n"
        "License: MIT\n"
        + "\n".join(" " + line if line else " ." for line in license_text.splitlines())
        + "\n\n"
        "Files: usr/share/doc/bbackup/README.Debian usr/share/doc/bbackup/build-manifest.json "
        "usr/share/doc/bbackup/copyright\n"
        "Copyright: 2026 Crux Experts LLC\n"
        "License: MIT\n"
        "Comment: Build metadata contains no secrets or machine topology.\n\n"
        "Files: usr/share/doc/bbackup/third-party-licenses/*\n"
        "License: see each distribution's retained license text\n"
        "Comment: Third-party rights and licenses remain with their respective owners.\n"
    )
    write_text(docs / "copyright", content)


def export_locked_requirements(uv: str, lock_path: Path, requirement_path: Path, lock_hash: str) -> str:
    command = [
        uv,
        "export",
        "--locked",
        "--no-dev",
        "--no-default-groups",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
        "--format",
        "requirements.txt",
        "--output-file",
        str(requirement_path),
    ]
    try:
        subprocess.run(command, cwd=ROOT, check=True, env=uv_environment())
    except FileNotFoundError as exc:
        raise BuildError(f"uv was not found at {uv!r}") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"uv export failed with exit status {exc.returncode}") from exc
    if sha256_file(lock_path) != lock_hash:
        raise BuildError("uv.lock changed during export; refusing a non-reproducible package")
    try:
        exported = requirement_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"uv did not produce a readable hashed requirements export: {exc}") from exc
    if "--hash=sha256:" not in exported or not re.search(r"(?m)^\S+==\S+", exported):
        raise BuildError("uv export did not produce pinned requirements with SHA-256 hashes")
    return hashlib.sha256(exported.encode("utf-8")).hexdigest()


def remove_local_wheel_provenance(site_packages: Path) -> None:
    project_metadata = list(site_packages.glob("bbackup-*.dist-info"))
    if len(project_metadata) != 1:
        raise BuildError("Installed payload must contain exactly one bbackup distribution metadata directory")
    project_info = project_metadata[0]
    direct_url = project_info / "direct_url.json"
    if direct_url.is_symlink():
        raise BuildError("Installed bbackup metadata contains a symbolic direct_url.json")
    if direct_url.exists():
        record = project_info / "RECORD"
        if not record.is_file() or record.is_symlink():
            raise BuildError("Cannot remove local wheel provenance because bbackup RECORD is missing or unsafe")
        relative_path = direct_url.relative_to(site_packages).as_posix()
        try:
            with record.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.reader(stream))
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            raise BuildError(f"Cannot read installed bbackup RECORD: {exc}") from exc
        remaining = [row for row in rows if not row or row[0] != relative_path]
        if len(remaining) == len(rows) or sum(bool(row) and row[0] == relative_path for row in rows) != 1:
            raise BuildError("bbackup RECORD does not contain exactly one direct_url.json entry")
        temporary_record = record.with_name("RECORD.tmp")
        try:
            with temporary_record.open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream, lineterminator="\n").writerows(remaining)
            temporary_record.chmod(stat.S_IMODE(record.stat().st_mode))
            os.replace(temporary_record, record)
            direct_url.unlink()
        except OSError as exc:
            raise BuildError(f"Cannot remove the local wheel source path from package metadata: {exc}") from exc
    unexpected = list(site_packages.rglob("direct_url.json"))
    if unexpected:
        relative_paths = ", ".join(str(path.relative_to(site_packages)) for path in unexpected)
        raise BuildError(f"Unexpected direct_url.json source metadata would leak build paths: {relative_paths}")


def install_payload(uv: str, wheel: Path, requirements: Path, site_packages: Path) -> None:
    site_packages.mkdir(parents=True)
    commands = [
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site_packages),
            "--no-deps",
            "--no-compile",
            "--only-binary=:all:",
            "--link-mode=copy",
            str(wheel),
        ],
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site_packages),
            "--require-hashes",
            "--no-deps",
            "--no-compile",
            "--only-binary=:all:",
            "--link-mode=copy",
            "--requirement",
            str(requirements),
        ],
    ]
    for command in commands:
        try:
            subprocess.run(command, cwd=ROOT, check=True, env=uv_environment())
        except FileNotFoundError as exc:
            raise BuildError(f"uv was not found at {uv!r}") from exc
        except subprocess.CalledProcessError as exc:
            raise BuildError(f"uv pip install failed with exit status {exc.returncode}") from exc
    remove_local_wheel_provenance(site_packages)
    symlinks = [path for path in site_packages.rglob("*") if path.is_symlink()]
    if symlinks:
        raise BuildError(f"Installed Python payload contains a symbolic link: {symlinks[0].relative_to(site_packages)}")
    required_files = [site_packages / "bbackup" / "cli.py", site_packages / "bbackup" / "bbman_entry.py"]
    missing = [str(path.relative_to(site_packages)) for path in required_files if not path.is_file()]
    if missing:
        raise BuildError(f"Installed wheel is missing required package files: {', '.join(missing)}")


def render_systemd_templates(site_packages: Path) -> dict[str, str]:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        "import bbackup\n"
        "if not Path(bbackup.__file__).resolve().is_relative_to(root):\n"
        "    raise SystemExit('wrong package root')\n"
        "from bbackup.scheduling import unit_templates\n"
        "print(json.dumps(unit_templates(), sort_keys=True))\n"
    )
    try:
        rendered = subprocess.run(
            [sys.executable, "-I", "-c", code, str(site_packages)],
            check=True,
            capture_output=True,
            text=True,
        )
        templates = json.loads(rendered.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, TypeError) as exc:
        raise BuildError(
            f"Could not render systemd templates from the installed wheel: {exc}"
        ) from exc
    expected_names = {"bbackup@.service", "bbackup@.timer"}
    if (
        not isinstance(templates, dict)
        or set(templates) != expected_names
        or any(
            not isinstance(contents, str) or not contents.endswith("\n")
            for contents in templates.values()
        )
    ):
        raise BuildError("Installed wheel did not provide exactly the expected systemd unit templates")
    return templates


def write_payload_manifest(
    path: Path,
    stage: Path,
    version_root: Path,
    binary_dir: Path,
    extra_files: Sequence[Path] = (),
) -> None:
    payload_files = [
        *version_root.rglob("*"),
        binary_dir / "bbackup",
        binary_dir / "bbman",
        *extra_files,
    ]
    entries: list[str] = []
    for file_path in sorted(payload_files):
        if file_path.is_dir():
            continue
        if file_path.is_symlink() or not file_path.is_file():
            raise BuildError(f"Package payload contains a non-regular file: {file_path.relative_to(stage)}")
        relative_path = file_path.relative_to(stage).as_posix()
        if "\n" in relative_path or "\r" in relative_path or "\\" in relative_path:
            raise BuildError(f"Package payload path cannot be represented in checksum manifest: {relative_path!r}")
        entries.append(f"{sha256_file(file_path)}  {relative_path}\n")
    write_text(path, "".join(entries))


def deb_artifact_stem(deb_version: str, target_release: str, architecture: str) -> str:
    return f"bbackup_{deb_version}_ubuntu{target_release}_{architecture}"




def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    write_text(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def run_build(args: argparse.Namespace) -> tuple[Path, str]:
    if os.geteuid() == 0:
        raise BuildError("Run the builder as an unprivileged user; it creates artifacts and never installs packages")
    lock_path = require_regular_file(ROOT / "uv.lock", "uv.lock")
    lock_hash, version, locked_requires_dist = read_lock(lock_path)
    os_release = parse_os_release()
    architecture, python_version = validate_target(args.target_release, os_release)
    wheel = require_regular_file(args.wheel, "bbackup wheel").resolve(strict=True)
    wheel_hash = sha256_file(wheel)
    inspect_wheel(wheel, version, python_version, architecture, locked_requires_dist)
    restic = require_regular_file(args.restic, "restic executable").resolve(strict=True)
    restic_hash = validate_restic(restic, args.restic_sha256, architecture)
    deb_version = debian_version(version)

    output_dir = args.output_dir.expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir = output_dir.resolve(strict=True)
    except OSError as exc:
        raise BuildError(f"Cannot create output directory {output_dir}: {exc}") from exc
    artifact_stem = deb_artifact_stem(deb_version, args.target_release, architecture)
    final_deb = output_dir / f"{artifact_stem}.deb"
    final_manifest = output_dir / f"{artifact_stem}.manifest.json"
    final_sha = output_dir / f"{artifact_stem}.sha256"
    existing = [
        path.name
        for path in (final_deb, final_manifest, final_sha)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise BuildError(f"Refusing to overwrite existing output artifact(s): {', '.join(existing)}")

    uv = shutil.which("uv")
    if not uv:
        raise BuildError("uv must already be installed and available on PATH; the builder does not install tools")
    host_os = os_release
    with tempfile.TemporaryDirectory(prefix=".bbackup-deb-", dir=output_dir) as temporary:
        work = Path(temporary)
        stage = work / "root"
        control_dir = stage / "DEBIAN"
        site_packages = stage / "opt" / "bbackup" / version / "site-packages"
        restic_dir = stage / "opt" / "bbackup" / version / "bin"
        docs = stage / "usr" / "share" / "doc" / "bbackup"
        requirements = work / "runtime-requirements.txt"
        control_dir.mkdir(parents=True)
        lock_export_hash = export_locked_requirements(uv, lock_path, requirements, lock_hash)
        install_payload(uv, wheel, requirements, site_packages)
        if sha256_file(wheel) != wheel_hash:
            raise BuildError("bbackup wheel changed while building; refusing an artifact with an uncertain source")

        restic_dir.mkdir(parents=True)
        shutil.copyfile(restic, restic_dir / "restic")
        if sha256_file(restic_dir / "restic") != restic_hash:
            raise BuildError("Staged restic bytes do not match the reviewed SHA-256 digest")
        (restic_dir / "restic").chmod(0o755)
        (stage / "usr" / "bin").mkdir(parents=True)
        replacements = {
            "LOCK_PATH": "/run/lock/bbackup-package.lock",
            "PENDING_PATH": "/var/lib/bbackup-package/upgrade-pending",
            "STATE_DIR": "/var/lib/bbackup-package",
            "VERSION_ROOT": f"/opt/bbackup/{version}",
            "PYTHON": f"/usr/bin/python{python_version}",
            "BINARY_DIR": "/usr/bin",
            "TMPFILES_FILE": "/usr/lib/tmpfiles.d/bbackup.conf",
            "PAYLOAD_MANIFEST": "/usr/lib/bbackup/payload.sha256",
            "ARCH": architecture,
            "PYTHON_VERSION": python_version,
            "ROOT_UID": "0",
            "ROOT_GID": "0",
        }
        write_text(stage / "usr" / "bin" / "bbackup", render_asset("bbackup.in", replacements), 0o755)
        write_text(stage / "usr" / "bin" / "bbman", render_asset("bbman.in", replacements), 0o755)
        write_text(stage / "usr" / "lib" / "tmpfiles.d" / "bbackup.conf", render_asset("tmpfiles.conf", replacements))
        unit_paths = []
        for unit_name, unit_text in render_systemd_templates(site_packages).items():
            unit_path = stage / "usr" / "lib" / "systemd" / "system" / unit_name
            write_text(unit_path, unit_text)
            unit_paths.append(unit_path)
        write_text(docs / "README.Debian", render_asset("README.Debian", replacements))
        (docs / "third-party-licenses").mkdir(parents=True, exist_ok=True)
        licenses_copied = copy_third_party_licenses(site_packages, docs)
        runtime_payload = [
            stage / "usr" / "lib" / "tmpfiles.d" / "bbackup.conf",
            *unit_paths,
        ]
        write_payload_manifest(
            stage / "usr" / "lib" / "bbackup" / "payload.sha256",
            stage,
            stage / "opt" / "bbackup" / version,
            stage / "usr" / "bin",
            runtime_payload,
        )
        write_copyright(docs)

        if sha256_file(lock_path) != lock_hash:
            raise BuildError("uv.lock changed during package construction; refusing a non-reproducible package")
        manifest: dict[str, object] = {
            "manifest_schema": 1,
            "package": {"name": "bbackup", "version": version, "debian_version": deb_version},
            "target": {
                "distribution": "Ubuntu",
                "release": args.target_release,
                "architecture": architecture,
                "python": python_version,
            },
            "builder": {
                "os_id": host_os.get("ID", "unknown"),
                "os_version": host_os.get("VERSION_ID", "unknown"),
                "python_implementation": sys.implementation.name,
                "python": python_version,
                "architecture": architecture,
            },
            "inputs": {
                "wheel": {"filename": wheel.name, "sha256": wheel_hash},
                "uv_lock_sha256": lock_hash,
                "dependency_export_sha256": lock_export_hash,
                "restic_sha256": restic_hash,
            },
            "payload": {
                "third_party_license_files_copied": licenses_copied,
                "versioned_install_root": f"/opt/bbackup/{version}",
                "launchers": ["/usr/bin/bbackup", "/usr/bin/bbman"],
                "systemd_units": [
                    "/usr/lib/systemd/system/bbackup@.service",
                    "/usr/lib/systemd/system/bbackup@.timer",
                ],
            },
        }
        write_manifest(docs / "build-manifest.json", manifest)
        control = (
            "Package: bbackup\n"
            f"Version: {deb_version}\n"
            f"Architecture: {architecture}\n"
            "Section: utils\n"
            "Priority: optional\n"
            "Maintainer: Crux Experts LLC\n"
            f"Depends: python{python_version}, util-linux, systemd\n"
            "Homepage: https://github.com/CruxExperts/best-backup\n"
            "Description: encrypted Linux backup and restore tools\n"
            " bbackup manages encrypted backup workflows using the bundled restic executable.\n"
            " Package installation does not enable or start services.\n"
        )
        write_text(control_dir / "control", control)
        for hook in ("preinst", "postinst", "prerm", "postrm"):
            write_text(control_dir / hook, render_asset(hook, replacements), 0o755)

        temporary_deb = work / final_deb.name
        command = ["dpkg-deb", "--build", "--root-owner-group", "--uniform-compression", str(stage), str(temporary_deb)]
        try:
            subprocess.run(command, check=True)
        except FileNotFoundError as exc:
            raise BuildError("dpkg-deb is required to create the package") from exc
        except subprocess.CalledProcessError as exc:
            raise BuildError(f"dpkg-deb failed with exit status {exc.returncode}") from exc

        package_hash = sha256_file(temporary_deb)
        external_manifest = dict(manifest)
        external_manifest["artifact"] = {"filename": final_deb.name, "sha256": package_hash}
        write_manifest(work / final_manifest.name, external_manifest)
        write_text(work / final_sha.name, f"{package_hash}  {final_deb.name}\n")

        created: list[tuple[Path, int, int]] = []
        try:
            for source, destination in (
                (temporary_deb, final_deb),
                (work / final_manifest.name, final_manifest),
                (work / final_sha.name, final_sha),
            ):
                source_info = source.stat()
                os.link(source, destination)
                created.append((destination, source_info.st_dev, source_info.st_ino))
                source.unlink()
        except OSError:
            for path, device, inode in created:
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) == (device, inode):
                    path.unlink()
            raise
    return final_deb, package_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-release", choices=sorted(TARGETS), required=True)
    parser.add_argument("--wheel", type=Path, required=True, help="bbackup wheel matching this repository's uv.lock")
    parser.add_argument("--restic", type=Path, required=True, help="supplied native restic executable")
    parser.add_argument("--restic-sha256", required=True, help="reviewed SHA-256 digest of the executable bytes")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        artifact, digest = run_build(build_parser().parse_args(argv))
    except BuildError as exc:
        print(f"build_deb: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"build_deb: filesystem operation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built {artifact}")
    print(f"SHA-256: {digest}")
    print(f"Manifest: {artifact.with_suffix('.manifest.json')}")
    print(f"Checksum: {artifact.with_suffix('.sha256')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
