"""Restore trusted PostgreSQL custom archives into new databases only.

PostgreSQL archives can execute SQL and user-defined functions during restore.
The caller must verify archive provenance before invoking this helper.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import stat
import time

from .models import DatabaseBinding
from .operations import OperationPlan, OperationRunner, OperationStatus


_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_ARCHIVE_MAGIC = b"PGDMP"
_DATABASE_OPTION_KEYS = {"encoding", "collate", "ctype", "provider"}


class DatabaseRestoreError(RuntimeError):
    """A PostgreSQL restore could not be started or validated safely."""


class DatabaseRestoreUncertainError(DatabaseRestoreError):
    """A mutation may have started; inspect the new database before retrying."""

    def __init__(self, target_database: str, stage: str) -> None:
        self.target_database = target_database
        self.stage = stage
        super().__init__(
            f"PostgreSQL restore into new database {target_database} is uncertain "
            f"after {stage}; inspect it manually before retrying"
        )


def _private_regular_file(path: Path, description: str) -> os.stat_result:
    if not path.is_absolute():
        raise DatabaseRestoreError(f"{description} must be an absolute file path")
    try:
        info = path.lstat()
    except OSError:
        raise DatabaseRestoreError(f"{description} is unavailable") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or not info.st_mode & 0o400
    ):
        raise DatabaseRestoreError(
            f"{description} must be a private, owner-readable regular file"
        )
    return info


def _validate_archive(path: Path) -> None:
    _private_regular_file(path, "PostgreSQL archive")
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise DatabaseRestoreError(
                "PostgreSQL archive must be a private regular file"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            if stream.read(len(_ARCHIVE_MAGIC)) != _ARCHIVE_MAGIC:
                raise DatabaseRestoreError(
                    "PostgreSQL archive is not a custom-format PGDMP file"
                )
    except DatabaseRestoreError:
        raise
    except OSError:
        raise DatabaseRestoreError(
            "Could not securely read PostgreSQL archive"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_binding(binding: DatabaseBinding) -> dict[str, str]:
    if binding.kind != "postgresql":
        raise DatabaseRestoreError("A PostgreSQL connection binding is required")
    if binding.service_file is None or not binding.service_name:
        raise DatabaseRestoreError(
            "PostgreSQL service file and service name are required"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", binding.service_name):
        raise DatabaseRestoreError("PostgreSQL service name is invalid")
    _private_regular_file(binding.service_file, "PostgreSQL service file")
    if binding.password_file is not None:
        _private_regular_file(binding.password_file, "PostgreSQL password file")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "PGSERVICEFILE": str(binding.service_file),
        "PGPASSFILE": "/dev/null",
    }
    if binding.password_file is not None:
        env["PGPASSFILE"] = str(binding.password_file)
    return env


def _validate_database_options(database_options: dict[str, str]) -> dict[str, str]:
    if (
        not isinstance(database_options, dict)
        or set(database_options) != _DATABASE_OPTION_KEYS
    ):
        raise DatabaseRestoreError(
            "Database encoding and locale options are incomplete or unsupported"
        )
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or "\0" in value
        for value in database_options.values()
    ):
        raise DatabaseRestoreError("Database encoding and locale options are invalid")
    if database_options["provider"] != "c":
        raise DatabaseRestoreError(
            "Only PostgreSQL libc locale providers are supported"
        )
    return database_options


def _sql_literal(value: str) -> str:
    # E-strings keep locale metadata literal even when the connected server has
    # standard_conforming_strings disabled. Escape backslashes before quotes.
    return "E'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _psql_args(service: str, database: str, command: str) -> list[str]:
    connection = f"service={service} dbname={database}"
    return [
        "psql",
        "--no-psqlrc",
        "--no-password",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        f"--dbname={connection}",
        f"--command={command}",
    ]


def _run(
    runner: OperationRunner,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float,
    cancel,
    *,
    mutating_target: str | None = None,
    stage: str,
):
    if timeout_seconds <= 0:
        if mutating_target is not None:
            raise DatabaseRestoreUncertainError(mutating_target, stage)
        raise DatabaseRestoreError(
            "PostgreSQL restore timed out before database creation"
        )
    plan = OperationPlan.create(
        f"postgresql-restore:{mutating_target or 'preflight'}",
        "db-restore",
        argv,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    try:
        result = runner.run(plan, cancel_event=cancel)
    except Exception:
        if mutating_target is not None:
            raise DatabaseRestoreUncertainError(mutating_target, stage) from None
        raise DatabaseRestoreError("PostgreSQL connection preflight failed") from None
    if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
        if mutating_target is not None:
            raise DatabaseRestoreUncertainError(mutating_target, stage)
        raise DatabaseRestoreError(
            f"PostgreSQL connection preflight did not complete ({result.status.value})"
        )
    return result


def restore_postgresql(
    archive: Path,
    binding: DatabaseBinding,
    target_database: str,
    *,
    database_options: dict[str, str],
    runner: OperationRunner | None = None,
    cancel=None,
    timeout: float = 3600,
) -> str:
    """Restore a trusted custom archive to a new PostgreSQL database.

    The helper refuses existing targets, never drops databases, and retains a
    target after mutation begins. The returned name means `SELECT 1` completed
    successfully after ``pg_restore``.
    """

    if not isinstance(target_database, str) or not _DATABASE_NAME.fullmatch(
        target_database
    ):
        raise DatabaseRestoreError(
            "Target database must be a safe PostgreSQL identifier of at most 63 characters"
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise DatabaseRestoreError("Restore timeout must be a finite positive number")
    database_options = _validate_database_options(database_options)
    archive_path = Path(archive)
    _validate_archive(archive_path)
    env = _validate_binding(binding)
    runner = runner or OperationRunner()
    deadline = time.monotonic() + timeout
    if cancel is not None and cancel.is_set():
        raise DatabaseRestoreError(
            "PostgreSQL restore cancelled before database creation"
        )

    exists_query = (
        f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{target_database}';"
    )
    existing = _run(
        runner,
        _psql_args(binding.service_name, "postgres", exists_query),
        env,
        deadline - time.monotonic(),
        cancel,
        stage="preflight",
    )
    if existing.stdout_tail.strip() == b"1":
        raise DatabaseRestoreError("Target PostgreSQL database already exists")
    if existing.stdout_tail.strip():
        raise DatabaseRestoreError("Could not determine whether target database exists")
    if cancel is not None and cancel.is_set():
        raise DatabaseRestoreError(
            "PostgreSQL restore cancelled before database creation"
        )

    create_command = (
        f'CREATE DATABASE "{target_database}" TEMPLATE template0 '
        f"ENCODING {_sql_literal(database_options['encoding'])} "
        f"LC_COLLATE {_sql_literal(database_options['collate'])} "
        f"LC_CTYPE {_sql_literal(database_options['ctype'])} "
        "LOCALE_PROVIDER libc;"
    )
    _run(
        runner,
        _psql_args(binding.service_name, "postgres", create_command),
        env,
        deadline - time.monotonic(),
        cancel,
        mutating_target=target_database,
        stage="database creation",
    )

    restore_connection = f"service={binding.service_name} dbname={target_database}"
    _run(
        runner,
        [
            "pg_restore",
            "--exit-on-error",
            "--no-password",
            f"--dbname={restore_connection}",
            str(archive_path),
        ],
        env,
        deadline - time.monotonic(),
        cancel,
        mutating_target=target_database,
        stage="archive restore",
    )

    validation = _run(
        runner,
        _psql_args(binding.service_name, target_database, "SELECT 1;"),
        env,
        deadline - time.monotonic(),
        cancel,
        mutating_target=target_database,
        stage="post-restore validation",
    )
    if validation.stdout_tail.strip() != b"1":
        raise DatabaseRestoreUncertainError(target_database, "post-restore validation")
    return target_database


__all__ = [
    "DatabaseRestoreError",
    "DatabaseRestoreUncertainError",
    "restore_postgresql",
]
