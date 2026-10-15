"""Local Docker capture and new-resource restore primitives for version two.

The runtime deliberately exposes a small subset of Docker's deployment model.
Unsupported storage and host features fail closed instead of producing a partial
application backup. Docker metadata can contain application secrets; callers must
keep the generated metadata file inside the encrypted restic capture.
"""

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
import time
from typing import Any, Mapping, Sequence

from .models import strict_json
from .operations import OperationPlan, OperationStatus


_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_JOURNAL_NAME = "docker-restart-intentions.json"
_JOURNAL_SCHEMA = 1
_METADATA_SCHEMA = 2


class DockerRecoveryError(RuntimeError):
    """A Docker operation was refused or failed safely."""


class DockerRecoveryUncertainError(DockerRecoveryError):
    """A Docker mutation may have happened; inspect the exact resource first."""



def _container_name(value: Any) -> str:
    if not isinstance(value, str) or not _CONTAINER_NAME.fullmatch(value):
        raise DockerRecoveryError("Docker container name is invalid")
    return value


def _container_paths(value: Any, description: str) -> list[str]:
    if not isinstance(value, list):
        raise DockerRecoveryError(f"{description} must be an array of absolute paths")
    paths = []
    for path in value:
        if (not isinstance(path, str) or not path.startswith("/") or "\0" in path
                or ".." in Path(path).parts or str(Path(path)) != path):
            raise DockerRecoveryError(f"{description} contains an invalid container path")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise DockerRecoveryError(f"{description} contains duplicate container paths")
    return paths


def _has_uncaptured_writable_layer_changes(changes: Any, mounts: Sequence[Mapping[str, Any]]) -> bool:
    if not isinstance(changes, list):
        return True
    destinations = {mount["destination"] for mount in mounts}
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {"Path", "Kind"}
                or type(change.get("Kind")) is not int or change["Kind"] != 1
                or not isinstance(change.get("Path"), str) or change["Path"] not in destinations):
            return True
    return False


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _resolved(path: Path, description: str, *, must_exist: bool) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise DockerRecoveryError(f"{description} must be an absolute local path")
    try:
        return path.resolve(strict=must_exist)
    except (OSError, RuntimeError, ValueError):
        raise DockerRecoveryError(f"{description} is unavailable") from None


def _binding_socket(binding: Any) -> Path:
    value = getattr(binding, "socket", None)
    if value is None and isinstance(binding, Mapping):
        value = binding.get("socket")
    if not isinstance(value, (str, os.PathLike)):
        raise DockerRecoveryError("Docker connection requires a local Unix socket")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DockerRecoveryError("Docker socket must be an absolute local path")
    try:
        info = path.lstat()
    except OSError:
        raise DockerRecoveryError("Docker socket is unavailable") from None
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerRecoveryError("Docker connection must be a local Unix socket")
    return path.resolve(strict=True)


