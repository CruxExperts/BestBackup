"""Private, revision-checked configuration editing for CLI and dashboard."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from .models import Configuration, ConfigurationError, strict_json
from .enrollment import (machine_identity, parse_pair, policy_lease, read_pair,
                         verify_bytes, write_bytes)
from .service import BackupService


_MAX_DOCUMENT = 1024 * 1024


def _document(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > _MAX_DOCUMENT:
        raise ConfigurationError("Configuration exceeds 1 MiB")
    return data


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ConfigurationError("Configuration must be a regular file")
        content = stream.read(_MAX_DOCUMENT + 1)
    if len(content) > _MAX_DOCUMENT:
        raise ConfigurationError("Configuration exceeds 1 MiB")
    return content


def read_document(path: Path) -> str:
    """Read at most one MiB from a real UTF-8 configuration input file."""
    return _read(path).decode("utf-8")


@contextmanager
def _staged(path: Path, data: bytes):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".bbackup-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _review_revision(pair: tuple[bytes, bytes]) -> str:
    return ":".join(hashlib.sha256(content).hexdigest() for content in pair)


def _require_review(pair: tuple[bytes, bytes], revision: str | None) -> None:
    if revision is not None and _review_revision(pair) != revision:
        raise ConfigurationError("Configuration changed since review; refresh mappings and review again")


class ConfigurationWorkspace:
    """Own explicit portable-config and private-binding paths, never secrets."""

    def __init__(self, config_path: Path, bindings_path: Path):
        self.config_path = config_path.absolute()
        self.bindings_path = bindings_path.absolute()
        if self.config_path == self.bindings_path:
            raise ConfigurationError("Configuration and bindings need separate files")

    def read(self) -> dict[str, str]:
        content = _read(self.config_path)
        Configuration.parse(strict_json(content.decode("utf-8")))
        return {"document": content.decode("utf-8"),
                "revision": hashlib.sha256(content).hexdigest()}

    def save(self, document: str, revision: str) -> dict[str, str]:
        """Validate with current bindings, preserve old bytes, then replace once."""
        content = _document(document)
        configuration = Configuration.parse(strict_json(document))
        preliminary_policy, preliminary_bindings = read_pair(self.config_path, self.bindings_path)
        existing, preliminary_host = parse_pair(preliminary_policy, preliminary_bindings)
        if configuration.schema_version != existing.schema_version:
            raise ConfigurationError("Configuration save cannot change schema version; use config migrate")
        state = preliminary_host.state_dir.resolve()
        with policy_lease(state, exclusive=True):
            previous, binding_bytes = read_pair(self.config_path, self.bindings_path)
            current, current_host = parse_pair(previous, binding_bytes)
            if current_host.state_dir.resolve() != state or current.schema_version != configuration.schema_version:
                raise ConfigurationError("Configuration generation changed; reload before saving")
            if configuration.schema_version == 3:
                verify_bytes(state, previous, binding_bytes)
            if hashlib.sha256(previous).hexdigest() != revision:
                raise ConfigurationError("Configuration changed; reload before saving")
            _, candidate_host = parse_pair(content, binding_bytes)
            service = BackupService(configuration, candidate_host)
            backup_dir = service.state / "configuration-backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            if backup_dir.is_symlink() or backup_dir.stat().st_mode & 0o077:
                raise ConfigurationError("Configuration backups must be private")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backup_dir / f"config-{stamp}-{uuid.uuid4().hex}.json.gz"
            with _staged(backup, gzip.compress(previous, mtime=0)) as temporary:
                os.link(temporary, backup)
            _sync_directory(backup_dir)
            with _staged(self.config_path, content) as temporary:
                # Also catch edits made while the backup was being flushed.
                if read_pair(self.config_path, self.bindings_path) != (previous, binding_bytes):
                    raise ConfigurationError("Configuration changed; reload before saving")
                os.replace(temporary, self.config_path)
            _sync_directory(self.config_path.parent)
            if configuration.schema_version == 3:
                if read_pair(self.config_path, self.bindings_path) != (content, binding_bytes):
                    raise ConfigurationError("Configuration changed after replacement; enrollment was not refreshed")
                write_bytes(service.state, content, binding_bytes)
        return {"revision": hashlib.sha256(content).hexdigest(), "backup": str(backup)}

    def setup(self, configuration: str, bindings: str) -> BackupService:
        """Enroll new files only; never replace an existing host declaration."""
        config_content, binding_content = _document(configuration), _document(bindings)
        parsed = Configuration.parse(strict_json(configuration))
        if parsed.schema_version != 3:
            raise ConfigurationError("New setup requires version 3; use config migrate for version 2")
        if self.config_path.exists() or self.config_path.is_symlink():
            raise ConfigurationError("Setup requires a new configuration path")
        if self.bindings_path.exists() or self.bindings_path.is_symlink():
            raise ConfigurationError("Setup requires a new bindings path")
        with _staged(self.config_path, config_content) as config_temp:
            with _staged(self.bindings_path, binding_content) as bindings_temp:
                _, host = parse_pair(config_content, binding_content)
                machine_identity()
                with policy_lease(host.state_dir, exclusive=True, create=True):
                    service = BackupService(parsed, host)
                    if self.config_path.exists() or self.bindings_path.exists():
                        raise ConfigurationError("Setup paths changed; retry with new paths")
                    os.link(config_temp, self.config_path)
                    try:
                        os.link(bindings_temp, self.bindings_path)
                    except OSError:
                        # Remove only the exact link this attempt created.
                        if os.path.samestat(config_temp.stat(), self.config_path.lstat()):
                            self.config_path.unlink()
                        raise
                    _sync_directory(self.config_path.parent)
                    _sync_directory(self.bindings_path.parent)
                    if read_pair(self.config_path, self.bindings_path) != (config_content, binding_content):
                        raise ConfigurationError("Setup files changed before enrollment")
                    write_bytes(service.state, config_content, binding_content)
        return service

    def candidates(self) -> dict[str, object]:
        """Inspect the current pair without mutating host state."""
        pair = read_pair(self.config_path, self.bindings_path)
        configuration, bindings = parse_pair(*pair)
        sources = [bindings.source_bindings[source.name].resolve(source)
                   if configuration.schema_version == 3 else source
                   for source in configuration.sources]
        return {"schema_version": configuration.schema_version,
                "review_revision": _review_revision(pair),
                "sources": [{"name": source.name, "kind": source.kind,
                             "selector": list(source.paths) or [source.container or source.database],
                             "connection": source.connection}
                            for source in sources],
                "repositories": [{"name": name, "location": binding.repository}
                                 for name, binding in bindings.repositories.items()],
                "jobs": [job.name for job in configuration.jobs],
                "state_dir": str(bindings.state_dir)}

    def migrate(self, *, review_revision: str | None = None) -> dict[str, object]:
        """Write new enrolled v3 files beside untouched v2 originals."""
        policy_bytes, binding_bytes = read_pair(self.config_path, self.bindings_path)
        _require_review((policy_bytes, binding_bytes), review_revision)
        configuration, _ = parse_pair(policy_bytes, binding_bytes)
        original_config = strict_json(policy_bytes.decode("utf-8"))
        original_bindings = strict_json(binding_bytes.decode("utf-8"))
        if configuration.schema_version != 2 or original_bindings.get("schema_version") != 2:
            raise ConfigurationError("Migration requires a matching version 2 pair")
        policy_sources = []
        source_bindings = {}
        for source in original_config["sources"]:
            name, kind = source["name"], source["kind"]
            policy = {"name": name, "kind": kind}
            if kind in ("mysql", "mariadb"):
                policy["quiesce"] = source["quiesce"]
            if kind == "docker":
                policy["stop_policy"] = source["stop_policy"]
            policy_sources.append(policy)
            if kind in ("files", "sqlite"):
                source_bindings[name] = {"paths": source["paths"]}
            elif kind == "docker":
                source_bindings[name] = {"container": source["container"],
                                         "connection": source["connection"]}
            else:
                source_bindings[name] = {"database": source["database"],
                                         "connection": source["connection"]}
        new_config = dict(original_config, schema_version=3, sources=policy_sources)
        new_bindings = dict(original_bindings, schema_version=3, source_bindings=source_bindings)
        new_paths = ConfigurationWorkspace(self.config_path.with_name(
            f"{self.config_path.stem}.v3{self.config_path.suffix}"), self.bindings_path.with_name(
            f"{self.bindings_path.stem}.v3{self.bindings_path.suffix}"))
        new_paths.setup(json.dumps(new_config), json.dumps(new_bindings))
        return {"configuration_path": str(new_paths.config_path),
                "bindings_path": str(new_paths.bindings_path), "enrolled": True,
                "originals_preserved": True}

    def enroll(self, *, review_revision: str | None = None) -> dict[str, object]:
        """Explicitly trust this exact v3 pair on the current machine and UID."""
        preliminary = read_pair(self.config_path, self.bindings_path)
        _require_review(preliminary, review_revision)
        configuration, bindings = parse_pair(*preliminary)
        if configuration.schema_version != 3:
            raise ConfigurationError("Service enrollment requires version 3; run config migrate")
        machine_identity()
        with policy_lease(bindings.state_dir, exclusive=True, create=True):
            current = read_pair(self.config_path, self.bindings_path)
            if current != preliminary:
                raise ConfigurationError("Configuration changed before enrollment; review again")
            configuration, bindings = parse_pair(*current)
            service = BackupService(configuration, bindings)
            if read_pair(self.config_path, self.bindings_path) != current:
                raise ConfigurationError("Configuration changed before enrollment; review again")
            write_bytes(service.state, *current)
        return {"enrolled": True, "schema_version": 3}
