"""Backup manifest generation and verification."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

import bbackup

from .config import BackupScope, FilesystemTarget
from .config import open_private_file


MANIFEST_NAME = "backup_manifest.json"
MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_manifest_files(backup_dir: Path) -> Iterable[Path]:
    for path in sorted(backup_dir.rglob("*")):
        try:
            is_regular_file = stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            is_regular_file = False
        if is_regular_file and path.name != MANIFEST_NAME:
            yield path


def _volume_artifacts(backup_dir: Path) -> List[Dict[str, str]]:
    volumes_dir = backup_dir / "volumes"
    if not volumes_dir.exists():
        return []

    artifacts = []
    for item in sorted(volumes_dir.iterdir()):
        if item.is_dir():
            artifacts.append({
                "name": item.name,
                "type": "directory",
                "path": item.relative_to(backup_dir).as_posix(),
            })
        elif item.is_file():
            for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
                if item.name.endswith(suffix):
                    artifacts.append({
                        "name": item.name[:-len(suffix)],
                        "type": "archive",
                        "path": item.relative_to(backup_dir).as_posix(),
                    })
                    break
    return artifacts


def generate_backup_manifest(
    backup_dir: Path,
    scope: BackupScope,
    filesystem_targets: Optional[List[FilesystemTarget]] = None,
    encryption_mode: str = "disabled",
    item_results: Optional[Dict[str, Any]] = None,
    errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Write and return a manifest for the current backup directory."""
    backup_dir = Path(backup_dir)
    files = []
    for path in _iter_manifest_files(backup_dir):
        files.append({
            "path": path.relative_to(backup_dir).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })

    manifest: Dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backup_name": backup_dir.name,
        "tool_version": bbackup.__version__,
        "source_scope": {
            "containers": scope.containers,
            "configs": scope.configs,
            "volumes": scope.volumes,
            "networks": scope.networks,
            "filesystems": scope.filesystems,
        },
        "filesystem_original_paths": [
            {"name": target.name, "path": target.path}
            for target in (filesystem_targets or [])
        ],
        "volume_artifacts": _volume_artifacts(backup_dir),
        "encryption_mode": encryption_mode,
        "status": "partial" if errors else "complete",
        "item_results": item_results or {},
        "errors": errors or [],
        "files": files,
    }

    manifest_path = backup_dir / MANIFEST_NAME
    with open_private_file(manifest_path, "w", encoding="utf-8") as manifest_file:
        manifest_file.write(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def verify_backup_manifest(backup_dir: Path) -> List[str]:
    """Return manifest verification errors. Missing manifest means legacy backup."""
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / MANIFEST_NAME
    try:
        manifest_info = manifest_path.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"Invalid backup manifest: {exc}"]
    if not stat.S_ISREG(manifest_info.st_mode):
        return ["Invalid backup manifest: manifest path is not a regular file"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Invalid backup manifest: {exc}"]

    errors = []
    expected_paths = set()
    for entry in manifest.get("files", []):
        rel_path = entry.get("path")
        if not rel_path:
            errors.append("Manifest entry missing path")
            continue
        rel_parts = PurePosixPath(rel_path).parts
        if PurePosixPath(rel_path).is_absolute() or ".." in rel_parts:
            errors.append(f"Manifest file path escapes backup root: {rel_path}")
            continue
        expected_paths.add(rel_path)
        path = backup_dir / rel_path
        try:
            path_info = path.lstat()
        except OSError:
            errors.append(f"Manifest file missing: {rel_path}")
            continue
        if not stat.S_ISREG(path_info.st_mode):
            errors.append(f"Manifest file is not a regular file: {rel_path}")
            continue
        expected_size = entry.get("size")
        if expected_size is not None and path.stat().st_size != expected_size:
            errors.append(f"Manifest file size mismatch: {rel_path}")
            continue
        expected_hash = entry.get("sha256")
        if expected_hash and _sha256(path) != expected_hash:
            errors.append(f"Manifest file hash mismatch: {rel_path}")
    if errors:
        return errors

    for path in _iter_manifest_files(backup_dir):
        rel_path = path.relative_to(backup_dir).as_posix()
        if rel_path not in expected_paths:
            errors.append(f"Manifest file not listed: {rel_path}")
    return errors