def _json_file(path: Path, value: Any) -> None:
    """Write private JSON with file and directory durability."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".docker-metadata-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _private_json(path: Path, description: str, *, maximum: int = 2 * 1024 * 1024) -> Any:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > maximum:
            raise DockerRecoveryError(f"{description} must be a private regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077 or opened.st_size > maximum:
                raise DockerRecoveryError(f"{description} must be a private regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                value = strict_json(stream.read(maximum + 1))
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return value
    except DockerRecoveryError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise DockerRecoveryError(f"Could not securely read {description}") from None


class DockerRuntime:
    """Manage strict local-Docker capture, restart recovery, and new restores."""

    def __init__(
        self,
        state_dir: Path,
        bindings: Mapping[str, Any],
        *,
        client_factory=None,
        stop_timeout: int = 15,
        request_timeout: float = 20.0,
        recovery_timeout: float = 60.0,
        startup_timeout: float = 120.0,
        poll_interval: float = 0.25,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.bindings = bindings
        self.client_factory = client_factory
        self.stop_timeout = stop_timeout
        self.request_timeout = request_timeout
        self.recovery_timeout = recovery_timeout
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.journal_path = self.state_dir / _JOURNAL_NAME
        self.lock_dir = self.state_dir / "docker-locks"
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.lock_dir, 0o700)

    def _socket(self, connection: str) -> Path:
        if not isinstance(connection, str) or connection not in self.bindings:
            raise DockerRecoveryError("Unknown Docker connection")
        return _binding_socket(self.bindings[connection])

    def _client(self, connection: str):
        socket_path = self._socket(connection)
        if self.client_factory is not None:
            return self.client_factory(connection, socket_path, self.request_timeout)
        try:
            import docker

            return docker.DockerClient(
                base_url=f"unix://{socket_path}",
                version="auto",
                timeout=self.request_timeout,
            )
        except Exception:
            raise DockerRecoveryError("Could not connect to the configured local Docker daemon") from None

    @staticmethod
    def _daemon_id(client) -> str:
        try:
            daemon_id = client.info().get("ID")
        except Exception:
            raise DockerRecoveryError("Could not verify Docker daemon identity") from None
        if not isinstance(daemon_id, str) or not daemon_id or len(daemon_id) > 256:
            raise DockerRecoveryError("Docker daemon identity is invalid")
        return daemon_id

    def daemon_identity(self, connection: str) -> str:
        client = self._client(connection)
        try:
            return self._daemon_id(client)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def protected_mount_paths(self) -> tuple[Path, ...]:
        """Return every local Docker root and persistent mount path on bound daemons."""
        protected = []
        for connection in self.bindings:
            client = self._client(connection)
            try:
                try:
                    info = client.info()
                    root = info.get("DockerRootDir")
                    if isinstance(root, str) and Path(root).is_absolute():
                        protected.append(_resolved(Path(root), "Docker data root", must_exist=False))
                    containers = client.containers.list(all=True)
                except Exception:
                    raise DockerRecoveryError("Existing Docker resource locations could not be inspected") from None
                for container in containers:
                    try:
                        container.reload()
                        mounts = container.attrs.get("Mounts")
                    except Exception:
                        raise DockerRecoveryError("Existing Docker mount locations could not be inspected") from None
                    if not isinstance(mounts, list):
                        raise DockerRecoveryError("Existing Docker mount inventory is invalid")
                    for mount in mounts:
                        if not isinstance(mount, dict):
                            raise DockerRecoveryError("Existing Docker mount inventory is invalid")
                        if mount.get("Type") in ("bind", "volume"):
                            source = mount.get("Source")
                            if not isinstance(source, str) or not Path(source).is_absolute():
                                raise DockerRecoveryError("Existing Docker mount path is invalid")
                            protected.append(_resolved(Path(source), "Existing Docker mount", must_exist=False))
                        elif mount.get("Type") != "tmpfs":
                            raise DockerRecoveryError("Existing Docker storage type is unsupported")
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return tuple(protected)

    def _resource_lock(self, daemon_id: str, container_id: str) -> int:
        key = hashlib.sha256(f"{daemon_id}\0{container_id}".encode()).hexdigest()
        path = self.lock_dir / f"{key}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in (11, 13):
                raise DockerRecoveryError("A Docker operation already owns this container") from None
            raise
        return descriptor

    @staticmethod
    def _unlock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def _journal_lock(self):
        path = self.lock_dir / "journal.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_journal_unlocked(self) -> list[dict[str, Any]]:
        try:
            info = self.journal_path.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise DockerRecoveryError("Docker recovery journal is unavailable") from None
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
            raise DockerRecoveryError("Docker recovery journal must be a private regular file")
        raw = _private_json(self.journal_path, "Docker recovery journal", maximum=1024 * 1024)
        if (not isinstance(raw, dict) or set(raw) != {"schema_version", "intentions"}
                or type(raw["schema_version"]) is not int or raw["schema_version"] != _JOURNAL_SCHEMA
                or not isinstance(raw["intentions"], list)):
            raise DockerRecoveryError("Docker recovery journal is invalid")
        for item in raw["intentions"]:
            if (not isinstance(item, dict)
                    or set(item) != {"operation_id", "connection", "socket", "daemon_id", "container_id",
                                     "container_name", "phase"}
                    or not all(isinstance(item[key], str) and item[key]
                               for key in ("operation_id", "connection", "socket", "daemon_id",
                                           "container_id", "container_name", "phase"))
                    or not _OPERATION_ID.fullmatch(item["operation_id"])
                    or not _CONTAINER_NAME.fullmatch(item["connection"])
                    or not _CONTAINER_NAME.fullmatch(item["container_name"])
                    or not Path(item["socket"]).is_absolute() or ".." in Path(item["socket"]).parts
                    or len(item["daemon_id"]) > 256
                    or item["phase"] not in ("prepared", "stop-requested", "stopped", "restart-requested")):
                raise DockerRecoveryError("Docker recovery journal is invalid")
        return raw["intentions"]

    def _write_journal_unlocked(self, intentions: Sequence[dict[str, Any]]) -> None:
        if not intentions:
            try:
                self.journal_path.unlink()
            except FileNotFoundError:
                return
            directory = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return
        _json_file(self.journal_path, {"schema_version": _JOURNAL_SCHEMA, "intentions": list(intentions)})

    def _journal_add(self, additions: Sequence[dict[str, Any]]) -> None:
        with self._journal_lock():
            current = self._read_journal_unlocked()
            current_keys = {(item["operation_id"], item["daemon_id"], item["container_id"]) for item in current}
            if any((item["operation_id"], item["daemon_id"], item["container_id"]) in current_keys for item in additions):
                raise DockerRecoveryError("Docker recovery journal already contains this operation")
            current.extend(additions)
            self._write_journal_unlocked(current)

    def _journal_phase(self, operation_id: str, daemon_id: str, container_id: str, phase: str) -> None:
        with self._journal_lock():
            intentions = self._read_journal_unlocked()
            found = False
            for item in intentions:
                if (item["operation_id"], item["daemon_id"], item["container_id"]) == (operation_id, daemon_id, container_id):
                    item["phase"] = phase
                    found = True
            if not found:
                raise DockerRecoveryError("Docker restart intention disappeared")
            self._write_journal_unlocked(intentions)

    def _journal_remove(self, operation_id: str, daemon_id: str, container_id: str) -> None:
        with self._journal_lock():
            intentions = self._read_journal_unlocked()
            remaining = [item for item in intentions if (item["operation_id"], item["daemon_id"], item["container_id"])
                         != (operation_id, daemon_id, container_id)]
            if len(remaining) == len(intentions):
                raise DockerRecoveryError("Docker restart intention disappeared")
            self._write_journal_unlocked(remaining)

    @staticmethod
    def _container(client, identifier: str):
        try:
            container = client.containers.get(identifier)
            container.reload()
        except Exception:
            raise DockerRecoveryError("Docker container identity could not be inspected") from None
        return container

    @staticmethod
    def _container_state(container) -> tuple[str, str]:
        attrs = getattr(container, "attrs", {})
        container_id = attrs.get("Id", getattr(container, "id", None))
        state = attrs.get("State", {}).get("Status")
        if not isinstance(container_id, str) or not container_id or not isinstance(state, str):
            raise DockerRecoveryError("Docker container state could not be verified")
        return container_id, state

    @staticmethod
    def _validate_typed_mounts(attrs: Mapping[str, Any]) -> None:
        host = attrs.get("HostConfig") or {}
        declarations = host.get("Mounts")
        if declarations in (None, []):
            return
        if not isinstance(declarations, list):
            raise DockerRecoveryError("Docker typed mount configuration is invalid")
        actual_mounts = attrs.get("Mounts")
        if not isinstance(actual_mounts, list):
            raise DockerRecoveryError("Docker mount inventory is invalid")
        actual_by_target = {mount["Destination"]: mount for mount in actual_mounts}
        seen_targets = set()
        mount_fields = {
            "Type", "Source", "Target", "ReadOnly", "Consistency",
            "BindOptions", "VolumeOptions", "TmpfsOptions", "ClusterOptions",
        }
        for declaration in declarations:
            if not isinstance(declaration, dict) or set(declaration) - mount_fields:
                raise DockerRecoveryError("Docker typed mount contains unsupported options")
            mount_type = declaration.get("Type")
            source = declaration.get("Source", "")
            target = declaration.get("Target")
            read_only = declaration.get("ReadOnly", False)
            if source is None:
                source = ""
            if (mount_type not in ("bind", "volume") or not isinstance(source, str)
                    or not isinstance(target, str) or not target.startswith("/")
                    or target == "/" or "\0" in target or ".." in Path(target).parts
                    or str(Path(target)) != target or target in seen_targets
                    or type(read_only) is not bool):
                raise DockerRecoveryError("Docker typed mount declaration is invalid")
            seen_targets.add(target)
            actual = actual_by_target.get(target)
            if (actual is None or actual.get("Type") != mount_type
                    or read_only != (not actual.get("RW", True))):
                raise DockerRecoveryError("Docker typed mount does not match its inspected mount")
            consistency = declaration.get("Consistency")
            if consistency not in (None, "", "default", "consistent"):
                raise DockerRecoveryError("Docker mount consistency mode is unsupported")
            if mount_type == "bind":
                if not Path(source).is_absolute() or ".." in Path(source).parts:
                    raise DockerRecoveryError("Docker typed bind source is invalid")
                actual_source = actual.get("Source")
                if (not isinstance(actual_source, str)
                        or str(_resolved(Path(source), "Docker typed bind source", must_exist=True))
                        != str(_resolved(Path(actual_source), "Docker persistent mount", must_exist=True))):
                    raise DockerRecoveryError("Docker typed bind source does not match its inspected mount")
                bind_options = declaration.get("BindOptions")
                if bind_options not in (None, {}):
                    if (not isinstance(bind_options, dict)
                            or set(bind_options) - {
                                "Propagation", "NonRecursive", "CreateMountpoint",
                                "ReadOnlyNonRecursive", "ReadOnlyForceRecursive",
                            }
                            or bind_options.get("Propagation", "rprivate") not in (None, "", "rprivate")
                            or any(
                                type(bind_options.get(key, False)) is not bool or bind_options.get(key, False)
                                for key in (
                                    "NonRecursive", "CreateMountpoint",
                                    "ReadOnlyNonRecursive", "ReadOnlyForceRecursive",
                                )
                            )):
                        raise DockerRecoveryError("Docker typed bind options are unsupported")
                if declaration.get("VolumeOptions") not in (None, {}):
                    raise DockerRecoveryError("Docker typed bind has volume-only options")
            else:
                if source not in ("", actual.get("Name")):
                    raise DockerRecoveryError("Docker typed volume name does not match its inspected mount")
                if declaration.get("BindOptions") not in (None, {}):
                    raise DockerRecoveryError("Docker typed volume has bind-only options")
                volume_options = declaration.get("VolumeOptions")
                if volume_options not in (None, {}):
                    allowed_volume_fields = {"NoCopy", "Labels", "DriverConfig", "Subpath"}
                    if not isinstance(volume_options, dict) or set(volume_options) - allowed_volume_fields:
                        raise DockerRecoveryError("Docker typed volume options are unsupported")
                    no_copy = volume_options.get("NoCopy", False)
                    labels = volume_options.get("Labels")
                    driver = volume_options.get("DriverConfig")
                    subpath = volume_options.get("Subpath")
                    if (type(no_copy) is not bool or no_copy or labels not in (None, {})
                            or subpath not in (None, "")):
                        raise DockerRecoveryError("Docker typed volume options are unsupported")
                    if driver not in (None, {}):
                        if (not isinstance(driver, dict) or set(driver) - {"Name", "Options"}
                                or driver.get("Name") not in ("", "local")
                                or driver.get("Options") not in (None, {})):
                            raise DockerRecoveryError("Only default local Docker volume options are supported")
            if declaration.get("TmpfsOptions") not in (None, {}) or declaration.get("ClusterOptions") not in (None, {}):
                raise DockerRecoveryError("Docker typed mount options are unsupported")

    def _mounts(self, client, attrs: Mapping[str, Any], protected_paths: Sequence[Path]) -> list[dict[str, Any]]:
        mounts = attrs.get("Mounts")
        if not isinstance(mounts, list):
            raise DockerRecoveryError("Docker mount inventory is invalid")
        config_volumes = (attrs.get("Config") or {}).get("Volumes") or {}
        if not isinstance(config_volumes, dict):
            raise DockerRecoveryError("Docker declared volume inventory is invalid")
        destinations = set()
        captured = []
        captured_sources = set()
        for mount in mounts:
            if not isinstance(mount, dict):
                raise DockerRecoveryError("Docker mount inventory is invalid")
            mount_type = mount.get("Type")
            destination = mount.get("Destination")
            source = mount.get("Source")
            if (mount_type not in ("bind", "volume") or not isinstance(destination, str)
                    or not Path(destination).is_absolute() or destination in ("", "/")
                    or ".." in Path(destination).parts or str(Path(destination)) != destination
                    or destination in destinations or not isinstance(source, str)
                    or not Path(source).is_absolute() or type(mount.get("RW", True)) is not bool):
                raise DockerRecoveryError("Docker contains an unsupported or ambiguous persistent mount")
            destinations.add(destination)
            if mount_type == "volume":
                volume_name = mount.get("Name")
                try:
                    volume = client.volumes.get(volume_name)
                    volume.reload()
                    volume_attrs = volume.attrs
                except Exception:
                    raise DockerRecoveryError("Docker local volume could not be inspected") from None
                if (not isinstance(volume_attrs, dict) or volume_attrs.get("Driver") != "local"
                        or volume_attrs.get("Scope") != "local" or volume_attrs.get("Options") not in (None, {})):
                    raise DockerRecoveryError("Only local Docker volumes without driver options are supported")
            elif mount.get("Propagation") not in (None, "", "rprivate"):
                raise DockerRecoveryError("Docker bind propagation mode is not supported")
            local_path = _resolved(Path(source), "Docker persistent mount", must_exist=True)
            if local_path == Path("/"):
                raise DockerRecoveryError("A Docker root filesystem mount cannot be captured")
            try:
                info = local_path.stat()
            except OSError:
                raise DockerRecoveryError("Docker persistent mount is inaccessible") from None
            if mount_type == "volume" and not stat.S_ISDIR(info.st_mode):
                raise DockerRecoveryError("Docker local volume data must be a directory")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise DockerRecoveryError("Docker persistent mount must be a regular file or directory")
            if any(_overlap(local_path, protected) for protected in protected_paths):
                raise DockerRecoveryError("Docker persistent mount overlaps protected repository or runtime data")
            if str(local_path) in captured_sources:
                raise DockerRecoveryError("Docker mount sources cannot be duplicated")
            captured_sources.add(str(local_path))
            captured.append({
                "type": mount_type,
                "archive_source": str(local_path),
                "destination": destination,
                "read_only": not bool(mount.get("RW", True)),
                "source_kind": "bind" if mount_type == "bind" else "local-volume",
            })
        if set(config_volumes) - destinations:
            raise DockerRecoveryError("A declared Docker volume is not represented by a captured mount")
        if not captured and config_volumes:
            raise DockerRecoveryError("Docker declares persistent storage without an inspectable mount")
        bind_sources = {}
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            mount_type = mount.get("Type")
            destination = mount.get("Destination")
            source = mount.get("Source")
            if isinstance(destination, str) and isinstance(source, str):
                bind_sources[destination] = (
                    mount_type,
                    str(_resolved(Path(source), "Docker persistent mount", must_exist=True)),
                    mount.get("Name"),
                    not bool(mount.get("RW", True)),
                )
        binds = (attrs.get("HostConfig") or {}).get("Binds") or []
        if not isinstance(binds, list):
            raise DockerRecoveryError("Docker bind configuration is invalid")
        for bind in binds:
            if not isinstance(bind, str):
                raise DockerRecoveryError("Docker bind configuration is invalid")
            pieces = bind.split(":")
            if len(pieces) == 1:
                source, destination, options = "", pieces[0], set()
            elif len(pieces) in (2, 3):
                source, destination = pieces[:2]
                options = set(pieces[2].split(",")) if len(pieces) == 3 else set()
            else:
                raise DockerRecoveryError("Docker bind options are unsupported")
            if (destination not in bind_sources or options - {"", "ro", "rw", "rprivate"}
                    or ("ro" in options and "rw" in options)):
                raise DockerRecoveryError("Docker bind configuration is unsupported")
            mount_type, actual_source, volume_name, read_only = bind_sources[destination]
            if read_only != ("ro" in options):
                raise DockerRecoveryError("Docker bind read-only mode does not match its inspected mount")
            if mount_type == "volume":
                if source not in ("", volume_name):
                    raise DockerRecoveryError("Docker named-volume binding does not match its inspected mount")
            elif not Path(source).is_absolute() or str(_resolved(
                    Path(source), "Docker bind source", must_exist=True)) != actual_source:
                raise DockerRecoveryError("Docker bind source does not match its inspected mount")
        self._validate_typed_mounts(attrs)
        return captured

    @staticmethod
    def _assert_no_shared_running_mounts(client, selected: Sequence[dict[str, Any]]) -> None:
        """Refuse an inconsistent capture when an unselected live container shares data."""
        selected_ids = {item["container_id"] for item in selected}
        selected_paths = {
            Path(mount["archive_source"])
            for item in selected for mount in item["mounts"]
        }
        try:
            containers = client.containers.list(all=True)
        except Exception:
            raise DockerRecoveryError("Docker container mount sharing could not be inspected") from None
        if not isinstance(containers, list):
            raise DockerRecoveryError("Docker container inventory is invalid")
        for container in containers:
            try:
                container.reload()
                container_id, state = DockerRuntime._container_state(container)
                mounts = container.attrs.get("Mounts")
            except Exception:
                raise DockerRecoveryError("Docker container mount sharing could not be inspected") from None
            if container_id in selected_ids or state != "running":
                continue
            if not isinstance(mounts, list):
                raise DockerRecoveryError("Docker container mount inventory is invalid")
            for mount in mounts:
                if not isinstance(mount, dict):
                    raise DockerRecoveryError("Docker container mount inventory is invalid")
                mount_type = mount.get("Type")
                if mount_type == "tmpfs":
                    continue
                source = mount.get("Source")
                if (mount_type not in ("bind", "volume") or not isinstance(source, str)
                        or not Path(source).is_absolute()):
                    raise DockerRecoveryError("Docker container uses unsupported shared storage")
                other_path = _resolved(Path(source), "Docker persistent mount", must_exist=True)
                if any(_overlap(other_path, path) for path in selected_paths):
                    raise DockerRecoveryError(
                        "Docker data is shared with a running container not selected for quiescence"
                    )

    @staticmethod
    def _supported_config(attrs: Mapping[str, Any]) -> dict[str, Any]:
        config = attrs.get("Config")
        host = attrs.get("HostConfig")
        if not isinstance(config, dict) or not isinstance(host, dict):
            raise DockerRecoveryError("Docker deployment metadata is incomplete")
        if config.get("Image") is None:
            raise DockerRecoveryError("Docker image identity is missing")
        image_id = attrs.get("Image")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise DockerRecoveryError("Docker container image is not pinned by content identity")
        known_host_fields = {
            "Binds", "ContainerIDFile", "LogConfig", "NetworkMode", "PortBindings",
            "RestartPolicy", "AutoRemove", "VolumeDriver", "VolumesFrom", "ConsoleSize",
            "CapAdd", "CapDrop", "CgroupnsMode", "Dns", "DnsOptions", "DnsSearch",
            "ExtraHosts", "GroupAdd", "IpcMode", "Cgroup", "Links", "OomScoreAdj",
            "PidMode", "Privileged", "PublishAllPorts", "ReadonlyRootfs", "SecurityOpt",
            "StorageOpt", "UTSMode", "UsernsMode", "ShmSize", "Runtime", "Isolation",
            "CpuShares", "Memory", "NanoCpus", "CgroupParent", "BlkioWeight",
            "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
            "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "CpuPeriod", "CpuQuota",
            "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus", "CpusetMems",
            "Devices", "DeviceCgroupRules", "DeviceRequests", "KernelMemory",
            "KernelMemoryTCP", "MemoryReservation", "MemorySwap", "MemorySwappiness",
            "OomKillDisable", "PidsLimit", "Ulimits", "CpuCount", "CpuPercent",
            "IOMaximumIOps", "IOMaximumBandwidth", "Mounts", "Tmpfs", "Sysctls",
            "Init", "MaskedPaths", "ReadonlyPaths",
        }
        if set(host) - known_host_fields:
            raise DockerRecoveryError("Docker host configuration contains unsupported fields")
        unsupported_truthy = ("Privileged", "PublishAllPorts", "AutoRemove")
        if any(host.get(key) for key in unsupported_truthy):
            raise DockerRecoveryError("Docker host configuration uses an unsupported feature")
        unsupported_nonempty = (
            "CapAdd", "CapDrop", "Devices", "DeviceCgroupRules", "DeviceRequests",
            "GroupAdd", "Links", "VolumesFrom", "PortBindings", "Tmpfs",
            "Sysctls", "ExtraHosts", "Dns", "DnsSearch", "DnsOptions", "SecurityOpt",
            "StorageOpt", "Ulimits", "BlkioWeightDevice", "BlkioDeviceReadBps",
            "BlkioDeviceWriteBps", "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps",
        )
        for key in unsupported_nonempty:
            value = host.get(key)
            if value not in (None, {}, [], ()):
                raise DockerRecoveryError("Docker host configuration uses an unsupported feature")
        default_values = {
            "ContainerIDFile": "", "Cgroup": "", "CgroupParent": "",
            "VolumeDriver": "", "Isolation": "", "CpusetCpus": "", "CpusetMems": "",
            "OomScoreAdj": 0, "Memory": 0, "MemoryReservation": 0, "MemorySwap": 0,
            "CpuShares": 0, "NanoCpus": 0, "CpuPeriod": 0, "CpuQuota": 0,
            "CpuRealtimePeriod": 0, "CpuRealtimeRuntime": 0, "BlkioWeight": 0,
            "CpuCount": 0, "CpuPercent": 0, "IOMaximumIOps": 0,
            "IOMaximumBandwidth": 0, "KernelMemory": 0, "KernelMemoryTCP": 0,
            "MemorySwappiness": None, "OomKillDisable": None, "PidsLimit": None,
            "Init": None, "ShmSize": 64 * 1024 * 1024, "ConsoleSize": [0, 0],
        }
        for key, default in default_values.items():
            value = host.get(key, default)
            allowed = (
                (default, 0, False, None, -1) if key == "MemorySwappiness"
                else (default, 0, False, None) if key in ("OomKillDisable", "PidsLimit", "Init")
                else (default,)
            )
            if value not in allowed or (key in ("Init", "OomKillDisable") and value not in (None, False)):
                raise DockerRecoveryError("Docker host resource configuration is unsupported")
        log_config = host.get("LogConfig")
        if log_config not in (None, {}, {"Type": "json-file", "Config": {}}):
            raise DockerRecoveryError("Docker logging driver configuration is unsupported")
        if host.get("NetworkMode", "default") not in ("default", "bridge"):
            raise DockerRecoveryError("Only Docker's built-in bridge network is supported")
        expected_defaults = {
            "PidMode": "", "IpcMode": "private", "UTSMode": "", "UsernsMode": "",
            "CgroupnsMode": "private", "Runtime": "runc",
        }
        for key, default in expected_defaults.items():
            value = host.get(key, default)
            if value not in (None, "", default):
                raise DockerRecoveryError("Docker host namespace or runtime configuration is unsupported")
        readonly_rootfs = host.get("ReadonlyRootfs", False)
        if type(readonly_rootfs) is not bool:
            raise DockerRecoveryError("Docker root filesystem mode is invalid")
        networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
        if not isinstance(networks, dict) or set(networks) - {"bridge"}:
            raise DockerRecoveryError("Only Docker's built-in bridge network is supported")
        for network in networks.values():
            if not isinstance(network, dict) or network.get("IPAMConfig") is not None or network.get("Aliases"):
                raise DockerRecoveryError("Custom Docker network aliases or addresses are unsupported")
        restart = host.get("RestartPolicy") or {"Name": "no", "MaximumRetryCount": 0}
        if (not isinstance(restart, dict) or restart.get("Name") not in ("no", "always", "unless-stopped", "on-failure")
                or type(restart.get("MaximumRetryCount", 0)) is not int or restart.get("MaximumRetryCount", 0) < 0):
            raise DockerRecoveryError("Docker restart policy is invalid")
        env = config.get("Env") or []
        labels = config.get("Labels") or {}
        exposed = config.get("ExposedPorts") or {}
        if (not isinstance(env, list) or not isinstance(labels, dict)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items())
                or not isinstance(exposed, dict)):
            raise DockerRecoveryError("Docker application configuration is invalid")
        for value in env:
            if not isinstance(value, str) or "\0" in value:
                raise DockerRecoveryError("Docker environment configuration is invalid")
        if any(not isinstance(key, str) for key in exposed):
            raise DockerRecoveryError("Docker exposed-port configuration is invalid")
        for key in ("Cmd", "Entrypoint"):
            value = config.get(key)
            if value is not None and (not isinstance(value, list) or any(not isinstance(part, str) for part in value)):
                raise DockerRecoveryError("Docker command configuration is unsupported")
        for key in ("User", "WorkingDir"):
            if config.get(key) is not None and not isinstance(config.get(key), str):
                raise DockerRecoveryError("Docker application configuration is invalid")
        for key in ("Hostname", "Domainname", "StopSignal"):
            if config.get(key) is not None and not isinstance(config.get(key), str):
                raise DockerRecoveryError("Docker application configuration is invalid")
        for key in ("Tty", "OpenStdin", "StdinOnce"):
            if type(config.get(key, False)) is not bool:
                raise DockerRecoveryError("Docker application configuration is invalid")
        if config.get("StdinOnce", False):
            raise DockerRecoveryError("Docker StdinOnce behavior is unsupported")
        if host.get("PortBindings") not in (None, {}, []):
            raise DockerRecoveryError("Published Docker ports are unsupported")
        masked_paths = _container_paths(host.get("MaskedPaths"), "Docker masked paths")
        readonly_paths = _container_paths(host.get("ReadonlyPaths"), "Docker read-only paths")
        return {
            "image_id": image_id,
            "config": {
                "env": env,
                "cmd": config.get("Cmd"),
                "entrypoint": config.get("Entrypoint"),
                "user": config.get("User"),
                "working_dir": config.get("WorkingDir"),
                "labels": labels,
                "exposed_ports": exposed,
                "tty": config.get("Tty", False),
                "open_stdin": config.get("OpenStdin", False),
                "stdin_once": config.get("StdinOnce", False),
                "healthcheck": config.get("Healthcheck"),
                "hostname": config.get("Hostname"),
                "domainname": config.get("Domainname"),
                "stop_signal": config.get("StopSignal"),
            },
            "host": {
                "network_mode": host.get("NetworkMode", "default"),
                "restart_policy": {
                    "Name": restart.get("Name", "no"),
                    "MaximumRetryCount": restart.get("MaximumRetryCount", 0),
                },
                "readonly_rootfs": readonly_rootfs,
                "masked_paths": masked_paths,
                "readonly_paths": readonly_paths,
            },
        }

    @contextmanager
    def capture(
        self,
        sources: Sequence[Any],
        private_dir: Path,
        *,
        protected_paths: Sequence[Path],
        operation_id: str,
        cancel=None,
    ):
        """Quiesce selected containers, yield their complete data and metadata paths.

        Every mount and deployment field is validated before any container is
        stopped. Journal intentions are durable before the first stop request.
        Only running-at-entry resources owned by this operation are restarted.
        """
        if not _OPERATION_ID.fullmatch(operation_id):
            raise DockerRecoveryError("Docker capture operation identity is invalid")
        if cancel is not None and cancel.is_set():
            raise DockerRecoveryError("Docker capture cancelled before dispatch")
        if not sources:
            yield (), ()
            return
        private_dir = Path(private_dir)
        protected = tuple(_resolved(Path(path), "Protected Docker path", must_exist=False) for path in protected_paths)
        clients: dict[str, Any] = {}
        locks: list[int] = []
        selected: list[dict[str, Any]] = []
        try:
            for source in sources:
                if getattr(source, "stop_policy", None) != "stop-restart":
                    raise DockerRecoveryError("Docker sources require stop_policy='stop-restart'")
                connection = getattr(source, "connection", None)
                container_name = _container_name(getattr(source, "container", None))
                if not isinstance(connection, str) or connection not in self.bindings:
                    raise DockerRecoveryError("Docker source requires a private connection binding")
                if connection not in clients:
                    clients[connection] = self._client(connection)
                client = clients[connection]
                daemon_id = self._daemon_id(client)
                container = self._container(client, container_name)
                container_id, state = self._container_state(container)
                attrs = container.attrs
                if attrs.get("Name", "").lstrip("/") != container_name or attrs.get("Id") != container_id:
                    raise DockerRecoveryError("Docker container name and exact identity do not match")
                if state not in ("running", "created", "exited", "paused", "restarting"):
                    raise DockerRecoveryError("Docker container state is unsupported")
                deployment = self._supported_config(attrs)
                mounts = self._mounts(client, attrs, protected)
                try:
                    writable_layer_changes = container.diff()
                except Exception:
                    raise DockerRecoveryError("Docker writable-layer changes could not be inspected") from None
                if _has_uncaptured_writable_layer_changes(writable_layer_changes, mounts):
                    raise DockerRecoveryError(
                        "Docker container has writable-layer changes outside persistent mounts"
                    )
                if state == "paused" or state == "restarting":
                    raise DockerRecoveryError("Paused or restarting Docker containers cannot be captured")
                item = {
                    "connection": connection,
                    "socket": str(self._socket(connection)),
                    "daemon_id": daemon_id,
                    "container_id": container_id,
                    "container_name": container_name,
                    "state": state,
                    "container": container,
                    "client": client,
                    "image_id": deployment["image_id"],
                    "mounts": mounts,
                    "deployment": deployment,
                    "healthcheck_configured": bool(deployment["config"].get("healthcheck")),
                }
                try:
                    image_object = client.images.get(deployment["image_id"])
                    image_config = image_object.attrs.get("Config") or {}
                except Exception:
                    raise DockerRecoveryError("Pinned Docker image configuration could not be inspected") from None
                if (image_config.get("Healthcheck") != deployment["config"].get("healthcheck")
                        or (image_config.get("ExposedPorts") or {}) != deployment["config"].get("exposed_ports")):
                    raise DockerRecoveryError("Container overrides image health or exposed-port configuration")
                selected.append(item)
            identities = [(item["daemon_id"], item["container_id"]) for item in selected]
            if len(set(identities)) != len(identities):
                raise DockerRecoveryError("A Docker container is selected more than once")
            for item in sorted(selected, key=lambda entry: (entry["daemon_id"], entry["container_id"])):
                locks.append(self._resource_lock(item["daemon_id"], item["container_id"]))
            for daemon_id in sorted({item["daemon_id"] for item in selected}):
                daemon_items = [item for item in selected if item["daemon_id"] == daemon_id]
                self._assert_no_shared_running_mounts(daemon_items[0]["client"], daemon_items)
            with self._journal_lock():
                pending = self._read_journal_unlocked()
            pending_keys = {(entry["daemon_id"], entry["container_id"]) for entry in pending}
            if any(identity in pending_keys for identity in identities):
                raise DockerRecoveryError("An unresolved Docker restart intention requires reviewed recovery")
            running = [item for item in selected if item["state"] == "running"]

            paths: list[str] = []
            exports: list[dict[str, Any]] = []
            private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for index, item in enumerate(selected):
                paths.extend(mount["archive_source"] for mount in item["mounts"])
                metadata_path = private_dir / f"bbackup-docker-{getattr(sources[index], 'name')}.json"
                manifest = {
                    "schema_version": _METADATA_SCHEMA,
                    "kind": "docker-container",
                    "source": getattr(sources[index], "name"),
                    "container_name": item["container_name"],
                    "container_id": item["container_id"],
                    "daemon_id": item["daemon_id"],
                    "image_id": item["image_id"],
                    "config": item["deployment"]["config"],
                    "host": item["deployment"]["host"],
                    "healthcheck_configured": item["healthcheck_configured"],
                    "mounts": item["mounts"],
                }
                _json_file(metadata_path, manifest)
                paths.append(str(metadata_path))
                item["metadata_path"] = metadata_path
                item["manifest"] = manifest
                exports.append({"source": getattr(sources[index], "name"), "kind": "docker",
                                "format": "docker-container-v1", "path": str(metadata_path)})
            attempted: list[dict[str, Any]] = []
            try:
                for item in running:
                    if cancel is not None and cancel.is_set():
                        raise DockerRecoveryError("Docker capture cancelled before stop")
                    intention = {
                        "operation_id": operation_id,
                        "connection": item["connection"],
                        "socket": item["socket"],
                        "daemon_id": item["daemon_id"],
                        "container_id": item["container_id"],
                        "container_name": item["container_name"],
                        "phase": "prepared",
                    }
                    self._journal_add((intention,))
                    current = self._container(item["client"], item["container_id"])
                    exact_id, current_state = self._container_state(current)
                    if exact_id != item["container_id"]:
                        raise DockerRecoveryUncertainError(
                            "Docker container identity changed before stop; review docker recover"
                        )
                    if current_state != "running":
                        self._journal_remove(operation_id, item["daemon_id"], item["container_id"])
                        continue
                    self._journal_phase(operation_id, item["daemon_id"], item["container_id"], "stop-requested")
                    attempted.append(item)
                    try:
                        current.stop(timeout=self.stop_timeout)
                    except Exception:
                        raise DockerRecoveryUncertainError("Docker stop outcome is uncertain; review docker recover") from None
                    current = self._container(item["client"], item["container_id"])
                    exact_id, current_state = self._container_state(current)
                    if exact_id != item["container_id"] or current_state not in ("exited", "created"):
                        raise DockerRecoveryUncertainError("Docker stop state is uncertain; review docker recover")
                    try:
                        stopped_layer_changes = current.diff()
                    except Exception:
                        raise DockerRecoveryError(
                            "Docker writable-layer changes could not be rechecked after stop"
                        ) from None
                    if _has_uncaptured_writable_layer_changes(stopped_layer_changes, item["mounts"]):
                        raise DockerRecoveryError(
                            "Docker container writable-layer changes occurred while stopping"
                        )
                    self._journal_phase(operation_id, item["daemon_id"], item["container_id"], "stopped")
                yield tuple(paths), tuple(exports)
            finally:
                failures = []
                for item in reversed(attempted):
                    try:
                        self._recover_intention(
                            item["connection"], item["socket"], item["daemon_id"],
                            item["container_id"], operation_id,
                        )
                    except DockerRecoveryError:
                        failures.append(item["container_id"])
                if failures:
                    raise DockerRecoveryUncertainError(
                        "Docker capture did not restore every originally running container; review docker recover"
                    )
        finally:
            for descriptor in reversed(locks):
                self._unlock(descriptor)
            for client in clients.values():
                try:
                    client.close()
                except Exception:
                    pass

    def _recover_intention(
        self, connection: str, expected_socket: str, daemon_id: str,
        container_id: str, operation_id: str, *, reviewed: bool = False,
    ) -> None:
        client = self._client(connection)
        try:
            if str(self._socket(connection)) != expected_socket or self._daemon_id(client) != daemon_id:
                raise DockerRecoveryUncertainError("Docker daemon identity changed; restart intention retained")
            try:
                container = self._container(client, container_id)
            except DockerRecoveryError:
                raise DockerRecoveryUncertainError("Exact Docker container is unavailable; restart intention retained") from None
            actual_id, state = self._container_state(container)
            if actual_id != container_id:
                raise DockerRecoveryUncertainError("Docker container identity changed; restart intention retained")
            if state == "running" and not reviewed:
                raise DockerRecoveryUncertainError(
                    "Docker container was already running before capture restart; restart intention retained"
                )
            if state != "running":
                self._journal_phase(operation_id, daemon_id, container_id, "restart-requested")
                try:
                    container.start()
                except Exception:
                    raise DockerRecoveryUncertainError("Docker restart outcome is uncertain; restart intention retained") from None
                deadline = time.monotonic() + self.recovery_timeout
                while True:
                    container = self._container(client, container_id)
                    actual_id, state = self._container_state(container)
                    if actual_id != container_id:
                        raise DockerRecoveryUncertainError("Docker container identity changed; restart intention retained")
                    if state == "running":
                        break
                    if time.monotonic() >= deadline:
                        raise DockerRecoveryUncertainError("Docker restart did not reach running state; intention retained")
                    time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
            self._journal_remove(operation_id, daemon_id, container_id)
        finally:
            try:
                client.close()
            except Exception:
                pass
    def _resolve_prepared_intention(
        self, connection: str, expected_socket: str, daemon_id: str,
        container_id: str, operation_id: str,
    ) -> None:
        """Discard a pre-stop intention only when the exact container is running.

        A prepared record means no stop request was issued. Starting a now-stopped
        container could therefore affect a workload this capture never stopped.
        """
        client = self._client(connection)
        try:
            if str(self._socket(connection)) != expected_socket or self._daemon_id(client) != daemon_id:
                raise DockerRecoveryUncertainError("Docker daemon identity changed; restart intention retained")
            try:
                container = self._container(client, container_id)
            except DockerRecoveryError:
                raise DockerRecoveryUncertainError("Exact Docker container is unavailable; restart intention retained") from None
            actual_id, state = self._container_state(container)
            if actual_id != container_id:
                raise DockerRecoveryUncertainError("Docker container identity changed; restart intention retained")
            if state != "running":
                raise DockerRecoveryUncertainError(
                    "Docker stop was not issued; stopped container requires manual review and intention remains"
                )
            self._journal_remove(operation_id, daemon_id, container_id)
        finally:
            try:
                client.close()
            except Exception:
                pass


    def recover(self, connection: str, *, reviewed: bool, ledger=None, cancel=None) -> dict[str, Any]:
        """Explicitly recover durable stop intentions; never run automatically."""
        if reviewed is not True:
            raise DockerRecoveryError("Docker restart recovery requires explicit review")
        socket_path = self._socket(connection)
        current_daemon = self.daemon_identity(connection)
        with self._journal_lock():
            intentions = [entry.copy() for entry in self._read_journal_unlocked()
                          if entry["connection"] == connection]
        resolved = 0
        unresolved = []
        operation_ids = []
        for item in intentions:
            if cancel is not None and cancel.is_set():
                raise DockerRecoveryError("Docker recovery cancelled; unresolved intentions were retained")
            if item["socket"] != str(socket_path) or item["daemon_id"] != current_daemon:
                unresolved.append({"operation_id": item["operation_id"], "container_id": item["container_id"]})
                continue
            lock = -1
            try:
                lock = self._resource_lock(item["daemon_id"], item["container_id"])
                identity = f"docker:{connection}:{item['daemon_id']}:{item['container_name']}"
                if ledger is None:
                    if item["phase"] == "prepared":
                        self._resolve_prepared_intention(
                            connection, item["socket"], item["daemon_id"], item["container_id"],
                            item["operation_id"],
                        )
                    else:
                        self._recover_intention(
                            connection, item["socket"], item["daemon_id"], item["container_id"],
                            item["operation_id"], reviewed=True,
                        )
                else:
                    plan = OperationPlan.create(
                        identity, "docker-recover", ["docker", "recover", item["container_id"]],
                        env={}, timeout_seconds=self.recovery_timeout, destructive=True,
                    )
                    with ledger.begin(plan) as lease:
                        if item["phase"] == "prepared":
                            self._resolve_prepared_intention(
                                connection, item["socket"], item["daemon_id"], item["container_id"],
                                item["operation_id"],
                            )
                        else:
                            self._recover_intention(
                                connection, item["socket"], item["daemon_id"], item["container_id"],
                                item["operation_id"], reviewed=True,
                            )
                        lease.finish(OperationStatus.SUCCEEDED, 0)
                    operation_ids.append(plan.operation_id)
                resolved += 1
            except Exception:
                unresolved.append({"operation_id": item["operation_id"], "container_id": item["container_id"]})
            finally:
                if lock >= 0:
                    self._unlock(lock)
        return {
            "recovered": resolved,
            "unresolved": unresolved,
            "recovery_operation_ids": operation_ids,
            "replayed": False,
        }

    @staticmethod
    def _validate_metadata(raw: Any, source_name: str) -> dict[str, Any]:
        expected = {"schema_version", "kind", "source", "container_name", "container_id", "daemon_id",
                    "image_id", "config", "host", "healthcheck_configured", "mounts"}
        if (not isinstance(raw, dict) or set(raw) != expected or type(raw.get("schema_version")) is not int
                or raw["schema_version"] != _METADATA_SCHEMA or raw.get("kind") != "docker-container"
                or raw.get("source") != source_name or not isinstance(raw.get("container_id"), str)
                or not isinstance(raw.get("daemon_id"), str) or not isinstance(raw.get("image_id"), str)
                or not _IMAGE_ID.fullmatch(raw["image_id"]) or not isinstance(raw.get("mounts"), list)):
            raise DockerRecoveryError("Docker archive metadata is invalid or does not match the selected source")
        _container_name(raw.get("container_name"))
        config, host = raw.get("config"), raw.get("host")
        config_keys = {"env", "cmd", "entrypoint", "user", "working_dir", "labels", "exposed_ports",
                       "tty", "open_stdin", "stdin_once", "healthcheck", "hostname", "domainname", "stop_signal"}
        host_keys = {"network_mode", "restart_policy", "readonly_rootfs", "masked_paths", "readonly_paths"}
        if (not isinstance(config, dict) or set(config) != config_keys or not isinstance(host, dict)
                or set(host) != host_keys or host.get("network_mode") not in ("default", "bridge")
                or type(host.get("readonly_rootfs")) is not bool or not isinstance(config.get("env"), list)
                or any(not isinstance(value, str) or "\0" in value for value in config["env"])
                or not isinstance(config.get("labels"), dict)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in config["labels"].items())
                or not isinstance(config.get("exposed_ports"), dict)
                or type(config.get("tty")) is not bool or type(config.get("open_stdin")) is not bool
                or type(config.get("stdin_once")) is not bool or config.get("stdin_once")
                or type(raw.get("healthcheck_configured")) is not bool
                or raw["healthcheck_configured"] != bool(config.get("healthcheck"))):
            raise DockerRecoveryError("Docker archive deployment metadata is unsupported")
        _container_paths(host["masked_paths"], "Docker archive masked paths")
        _container_paths(host["readonly_paths"], "Docker archive read-only paths")
        for key in ("cmd", "entrypoint"):
            value = config.get(key)
            if value is not None and (not isinstance(value, list) or any(not isinstance(part, str) for part in value)):
                raise DockerRecoveryError("Docker archive command metadata is invalid")
        for key in ("user", "working_dir"):
            if config.get(key) is not None and not isinstance(config[key], str):
                raise DockerRecoveryError("Docker archive application metadata is invalid")
        for key in ("hostname", "domainname", "stop_signal"):
            if config.get(key) is not None and not isinstance(config[key], str):
                raise DockerRecoveryError("Docker archive deployment metadata is invalid")
        restart = host.get("restart_policy")
        if (not isinstance(restart, dict) or set(restart) != {"Name", "MaximumRetryCount"}
                or restart.get("Name") not in ("no", "always", "unless-stopped", "on-failure")
                or type(restart.get("MaximumRetryCount")) is not int or restart["MaximumRetryCount"] < 0):
            raise DockerRecoveryError("Docker archive restart policy is invalid")
        destinations = set()
        archive_sources = set()
        for mount in raw["mounts"]:
            archive_source = mount.get("archive_source") if isinstance(mount, dict) else None
            destination = mount.get("destination") if isinstance(mount, dict) else None
            if (not isinstance(mount, dict) or set(mount) != {"type", "archive_source", "destination", "read_only", "source_kind"}
                    or mount.get("type") not in ("bind", "volume")
                    or mount.get("source_kind") != ("bind" if mount.get("type") == "bind" else "local-volume")
                    or not isinstance(archive_source, str) or not Path(archive_source).is_absolute()
                    or archive_source == "/" or ".." in Path(archive_source).parts
                    or str(Path(archive_source)) != archive_source or archive_source in archive_sources
                    or not isinstance(destination, str) or not Path(destination).is_absolute()
                    or destination == "/" or ".." in Path(destination).parts
                    or str(Path(destination)) != destination or type(mount.get("read_only")) is not bool
                    or destination in destinations):
                raise DockerRecoveryError("Docker archive mount metadata is unsupported")
            destinations.add(destination)
            archive_sources.add(archive_source)
        return raw

    def restore_new(
        self,
        connection: str,
        archive_root: Path,
        *,
        source_name: str,
        container_name: str,
        trusted_archive: bool,
        expected_daemon_id: str,
        cancel=None,
    ) -> dict[str, Any]:
        """Create and start one new container using a captured private manifest.

        The archive is extracted by the caller into an existing, new target
        directory. No container is created until metadata, target name, local
        image availability, supported features, and every data path pass checks.
        Any failure after create begins leaves the target and container for review.
        """
        if not isinstance(source_name, str) or not _CONTAINER_NAME.fullmatch(source_name):
            raise DockerRecoveryError("Docker source name is invalid")
        if trusted_archive is not True:
            raise DockerRecoveryError("Docker restore requires explicit trusted_archive acknowledgement")
        _container_name(container_name)
        if cancel is not None and cancel.is_set():
            raise DockerRecoveryError("Docker restore cancelled before dispatch")
        root = Path(archive_root).resolve(strict=True)
        if not root.is_dir():
            raise DockerRecoveryError("Docker restore target must be a directory")
        matches = []
        filename = f"bbackup-docker-{source_name}.json"
        for path in root.rglob(filename):
            if path.is_symlink():
                raise DockerRecoveryError("Docker archive metadata cannot be a symbolic link")
            current = root
            for part in path.relative_to(root).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise DockerRecoveryError("Docker archive metadata path contains a symbolic link")
            matches.append(path)
        if len(matches) != 1:
            raise DockerRecoveryError("Docker archive must contain exactly one matching private manifest")
        metadata = self._validate_metadata(_private_json(matches[0], "Docker archive metadata"), source_name)
        if container_name == metadata["container_name"]:
            raise DockerRecoveryError("Docker restore requires a new container name")
        root_real = root.resolve(strict=True)
        mount_sources = []
        for mount in metadata["mounts"]:
            original = Path(mount["archive_source"])
            restored = root_real.joinpath(*original.parts[1:])
            if not _overlap(root_real, restored):
                raise DockerRecoveryError("Docker archive mount path escapes the new target")
            current = root_real
            for part in restored.relative_to(root_real).parts:
                current = current / part
                if current.is_symlink():
                    raise DockerRecoveryError("Docker archive mount path contains a symbolic link")
            try:
                info = restored.stat()
            except OSError:
                raise DockerRecoveryError("Docker archive is missing persistent application data") from None
            if mount["type"] == "volume" and not stat.S_ISDIR(info.st_mode):
                raise DockerRecoveryError("Docker archive local-volume data is not a directory")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise DockerRecoveryError("Docker archive mount data has an unsupported type")
            mount_sources.append((mount, restored))
        if cancel is not None and cancel.is_set():
            raise DockerRecoveryError("Docker restore cancelled before container creation")
        client = self._client(connection)
        try:
            daemon_id = self._daemon_id(client)
            if daemon_id != expected_daemon_id:
                raise DockerRecoveryError("Docker destination daemon identity changed before container creation")
            try:
                client.containers.get(container_name)
            except Exception as exc:
                if type(exc).__name__ != "NotFound":
                    raise DockerRecoveryError("Docker target name could not be checked safely") from None
            else:
                raise DockerRecoveryError("Docker restore target container name already exists")
            try:
                image = client.images.get(metadata["image_id"])
                image_config = image.attrs.get("Config") or {}
                if (getattr(image, "id", metadata["image_id"]) != metadata["image_id"]
                        or image_config.get("Healthcheck") != metadata["config"].get("healthcheck")
                        or (image_config.get("ExposedPorts") or {}) != metadata["config"].get("exposed_ports")):
                    raise DockerRecoveryError("Captured image configuration does not match the pinned local image")
            except DockerRecoveryError:
                raise
            except Exception:
                raise DockerRecoveryError("Captured image ID is not available locally; registry pulls are disabled") from None
            restored_mounts = [
                {
                    "Type": "bind",
                    "Source": str(path),
                    "Target": mount["destination"],
                    "ReadOnly": mount["read_only"],
                }
                for mount, path in mount_sources
            ]
            config = metadata["config"]
            host = metadata["host"]
            try:
                host_config = client.api.create_host_config(
                    network_mode=host["network_mode"],
                    restart_policy=host["restart_policy"],
                    read_only=host["readonly_rootfs"],
                )
                if not isinstance(host_config, Mapping):
                    raise TypeError("Docker SDK returned an invalid HostConfig")
                host_config = dict(host_config)
                host_config["Mounts"] = restored_mounts
                host_config["MaskedPaths"] = host["masked_paths"]
                host_config["ReadonlyPaths"] = host["readonly_paths"]
            except Exception:
                raise DockerRecoveryError("Docker restore HostConfig could not be constructed safely") from None
            try:
                created_result = client.api.create_container(
                    image=metadata["image_id"],
                    name=container_name,
                    command=config["cmd"],
                    entrypoint=config["entrypoint"],
                    environment=config["env"],
                    user=config["user"],
                    working_dir=config["working_dir"],
                    hostname=config["hostname"],
                    domainname=config["domainname"],
                    stop_signal=config["stop_signal"],
                    labels=config["labels"],
                    tty=config["tty"],
                    stdin_open=config["open_stdin"],
                    host_config=host_config,
                    use_config_proxy=False,
                )
            except Exception:
                raise DockerRecoveryUncertainError("Docker container creation outcome is uncertain; inspect the new target before retrying") from None
            created_id = created_result.get("Id") if isinstance(created_result, dict) else None
            if not isinstance(created_id, str) or not created_id:
                raise DockerRecoveryUncertainError("New Docker container identity is uncertain; inspect the target before retrying")
            try:
                created = client.containers.get(created_id)
            except Exception:
                raise DockerRecoveryUncertainError(
                    "New Docker container was created but its exact identity could not be reacquired"
                ) from None
            try:
                created.reload()
                if getattr(created, "attrs", {}).get("Id") != created_id:
                    raise DockerRecoveryUncertainError("New Docker container identity changed; inspect the target")
                if cancel is not None and cancel.is_set():
                    raise DockerRecoveryUncertainError("Docker restore cancelled after creation; inspect the retained new container")
                created.start()
            except DockerRecoveryUncertainError:
                raise
            except Exception:
                raise DockerRecoveryUncertainError("Docker start outcome is uncertain; inspect the retained new container") from None
            deadline = time.monotonic() + self.startup_timeout
            healthy = not metadata["healthcheck_configured"]
            while True:
                if cancel is not None and cancel.is_set():
                    raise DockerRecoveryUncertainError("Docker restore cancelled during startup; inspect the retained new container")
                try:
                    created.reload()
                    attrs = created.attrs
                except Exception:
                    raise DockerRecoveryUncertainError("Docker startup state is uncertain; inspect the retained new container") from None
                if attrs.get("Id") != created_id:
                    raise DockerRecoveryUncertainError("New Docker container identity changed during startup")
                state = attrs.get("State") or {}
                status = state.get("Status")
                if status == "running":
                    if metadata["healthcheck_configured"]:
                        health = (state.get("Health") or {}).get("Status")
                        if health == "healthy":
                            healthy = True
                        elif health == "unhealthy":
                            raise DockerRecoveryUncertainError("New Docker container is unhealthy; inspect the retained target")
                    if healthy:
                        break
                elif status in ("exited", "dead"):
                    raise DockerRecoveryUncertainError("New Docker container did not remain running; inspect the retained target")
                if time.monotonic() >= deadline:
                    raise DockerRecoveryUncertainError("New Docker container startup timed out; inspect the retained target")
                time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
            return {
                "daemon_id": daemon_id,
                "container_id": created_id,
                "container_name": container_name,
                "running": True,
                "healthcheck_passed": metadata["healthcheck_configured"],
                "application_data_validated": False,
                "image_id": metadata["image_id"],
            }
        finally:
            try:
                client.close()
            except Exception:
                pass
