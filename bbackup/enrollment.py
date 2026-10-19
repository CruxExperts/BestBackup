"""Private, exact host enrollment for version-three policy operations."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .models import Configuration, ConfigurationError, HostBindings, strict_json


def canonical_digest(content: bytes) -> str:
    try:
        document = strict_json(content.decode("utf-8"))
    except UnicodeError:
        raise ConfigurationError("Invalid UTF-8 configuration") from None
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def machine_identity() -> tuple[str, int]:
    try:
        value = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise ConfigurationError("Machine identity unavailable; enrollment refused") from None
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value) or set(value.lower()) == {"0"}:
        raise ConfigurationError("Machine identity unavailable; enrollment refused")
    return hashlib.sha256(value.lower().encode("ascii")).hexdigest(), os.geteuid()


def _read_regular(path: Path, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("Enrollment inputs must be regular files")
        if private and (metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid()):
            raise ConfigurationError("Private bindings or enrollment have unsafe permissions")
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ConfigurationError("Enrollment input exceeds 1 MiB")
    return content


def read_pair(config_path: Path, bindings_path: Path) -> tuple[bytes, bytes]:
    """Read one bounded policy/binding generation without following links."""
    return _read_regular(config_path), _read_regular(bindings_path, private=True)


def parse_pair(policy: bytes, bindings: bytes) -> tuple[Configuration, HostBindings]:
    try:
        configuration = Configuration.parse(strict_json(policy.decode("utf-8")))
        host = HostBindings.parse(strict_json(bindings.decode("utf-8")), configuration)
    except UnicodeError:
        raise ConfigurationError("Invalid UTF-8 configuration") from None
    return configuration, host


def evidence_bytes(policy: bytes, bindings: bytes) -> dict[str, object]:
    machine, uid = machine_identity()
    return {"schema_version": 1, "machine_id_digest": machine, "euid": uid,
            "policy_digest": canonical_digest(policy),
            "bindings_digest": canonical_digest(bindings)}


def evidence(config_path: Path, bindings_path: Path) -> dict[str, object]:
    return evidence_bytes(*read_pair(config_path, bindings_path))


def verify_bytes(state: Path, policy: bytes, bindings: bytes) -> None:
    path = state / "enrollment.json"
    try:
        metadata = path.stat()
        if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
            raise ConfigurationError("Enrollment record must be private")
        record = strict_json(_read_regular(path, private=True).decode("utf-8"))
    except (OSError, UnicodeError, ConfigurationError):
        raise ConfigurationError("Host enrollment missing or invalid; run service enroll") from None
    if not isinstance(record, dict) or record != evidence_bytes(policy, bindings):
        raise ConfigurationError("Host enrollment differs from policy, bindings, machine or user; run service enroll")


def verify(state: Path, config_path: Path, bindings_path: Path) -> None:
    verify_bytes(state, *read_pair(config_path, bindings_path))


def write_bytes(state: Path, policy: bytes, bindings: bytes) -> None:
    """Commit a receipt only for already validated candidate bytes."""
    record = evidence_bytes(policy, bindings)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_mode & 0o077 or state.stat().st_uid != os.geteuid():
        raise ConfigurationError("State directory must be private")
    descriptor, name = tempfile.mkstemp(prefix=".enrollment-", dir=state)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state / "enrollment.json")
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write(state: Path, config_path: Path, bindings_path: Path) -> None:
    write_bytes(state, *read_pair(config_path, bindings_path))


@contextmanager
def policy_lease(state: Path, *, exclusive: bool = False, create: bool = False):
    """Hold one shared operation or exclusive policy edit lease."""
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags |= os.O_CREAT
    elif not state.is_dir():
        raise ConfigurationError("Host enrollment missing; run service enroll")
    if (state.is_symlink() or not state.is_dir() or state.stat().st_mode & 0o077
            or state.stat().st_uid != os.geteuid()):
        raise ConfigurationError("State directory must be private and not a symbolic link")
    try:
        descriptor = os.open(state / "configuration.lock", flags, 0o600)
    except FileNotFoundError:
        raise ConfigurationError("Host enrollment missing; run service enroll") from None
    with os.fdopen(descriptor, "rb+") as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ConfigurationError("Configuration lease must be a private regular file")
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError:
            raise ConfigurationError("Configuration busy; wait for active operations") from None
        yield
