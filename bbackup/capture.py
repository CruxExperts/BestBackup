"""Capture adapters for the production service; no archive or file staging copy."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import uuid
from typing import Any

from .models import DatabaseBinding, Source, strict_json
from .linux_coverage import CoverageError, verify_sqlite_identity
from .operations import OperationPlan, OperationRunner, OperationStatus
from .resource_limits import ResourceLimitError, ResourceMonitor


class DurableCaptureArtifacts:
    """Private, operation-owned remote exports; never discard uncertain data."""

    def __init__(self, staging: Path, state: Path, repository_id: str,
                 token: str, job: str):
        if not re.fullmatch(r"[a-f0-9]{64}", repository_id) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise CaptureError("Invalid durable capture identity")
        self.staging = Path(staging)
        self.directory = self.staging / f"capture-{token}"
        self.record_path = Path(state) / "capture-artifacts" / f"{token}.json"
        self.record = {"schema_version": 1, "repository_id": repository_id,
                       "capture_token": token, "job": job,
                       "stage": str(self.directory), "stage_inode": None,
                       "phase": "prepared", "pending": [], "exports": [], "manifest": None}

    def _save(self):
        parent = self.record_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
                or parent_info.st_mode & 0o077):
            raise CaptureError("Private capture record directory is not private")
        temporary = parent / f".{self.record_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                temporary.chmod(0o600)
                json.dump(self.record, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.record_path)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def prepare(self):
        if self.record_path.exists() or self.directory.exists():
            raise CaptureError("Durable capture identity is already in use")
        self._save()
        self.directory.mkdir(mode=0o700)
        self._sync_directory(self.staging)
        self.record["stage_inode"] = self.directory.lstat().st_ino
        self._save()

    def recover_empty_preparation(self):
        """Bind the exact empty stage left between mkdir and inode persistence."""

        if (self.record.get("phase") != "prepared" or self.record.get("stage_inode") is not None
                or self.record.get("pending") or self.record.get("exports")
                or self.record.get("manifest") is not None or not self.directory.exists()):
            return
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_dev != self.staging.stat().st_dev
                or any(self.directory.iterdir())):
            raise CaptureError("Unrecorded capture stage is not an empty owned preparation")
        self.record["stage_inode"] = info.st_ino
        self._save()

    def begin_export(self, path: Path):
        name = self._name(path)
        if name in self.record["pending"] or any(item["name"] == name for item in self.record["exports"]):
            raise CaptureError("Duplicate durable export path")
        self.record["pending"].append(name)
        self._save()

    def finish_export(self, path: Path):
        name = self._name(path)
        if name not in self.record["pending"]:
            raise CaptureError("Durable export was not prepared")
        self.record["exports"].append({"name": name, **self._fingerprint(path)})
        self._sync_directory(self.directory)
        self.record["pending"].remove(name)
        self._save()

    def ready(self, manifest: Path | None):
        if manifest is not None:
            name = self._name(manifest)
            if name not in self.record["pending"]:
                raise CaptureError("Durable capture manifest was not prepared")
            self.record["manifest"] = {"name": name, **self._fingerprint(manifest)}
            self.record["pending"].remove(name)
            self._save()
        if self.record["pending"]:
            raise CaptureError("An export is incomplete")
        self._sync_directory(self.directory)
        if self.record["manifest"] is not None and self.record["manifest"]["size"] > 64 * 1024:
            raise CaptureError("Durable capture manifest exceeds the inspection bound")
        self.record["phase"] = "ready"
        self._save()

    def _name(self, path: Path) -> str:
        path = Path(path)
        if path.parent != self.directory or path.name in ("", ".", ".."):
            raise CaptureError("Durable export is outside its stage")
        return path.name

    @staticmethod
    def _fingerprint(path: Path) -> dict:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise CaptureError("Durable capture file is not an owned private regular file")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = os.fstat(descriptor)
            if (after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns
                    or after.st_ctime_ns != info.st_ctime_ns):
                raise CaptureError("Durable capture changed while hashing")
            os.fsync(descriptor)
            return {"size": info.st_size, "sha256": digest.hexdigest()}
        finally:
            os.close(descriptor)

    @classmethod
    def load(cls, staging: Path, state: Path, repository_id: str, token: str):
        artifact = cls(staging, state, repository_id, token, "")
        parent_info = artifact.record_path.parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
                or parent_info.st_mode & 0o077):
            raise CaptureError("Private capture record directory changed")
        descriptor = os.open(artifact.record_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
                raise CaptureError("Durable capture record is not private")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                record = json.load(stream)
        finally:
            os.close(descriptor)
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("repository_id") != repository_id
                or record.get("capture_token") != token
                or record.get("stage") != str(artifact.directory)):
            raise CaptureError("Durable capture record identity changed")
        artifact.record = record
        return artifact

    def record_sha256(self) -> str:
        return self._fingerprint(self.record_path)["sha256"]

    def reviewed_failure(self, outcome: str):
        if outcome not in ("failed", "abandoned"):
            raise CaptureError("Only failed or abandoned captures retain exports")
        self.validate()
        observed = []
        for name in self.record["pending"]:
            path = self.directory / name
            observed.append({"name": name, **self._fingerprint(path)} if path.exists()
                            else {"name": name, "absent": True})
        self.record["pending_observed"] = observed
        self.record["reviewed_outcome"] = outcome
        self._save()

    def discard(self, expected_sha256: str):
        if (not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
                or not hmac.compare_digest(self.record_sha256(), expected_sha256)):
            raise CaptureError("Durable capture record digest changed")
        self.validate()
        if (self.record.get("reviewed_outcome") not in ("failed", "abandoned")
                or self.record["phase"] not in ("prepared", "ready", "discard_started")):
            raise CaptureError("Durable capture is not an explicitly reviewed failure")
        self.record["phase"] = "discard_started"
        self._save()
        if self.directory.exists():
            completed = [*self.record["exports"]]
            if self.record["manifest"] is not None:
                completed.append(self.record["manifest"])
            for item in completed:
                path = self.directory / item["name"]
                if path.exists():
                    if self._fingerprint(path) != {"size": item["size"], "sha256": item["sha256"]}:
                        raise CaptureError("Completed durable export changed during disposal")
                    path.unlink()
            observed = {item["name"]: item for item in self.record["pending_observed"]}
            for name in self.record["pending"]:
                path = self.directory / name
                if path.exists():
                    item = observed[name]
                    if item.get("absent") or self._fingerprint(path) != {
                            "size": item["size"], "sha256": item["sha256"]}:
                        raise CaptureError("Pending durable export changed during disposal")
                    path.unlink()
            self._sync_directory(self.directory)
            self.directory.rmdir()
            self._sync_directory(self.staging)
        self.record["phase"] = "discarded"
        self._save()

    def validate(self):
        record = self.record
        if (record.get("schema_version") != 1 or record.get("phase") not in
                ("prepared", "ready", "cleanup_started", "cleaned", "discard_started", "discarded")
                or not isinstance(record.get("job"), str) or not record["job"]
                or not isinstance(record.get("pending"), list)
                or not isinstance(record.get("exports"), list)
                or record.get("stage_inode") is not None and
                (type(record["stage_inode"]) is not int or record["stage_inode"] <= 0)):
            raise CaptureError("Durable capture record is invalid")
        completed = [*record["exports"]]
        if record.get("manifest") is not None:
            completed.append(record["manifest"])
        names = []
        for item in completed:
            if (not isinstance(item, dict) or set(item) != {"name", "size", "sha256"}
                    or type(item["size"]) is not int or item["size"] < 0
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
                raise CaptureError("Durable capture file record is invalid")
            names.append(item["name"])
        names.extend(record["pending"])
        if (any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
                or name in (".", "..") for name in names) or len(set(names)) != len(names)):
            raise CaptureError("Durable capture file names are invalid")
        if record["phase"] in ("ready", "cleanup_started", "cleaned") and record["exports"] and record.get("manifest") is None:
            raise CaptureError("Durable capture manifest is missing")
        observed = record.get("pending_observed")
        if record.get("reviewed_outcome") in ("failed", "abandoned"):
            if not isinstance(observed, list) or len(observed) != len(record["pending"]):
                raise CaptureError("Reviewed pending export record is invalid")
            for item in observed:
                if (not isinstance(item, dict) or item.get("name") not in record["pending"]
                        or (set(item) != {"name", "absent"} and
                            (set(item) != {"name", "size", "sha256"}
                             or type(item.get("size")) is not int or item["size"] < 0
                             or not isinstance(item.get("sha256"), str)
                             or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])))):
                    raise CaptureError("Reviewed pending export record is invalid")
            if len({item["name"] for item in observed}) != len(observed):
                raise CaptureError("Reviewed pending export record is duplicated")
        elif observed is not None:
            raise CaptureError("Pending export observations lack a reviewed failure")
        directory = self.directory
        if (not directory.exists() and self.record["phase"] == "prepared"
                and self.record["stage_inode"] is None
                and not self.record["exports"] and not self.record["pending"]):
            return
        if not directory.exists() and self.record["phase"] in (
                "cleanup_started", "cleaned", "discard_started", "discarded"):
            return
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_dev != self.staging.stat().st_dev
                or info.st_ino != self.record["stage_inode"]):
            raise CaptureError("Durable capture stage changed")
        entries = {item.name: item for item in directory.iterdir()}
        expected = set(self.record["pending"])
        expected.update(item["name"] for item in completed)
        if not set(entries).issubset(expected):
            raise CaptureError("Durable capture stage contains unknown files")
        for name, path in entries.items():
            if name in ("", ".", "..") or "/" in name:
                raise CaptureError("Invalid durable capture stage entry")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_dev != directory.stat().st_dev):
                raise CaptureError("Durable capture stage contains an unsafe file")
        missing_allowed = self.record["phase"] in (
            "cleanup_started", "cleaned", "discard_started", "discarded")
        for item in completed:
            name = item["name"]
            if name not in entries:
                if missing_allowed:
                    continue
                raise CaptureError("Completed durable export is missing")
            if self._fingerprint(entries[name]) != {"size": item["size"], "sha256": item["sha256"]}:
                raise CaptureError("Completed durable export changed")
        if observed is not None:
            for item in observed:
                name = item["name"]
                if item.get("absent") is True:
                    if name in entries:
                        raise CaptureError("Reviewed pending export appeared after inspection")
                elif name not in entries:
                    if self.record["phase"] not in ("discard_started", "discarded"):
                        raise CaptureError("Reviewed pending export is missing")
                elif self._fingerprint(entries[name]) != {
                        "size": item["size"], "sha256": item["sha256"]}:
                    raise CaptureError("Reviewed pending export changed")
        if self.record["phase"] in ("ready", "cleanup_started", "cleaned") and self.record["pending"]:
            raise CaptureError("Durable capture record is inconsistent")

    def cleanup_success(self):
        self.validate()
        if self.record["phase"] not in ("ready", "cleanup_started", "cleaned"):
            raise CaptureError("Durable capture was not ready for cleanup")
        if not self.directory.exists():
            if self.record["phase"] != "cleaned":
                self.record["phase"] = "cleaned"
                self._save()
            return
        self.record["phase"] = "cleanup_started"
        self._save()
        completed = [*self.record["exports"]]
        if self.record["manifest"] is not None:
            completed.append(self.record["manifest"])
        for item in completed:
            path = self.directory / item["name"]
            if path.exists():
                if self._fingerprint(path) != {"size": item["size"], "sha256": item["sha256"]}:
                    raise CaptureError("Completed durable export changed during cleanup")
                path.unlink()
        self.directory.rmdir()
        self._sync_directory(self.staging)
        self.record["phase"] = "cleaned"
        self._save()


class CaptureError(RuntimeError):
    """A requested source could not be completely captured."""


def _private_regular_file(path: Path, description: str) -> None:
    if not path.is_absolute():
        raise CaptureError(f"Private {description} must use an absolute path")
    try:
        info = path.lstat()
    except OSError:
        raise CaptureError(f"Private {description} is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise CaptureError(
            f"Private {description} must be a regular file with mode 0600 or stricter"
        )


def _database_binding(
    source: Source,
    database_bindings: Mapping[str, DatabaseBinding] | None,
) -> DatabaseBinding:
    if not source.database or not source.connection:
        raise CaptureError("Database source lacks a safe database or connection name")
    if database_bindings is None or source.connection not in database_bindings:
        raise CaptureError("Database source has no private host connection binding")
    binding = database_bindings[source.connection]
    if binding.kind != source.kind:
        raise CaptureError("Database source and private connection kinds do not match")
    if source.kind in ("mysql", "mariadb") and source.quiesce != "read-lock":
        raise CaptureError(
            "MySQL and MariaDB capture requires explicit read-lock quiescence"
        )
    for path in binding.sensitive_paths:
        _private_regular_file(path, "database client file")
    if source.kind == "postgresql" and binding.service_file is None:
        raise CaptureError("PostgreSQL source has no private service file")
    if source.kind in ("mysql", "mariadb") and binding.options_file is None:
        raise CaptureError("Database source has no private client options file")
    return binding


def _native_environment(**binding_values: str) -> dict[str, str]:
    """Build an exact native-client environment without inherited DB secrets."""

    environment = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    environment.update(binding_values)
    return environment


def _run_native(
    runner: OperationRunner,
    source: Source,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    cancel,
    on_stdout=None,
    monitor: ResourceMonitor | None = None,
):
    plan = OperationPlan.create(
        f"capture:{source.connection}:{source.name}",
        "db-capture",
        argv,
        env=env,
        timeout_seconds=timeout,
    )
    try:
        options = {"on_stdout": on_stdout, "cancel_event": cancel}
        if monitor is not None:
            options["on_tick"] = monitor.check
        result = runner.run(plan, **options)
    except ResourceLimitError:
        raise
    except Exception:
        raise CaptureError("Native database capture command failed") from None
    if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
        raise CaptureError(
            f"Native database capture did not complete successfully ({result.status.value})"
        )
    return result


def _bounded_write(output, chunk: bytes, monitor: ResourceMonitor | None):
    if monitor is not None:
        monitor.check(len(chunk))
    return output.write(chunk)


def _capture_postgresql(source, binding, target, runner, deadline, cancel, monitor=None):
    assert binding.service_file is not None and binding.service_name is not None
    env_values = {"PGSERVICEFILE": str(binding.service_file), "PGPASSFILE": "/dev/null",
                  "TMPDIR": str(target.parent)}
    if binding.password_file is not None:
        env_values["PGPASSFILE"] = str(binding.password_file)
    env = _native_environment(**env_values)
    connection = f"service={binding.service_name} dbname={source.database}"
    inspected = _run_native(
        runner,
        source,
        [
            "psql",
            "--no-psqlrc",
            "--no-password",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            f"--dbname={connection}",
            "--command=SELECT json_build_object('encoding', pg_encoding_to_char(encoding), "
            "'collate', datcollate, 'ctype', datctype, 'provider', datlocprovider) "
            "FROM pg_catalog.pg_database WHERE datname=current_database();",
        ],
        env=env,
        timeout=max(0.001, deadline - time.monotonic()),
        cancel=cancel,
        monitor=monitor,
    )
    try:
        if inspected.stdout_bytes != len(inspected.stdout_tail):
            raise ValueError()
        options = strict_json(inspected.stdout_tail.decode())
        if (
            not isinstance(options, dict)
            or set(options) != {"encoding", "collate", "ctype", "provider"}
            or options["provider"] != "c"
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or "\0" in value
                for value in options.values()
            )
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise CaptureError(
            "PostgreSQL encoding and libc locale metadata could not be captured"
        ) from None
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            _run_native(
                runner,
                source,
                [
                    "pg_dump",
                    "--no-password",
                    "--format=custom",
                    f"--dbname={connection}",
                ],
                env=env,
                timeout=max(0.001, deadline - time.monotonic()),
                cancel=cancel,
                on_stdout=lambda chunk: _bounded_write(output, chunk, monitor),
                monitor=monitor,
            )
            output.flush()
            os.fsync(output.fileno())
        with target.open("rb") as exported:
            if exported.read(5) != b"PGDMP":
                raise CaptureError(
                    "PostgreSQL client did not produce a custom-format archive"
                )
    except CaptureError:
        raise
    except OSError:
        raise CaptureError("Could not write the private PostgreSQL export") from None
    return options


def _capture_mysql(source, binding, target, runner, deadline, cancel, monitor=None):
    assert binding.options_file is not None and source.database is not None
    options = f"--defaults-file={binding.options_file}"
    client = "mysql" if source.kind == "mysql" else "mariadb"
    login_path_option = ["--no-login-paths"] if source.kind == "mysql" else []
    env_values = {"TMPDIR": str(target.parent)}
    if source.kind == "mariadb":
        # MariaDB clients may read ~/.mylogin.cnf; confine HOME to this private
        # staging directory and explicitly disable the login-path file.
        env_values["HOME"] = str(target.parent)
        env_values["MYSQL_TEST_LOGIN_FILE"] = os.devnull
    env = _native_environment(**env_values)
    metadata_query = (
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA "
        f"WHERE SCHEMA_NAME = '{source.database}'"
    )
    inspected = _run_native(
        runner,
        source,
        [
            client,
            options,
            *login_path_option,
            "--batch",
            "--skip-column-names",
            "--raw",
            f"--execute={metadata_query}",
            f"--database={source.database}",
        ],
        env=env,
        timeout=max(0.001, deadline - time.monotonic()),
        cancel=cancel,
        monitor=monitor,
    )
    if inspected.stdout_bytes != len(inspected.stdout_tail):
        raise CaptureError("MySQL and MariaDB database metadata is invalid")
    try:
        metadata = inspected.stdout_tail.decode("ascii").rstrip("\n").split("\t")
        if (
            len(metadata) != 2
            or any(
                not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value)
                for value in metadata
            )
        ):
            raise ValueError()
    except (UnicodeDecodeError, ValueError):
        raise CaptureError(
            "MySQL and MariaDB character set metadata could not be captured"
        ) from None

    query = (
        "SELECT COALESCE(ENGINE, '') FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{source.database}' AND TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_NAME"
    )
    non_innodb = False

    def inspect_engine(chunk: bytes) -> None:
        nonlocal non_innodb
        try:
            engine = chunk.decode("ascii").strip().lower()
        except UnicodeDecodeError:
            non_innodb = True
            return
        if not engine or engine != "innodb":
            non_innodb = True

    _run_native(
        runner,
        source,
        [
            client,
            options,
            *login_path_option,
            "--batch",
            "--skip-column-names",
            "--raw",
            f"--execute={query}",
            f"--database={source.database}",
        ],
        env=env,
        timeout=max(0.001, deadline - time.monotonic()),
        cancel=cancel,
        on_stdout=inspect_engine,
        monitor=monitor,
    )
    if non_innodb:
        raise CaptureError("MySQL and MariaDB capture requires InnoDB tables")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            _run_native(
                runner,
                source,
                [
                    "mysqldump" if source.kind == "mysql" else "mariadb-dump",
                    options,
                    *login_path_option,
                    "--lock-all-tables",
                    "--quick",
                    *(
                        ["--set-gtid-purged=OFF"]
                        if source.kind == "mysql"
                        else []
                    ),
                    "--routines",
                    "--events",
                    "--triggers",
                    source.database,
                ],
                env=env,
                timeout=max(0.001, deadline - time.monotonic()),
                cancel=cancel,
                on_stdout=lambda chunk: _bounded_write(output, chunk, monitor),
                monitor=monitor,
            )
            output.flush()
            os.fsync(output.fileno())
    except CaptureError:
        raise
    except OSError:
        raise CaptureError(
            "Could not write the private MySQL or MariaDB export"
        ) from None
    return {
        "source_database": source.database,
        "character_set": metadata[0],
        "collation": metadata[1],
    }


@contextmanager
def capture_sources(
    sources: tuple[Source, ...],
    state_dir: Path,
    *,
    timeout: float = 3600,
    cancel=None,
    database_bindings: Mapping[str, DatabaseBinding] | None = None,
    docker_runtime=None,
    protected_paths: tuple[Path, ...] = (),
    operation_id: str | None = None,
    runner: OperationRunner | None = None,
    resource_monitor: ResourceMonitor | None = None,
    coverage_sqlite: Mapping[str, Mapping[str, Any]] | None = None,
    durable_artifacts: DurableCaptureArtifacts | None = None,
):
    """Yield source paths plus temporary native exports and private Docker metadata.

    Ordinary file trees are live, best-effort captures, not atomic filesystem
    snapshots. Each PostgreSQL and SQLite export is transactional within that
    database; separate files and databases do not share a transaction. MySQL and
    MariaDB require an explicit read-lock quiescence declaration and use a
    server read lock during export. Docker sources require a local daemon and
    remain stopped only while the caller runs the restic operation.
    """
    deadline = time.monotonic() + timeout
    if timeout <= 0:
        raise CaptureError("Capture timeout must be positive")
    runner = runner or OperationRunner()
    docker_sources = tuple(source for source in sources if source.kind == "docker")
    if docker_sources and (docker_runtime is None or operation_id is None):
        raise CaptureError("Docker source capture requires the shared local Docker runtime")
    with ExitStack() as temporary_stack:
        if durable_artifacts is None:
            temporary = Path(temporary_stack.enter_context(
                tempfile.TemporaryDirectory(prefix="capture-", dir=state_dir)))
        else:
            durable_artifacts.prepare()
            temporary = durable_artifacts.directory
        if resource_monitor is not None:
            resource_monitor.check()
        paths = []
        exports = []
        for source in sources:
            if cancel is not None and cancel.is_set():
                raise CaptureError("Capture cancelled")
            if source.kind == "files":
                for raw in source.paths:
                    path = Path(raw)
                    if not path.exists():
                        raise CaptureError("A source path does not exist")
                    paths.append(str(path))
            elif source.kind == "sqlite":
                original = Path(source.paths[0])
                if not original.is_file():
                    raise CaptureError("SQLite source must be an existing database file")
                covered_sqlite = (coverage_sqlite or {}).get(source.name)
                if covered_sqlite is not None:
                    if (covered_sqlite.get("path") != str(original)
                            or covered_sqlite.get("source_id") != source.name
                            or not isinstance(covered_sqlite.get("identity"), dict)):
                        raise CaptureError("Reviewed SQLite mapping does not match the source")
                    try:
                        verify_sqlite_identity(original, covered_sqlite["identity"])
                    except CoverageError:
                        raise CaptureError("Reviewed SQLite source identity changed") from None
                target = Path(temporary) / (source.name + ".sqlite")
                if durable_artifacts is not None:
                    durable_artifacts.begin_export(target)

                def progress(status, remaining, total):
                    if time.monotonic() >= deadline:
                        raise CaptureError("SQLite capture timed out")
                    if cancel is not None and cancel.is_set():
                        raise CaptureError("Capture cancelled")
                    if resource_monitor is not None:
                        resource_monitor.check()

                try:
                    with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as src:
                        if resource_monitor is not None:
                            page_size = src.execute("PRAGMA page_size").fetchone()[0]
                            page_count = src.execute("PRAGMA page_count").fetchone()[0]
                            resource_monitor.check(page_size * page_count)
                        with sqlite3.connect(target) as dst:
                            target.chmod(0o600)
                            src.backup(dst, pages=256, progress=progress, sleep=0.05)
                            if resource_monitor is not None:
                                resource_monitor.check()
                            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                                raise CaptureError("SQLite capture validation failed")
                except sqlite3.Error as exc:
                    raise CaptureError("SQLite capture failed") from exc
                if durable_artifacts is not None:
                    durable_artifacts.finish_export(target)
                if covered_sqlite is not None:
                    try:
                        verify_sqlite_identity(original, covered_sqlite["identity"])
                    except CoverageError:
                        raise CaptureError("Reviewed SQLite source identity changed") from None
                paths.append(str(target))
                export = {
                    "source": source.name, "kind": source.kind,
                    "format": "sqlite", "path": str(target),
                }
                if covered_sqlite is not None:
                    digest = hashlib.sha256()
                    with target.open("rb") as exported:
                        for chunk in iter(lambda: exported.read(1024 * 1024), b""):
                            digest.update(chunk)
                    export.update({
                        "coverage_root_id": covered_sqlite["root_id"],
                        "original_path": str(original),
                        "reviewed_identity": covered_sqlite["identity"],
                        "recipe_digest": covered_sqlite["recipe_digest"],
                        "baseline_digest": covered_sqlite["baseline_digest"],
                        "export_sha256": digest.hexdigest(),
                    })
                exports.append(export)
            elif source.kind in ("postgresql", "mysql", "mariadb"):
                binding = _database_binding(source, database_bindings)
                suffix = ".dump" if source.kind == "postgresql" else ".sql"
                target = Path(temporary) / f"{source.name}{suffix}"
                if durable_artifacts is not None:
                    durable_artifacts.begin_export(target)
                if source.kind == "postgresql":
                    database_options = _capture_postgresql(
                        source, binding, target, runner, deadline, cancel, resource_monitor
                    )
                else:
                    mysql_metadata = _capture_mysql(
                        source, binding, target, runner, deadline, cancel, resource_monitor
                    )
                paths.append(str(target))
                exports.append({
                    "source": source.name,
                    "kind": source.kind,
                    "format": "postgresql-custom"
                    if source.kind == "postgresql"
                    else f"{source.kind}-single-database-sql-v1",
                    "path": str(target),
                })
                if source.kind == "postgresql":
                    exports[-1]["database_options"] = database_options
                else:
                    exports[-1].update(mysql_metadata)
                if durable_artifacts is not None:
                    durable_artifacts.finish_export(target)
            elif source.kind != "docker":
                raise CaptureError("Source adapter is not implemented")

        with ExitStack() as stack:
            if docker_sources:
                docker_paths, docker_exports = stack.enter_context(docker_runtime.capture(
                    docker_sources,
                    Path(temporary),
                    protected_paths=protected_paths,
                    operation_id=operation_id,
                    cancel=cancel,
                ))
                paths.extend(docker_paths)
                exports.extend(docker_exports)
            if exports:
                manifest = Path(temporary) / "bbackup-capture.json"
                if durable_artifacts is not None:
                    durable_artifacts.begin_export(manifest)
                with manifest.open("x", encoding="utf-8") as output:
                    manifest.chmod(0o600)
                    json.dump({"schema_version": 2, "exports": exports}, output)
                paths.append(str(manifest))
            if durable_artifacts is not None:
                durable_artifacts.ready(manifest if exports else None)
            yield paths
