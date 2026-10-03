"""Capture adapters for the production service; no archive or file staging copy."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from .models import DatabaseBinding, Source, strict_json
from .operations import OperationPlan, OperationRunner, OperationStatus


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
):
    plan = OperationPlan.create(
        f"capture:{source.connection}:{source.name}",
        "db-capture",
        argv,
        env=env,
        timeout_seconds=timeout,
    )
    try:
        result = runner.run(plan, on_stdout=on_stdout, cancel_event=cancel)
    except Exception:
        raise CaptureError("Native database capture command failed") from None
    if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
        raise CaptureError(
            f"Native database capture did not complete successfully ({result.status.value})"
        )
    return result


def _capture_postgresql(source, binding, target, runner, deadline, cancel):
    assert binding.service_file is not None and binding.service_name is not None
    env_values = {"PGSERVICEFILE": str(binding.service_file), "PGPASSFILE": "/dev/null"}
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
                on_stdout=output.write,
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


def _capture_mysql(source, binding, target, runner, deadline, cancel):
    assert binding.options_file is not None and source.database is not None
    options = f"--defaults-file={binding.options_file}"
    client = "mysql" if source.kind == "mysql" else "mariadb"
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

    env_values = {}
    if source.kind == "mariadb":
        # MariaDB clients may read ~/.mylogin.cnf; confine HOME to this private
        # staging directory and explicitly disable the login-path file.
        env_values["HOME"] = str(target.parent)
        env_values["MYSQL_TEST_LOGIN_FILE"] = os.devnull
    env = _native_environment(**env_values)
    login_path_option = ["--no-login-paths"] if source.kind == "mysql" else []
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
                    "--routines",
                    "--events",
                    "--triggers",
                    "--databases",
                    source.database,
                ],
                env=env,
                timeout=max(0.001, deadline - time.monotonic()),
                cancel=cancel,
                on_stdout=output.write,
            )
            output.flush()
            os.fsync(output.fileno())
    except CaptureError:
        raise
    except OSError:
        raise CaptureError(
            "Could not write the private MySQL or MariaDB export"
        ) from None


@contextmanager
def capture_sources(
    sources: tuple[Source, ...],
    state_dir: Path,
    *,
    timeout: float = 3600,
    cancel=None,
    database_bindings: Mapping[str, DatabaseBinding] | None = None,
    runner: OperationRunner | None = None,
):
    """Yield source paths plus temporary database exports.

    Ordinary file trees are live, best-effort captures, not atomic filesystem
    snapshots. PostgreSQL and SQLite use transactional exports. MySQL and
    MariaDB require an explicit read-lock quiescence declaration and use a
    server read lock during export.
    """
    deadline = time.monotonic() + timeout
    if timeout <= 0:
        raise CaptureError("Capture timeout must be positive")
    runner = runner or OperationRunner()
    with tempfile.TemporaryDirectory(prefix="capture-", dir=state_dir) as temporary:
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
                    raise CaptureError(
                        "SQLite source must be an existing database file"
                    )
                target = Path(temporary) / (source.name + ".sqlite")

                def progress(status, remaining, total):
                    if time.monotonic() >= deadline:
                        raise CaptureError("SQLite capture timed out")
                    if cancel is not None and cancel.is_set():
                        raise CaptureError("Capture cancelled")

                try:
                    with sqlite3.connect(
                        original.as_uri() + "?mode=ro", uri=True
                    ) as src:
                        with sqlite3.connect(target) as dst:
                            target.chmod(0o600)
                            src.backup(dst, pages=256, progress=progress, sleep=0.05)
                            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                                raise CaptureError("SQLite capture validation failed")
                except sqlite3.Error as exc:
                    raise CaptureError("SQLite capture failed") from exc
                paths.append(str(target))
                exports.append(
                    {
                        "source": source.name,
                        "kind": source.kind,
                        "format": "sqlite",
                        "path": str(target),
                    }
                )
            elif source.kind in ("postgresql", "mysql", "mariadb"):
                binding = _database_binding(source, database_bindings)
                suffix = ".dump" if source.kind == "postgresql" else ".sql"
                target = Path(temporary) / f"{source.name}{suffix}"
                if source.kind == "postgresql":
                    database_options = _capture_postgresql(
                        source, binding, target, runner, deadline, cancel
                    )
                else:
                    _capture_mysql(source, binding, target, runner, deadline, cancel)
                paths.append(str(target))
                exports.append(
                    {
                        "source": source.name,
                        "kind": source.kind,
                        "format": "postgresql-custom"
                        if source.kind == "postgresql"
                        else "sql",
                        "path": str(target),
                    }
                )
                if source.kind == "postgresql":
                    exports[-1]["database_options"] = database_options
            else:
                raise CaptureError("Source adapter is not implemented")
        if exports:
            manifest = Path(temporary) / "bbackup-capture.json"
            with manifest.open("x", encoding="utf-8") as output:
                manifest.chmod(0o600)
                json.dump({"schema_version": 2, "exports": exports}, output)
            paths.append(str(manifest))
        yield paths
