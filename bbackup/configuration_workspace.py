"""Private, revision-checked configuration editing for CLI and dashboard."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import uuid

from .models import Configuration, ConfigurationError, HostBindings, strict_json
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
        bindings = HostBindings.load(self.bindings_path)
        service = BackupService(configuration, bindings)
        lock_path = service.state / "configuration.lock"
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = _read(self.config_path)
            if hashlib.sha256(previous).hexdigest() != revision:
                raise ConfigurationError("Configuration changed; reload before saving")
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
                if _read(self.config_path) != previous:
                    raise ConfigurationError("Configuration changed; reload before saving")
                os.replace(temporary, self.config_path)
            _sync_directory(self.config_path.parent)
        return {"revision": hashlib.sha256(content).hexdigest(), "backup": str(backup)}

    def setup(self, configuration: str, bindings: str) -> BackupService:
        """Enroll new files only; never replace an existing host declaration."""
        config_content, binding_content = _document(configuration), _document(bindings)
        parsed = Configuration.parse(strict_json(configuration))
        if self.config_path.exists() or self.config_path.is_symlink():
            raise ConfigurationError("Setup requires a new configuration path")
        if self.bindings_path.exists() or self.bindings_path.is_symlink():
            raise ConfigurationError("Setup requires a new bindings path")
        with _staged(self.config_path, config_content) as config_temp:
            with _staged(self.bindings_path, binding_content) as bindings_temp:
                host = HostBindings.load(bindings_temp)
                service = BackupService(parsed, host)
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
        return service
