"""Restore native PostgreSQL and MySQL/MariaDB exports into new databases."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .models import DatabaseBinding
from .operations import OperationPlan, OperationRunner, OperationStatus


_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_ARCHIVE_MAGIC = b"PGDMP"
_DATABASE_OPTION_KEYS = {"encoding", "collate", "ctype", "provider"}
_MYSQL_DATABASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MYSQL_METADATA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MYSQL_FORMATS = {
    "mysql": "mysql-single-database-sql-v1",
    "mariadb": "mariadb-single-database-sql-v1",
}
_MYSQL_TRANSPORT_KEYS = {
    "host",
    "port",
    "socket",
    "protocol",
    "ssl_ca",
    "ssl_capath",
    "ssl_cert",
    "ssl_key",
    "ssl_cipher",
    "ssl_crl",
    "ssl_crlpath",
    "tls_version",
    "tls_ciphersuites",
    "connect_timeout",
}


class DatabaseRestoreError(RuntimeError):
    """A native database restore could not be started or validated safely."""


class DatabaseRestoreUncertainError(DatabaseRestoreError):
    """A mutation may have started; inspect the new database before retrying."""

    def __init__(
        self,
        target_database: str,
        stage: str,
        *,
        database_type: str = "PostgreSQL",
    ) -> None:
        self.target_database = target_database
        self.stage = stage
        self.database_type = database_type
        super().__init__(
            f"{database_type} restore into new database {target_database} is uncertain "
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


def _decode_mysql_option_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options are ambiguous"
            )
        value = value[1:-1]
    elif any(character in value for character in ("'", '"', "#", ";")):
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options are ambiguous"
        )
    escapes = {
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "r": "\r",
        "s": " ",
        "\\": "\\",
    }
    decoded = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            decoded.append(escapes.get(following, "\\" + following))
            index += 2
        else:
            decoded.append(character)
            index += 1
    result = "".join(decoded)
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options contain an invalid value"
        )
    return result


def validate_mysql_database_name(name: str) -> str:
    """Validate and return a MySQL/MariaDB database identifier."""
    if not isinstance(name, str) or not _MYSQL_DATABASE_NAME.fullmatch(name):
        raise DatabaseRestoreError("MySQL/MariaDB database name is invalid")
    return name


def _read_private_mysql_options(path: Path) -> bytes:
    _private_regular_file(path, "MySQL/MariaDB provisioning options")
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or not info.st_mode & 0o400
            or info.st_size > 65_536
        ):
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options must be a private regular file"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options are too large"
            )
        raw.decode("utf-8")
        return raw
    except DatabaseRestoreError:
        raise
    except (OSError, UnicodeDecodeError):
        raise DatabaseRestoreError(
            "Could not securely read MySQL/MariaDB provisioning options"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_mysql_transport_options(
    binding: DatabaseBinding, raw: bytes
) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DatabaseRestoreError(
            "Could not securely read MySQL/MariaDB provisioning options"
        ) from None
    if "\0" in text:
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options contain an invalid value"
        )

    groups: set[str] = set()
    current_group: str | None = None
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        group = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", stripped)
        if group:
            current_group = group.group(1).lower()
            if current_group != "client" or current_group in groups:
                raise DatabaseRestoreError(
                    "MySQL/MariaDB provisioning options contain an unsupported group"
                )
            groups.add(current_group)
            continue
        if current_group != "client" or stripped.startswith("!"):
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options are not a simple client file"
            )
        setting = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(.*)", stripped)
        if not setting:
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options are ambiguous"
            )
        key = setting.group(1).replace("-", "_").lower()
        if key in values:
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning options contain duplicate settings"
            )
        values[key] = _decode_mysql_option_value(setting.group(2))
    if groups != {"client"} or not values.get("user"):
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options require one client group and an explicit user"
        )

    common = _MYSQL_TRANSPORT_KEYS
    if binding.kind == "mysql":
        allowed = common | {
            "ssl_mode",
            "get_server_public_key",
            "server_public_key_path",
        }
    else:
        allowed = common | {"ssl", "ssl_verify_server_cert"}
    if set(values) - allowed - {"user", "password"}:
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options contain an unsupported or unsafe setting"
        )
    options = {key: value for key, value in values.items() if key not in {"user", "password"}}
    if not options:
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options must specify a host or socket"
        )

    if "host" in options and (
        not re.fullmatch(r"[A-Za-z0-9._:%\-\[\]]{1,255}", options["host"])
    ):
        raise DatabaseRestoreError("MySQL/MariaDB host option is invalid")
    if "port" in options and (
        not options["port"].isdigit()
        or not 1 <= int(options["port"]) <= 65_535
    ):
        raise DatabaseRestoreError("MySQL/MariaDB port option is invalid")
    if "socket" in options and not Path(options["socket"]).is_absolute():
        raise DatabaseRestoreError("MySQL/MariaDB socket option must be absolute")
    protocol = options.get("protocol", "").lower()
    if protocol and protocol not in {"tcp", "socket"}:
        raise DatabaseRestoreError("MySQL/MariaDB protocol option is unsupported")
    if "socket" in options:
        if "host" in options or "port" in options or protocol == "tcp":
            raise DatabaseRestoreError(
                "MySQL/MariaDB provisioning transport is ambiguous"
            )
        options["protocol"] = "socket"
    elif "host" not in options or protocol == "socket":
        raise DatabaseRestoreError(
            "MySQL/MariaDB provisioning options require an explicit TCP host or socket"
        )
    elif protocol:
        options["protocol"] = protocol
    if "connect_timeout" in options and (
        not options["connect_timeout"].isdigit()
        or not 1 <= int(options["connect_timeout"]) <= 120
    ):
        raise DatabaseRestoreError("MySQL/MariaDB connect timeout is invalid")
    for key in (
        "ssl_ca",
        "ssl_capath",
        "ssl_cert",
        "ssl_key",
        "ssl_crl",
        "ssl_crlpath",
        "server_public_key_path",
    ):
        if key in options and not Path(options[key]).is_absolute():
            raise DatabaseRestoreError(
                "MySQL/MariaDB TLS file paths must be absolute"
            )
    for key in ("ssl_cipher", "tls_version", "tls_ciphersuites"):
        if key in options and not re.fullmatch(r"[A-Za-z0-9:_,.-]{1,512}", options[key]):
            raise DatabaseRestoreError("MySQL/MariaDB TLS option is invalid")
    if "ssl_mode" in options and (
        binding.kind != "mysql"
        or options["ssl_mode"].upper()
        not in {"DISABLED", "PREFERRED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"}
    ):
        raise DatabaseRestoreError("MySQL TLS mode is unsupported")
    for key in ("ssl", "ssl_verify_server_cert", "get_server_public_key"):
        if key in options and options[key].lower() not in {
            "0",
            "1",
            "off",
            "on",
            "false",
            "true",
        }:
            raise DatabaseRestoreError("MySQL/MariaDB TLS option is invalid")
    return options


def _read_mysql_transport_options(binding: DatabaseBinding) -> dict[str, str]:
    if binding.kind not in ("mysql", "mariadb") or binding.options_file is None:
        raise DatabaseRestoreError(
            "A MySQL or MariaDB provisioning options file is required"
        )
    return _parse_mysql_transport_options(
        binding, _read_private_mysql_options(binding.options_file)
    )


def extract_mysql_transport_options(binding: DatabaseBinding) -> dict[str, str]:
    """Return validated connection-only settings; credentials are never returned."""

    return _read_mysql_transport_options(binding)




def _validate_mysql_dump(path: Path) -> None:
    _private_regular_file(path, "MySQL/MariaDB SQL export")
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or not info.st_mode & 0o400
        ):
            raise DatabaseRestoreError(
                "MySQL/MariaDB SQL export must be a private regular file"
            )
    except DatabaseRestoreError:
        raise
    except OSError:
        raise DatabaseRestoreError(
            "Could not securely validate MySQL/MariaDB SQL export"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise DatabaseRestoreError(
            "Could not create a private MySQL/MariaDB restore file"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)



def _mysql_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "HOME": str(home),
        "MYSQL_TEST_LOGIN_FILE": os.devnull,
    }


def _mysql_argv(
    kind: str,
    options_file: Path,
    *,
    query: str | None = None,
    database: str | None = None,
) -> list[str]:
    argv = [
        "mysql" if kind == "mysql" else "mariadb",
        f"--defaults-file={options_file}",
    ]
    if kind == "mysql":
        argv.append("--no-login-paths")
    argv.extend(
        [
            "--batch",
            "--skip-column-names",
            "--raw",
            "--silent",
            "--binary-mode",
            "--skip-reconnect",
            "--local-infile=0",
        ]
    )
    if database is not None:
        argv.append(f"--database={database}")
    if query is not None:
        argv.append(f"--execute={query}")
    return argv


def _run_mysql(
    runner: OperationRunner,
    kind: str,
    options_file: Path,
    env: dict[str, str],
    timeout_seconds: float,
    cancel,
    *,
    target_database: str,
    stage: str,
    query: str | None = None,
    database: str | None = None,
    stdin_path: Path | None = None,
    uncertain: bool = False,
):
    database_type = "MySQL" if kind == "mysql" else "MariaDB"
    if timeout_seconds <= 0:
        raise DatabaseRestoreError(
            f"{database_type} restore timed out before command dispatch"
        )
    plan = OperationPlan.create(
        f"{kind}-restore:{target_database}",
        "db-restore",
        _mysql_argv(
            kind,
            options_file,
            query=query,
            database=database,
        ),
        env=env,
        timeout_seconds=timeout_seconds,
        stdin_path=stdin_path,
    )
    try:
        result = runner.run(plan, cancel_event=cancel)
    except Exception:
        if uncertain:
            raise DatabaseRestoreUncertainError(
                target_database, stage, database_type=database_type
            ) from None
        raise DatabaseRestoreError(
            f"{database_type} {stage} command failed before target creation"
        ) from None
    if result.status is OperationStatus.CANCELLED and result.exit_code is None:
        raise DatabaseRestoreError(
            f"{database_type} {stage} was cancelled before command dispatch"
        )
    if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
        if uncertain:
            raise DatabaseRestoreUncertainError(
                target_database, stage, database_type=database_type
            )
        raise DatabaseRestoreError(
            f"{database_type} {stage} did not complete ({result.status.value})"
        )
    return result


def _mysql_output(result, *, database_type: str, stage: str) -> str:
    if result.stdout_bytes != len(result.stdout_tail):
        raise DatabaseRestoreError(
            f"{database_type} {stage} result exceeded the bounded output limit"
        )
    try:
        return result.stdout_tail.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise DatabaseRestoreError(
            f"{database_type} {stage} returned invalid output"
        ) from None


def _mysql_server_details(
    output: str, database_type: str
) -> tuple[str, tuple[str, str, str, str]]:
    fields = output.split("\t")
    if len(fields) != 6 or any(not value for value in fields):
        raise DatabaseRestoreError(
            f"{database_type} server identity could not be validated"
        )
    version, comment, server_id, hostname, port, datadir = fields
    if not re.search(r"\d+\.\d+\.\d+", version) or not server_id.isdigit():
        raise DatabaseRestoreError(
            f"{database_type} server identity could not be validated"
        )
    if "mariadb" in (version + " " + comment).lower():
        product = "mariadb"
    elif "mysql" in comment.lower():
        product = "mysql"
    else:
        raise DatabaseRestoreError("Only MySQL and MariaDB servers are supported")
    if not port.isdigit() or not 0 <= int(port) <= 65_535:
        raise DatabaseRestoreError(
            f"{database_type} server identity could not be validated"
        )
    return product, (server_id, hostname, port, datadir)


def _mysql_lines(result, *, database_type: str, stage: str) -> list[str]:
    if result.stdout_bytes != len(result.stdout_tail):
        raise DatabaseRestoreError(
            f"{database_type} {stage} result exceeded the bounded output limit"
        )
    try:
        text = result.stdout_tail.decode("utf-8")
    except UnicodeDecodeError:
        raise DatabaseRestoreError(
            f"{database_type} {stage} returned invalid output"
        ) from None
    return text.splitlines()


def _mysql_count(result, *, database_type: str, stage: str) -> int:
    value = _mysql_output(result, database_type=database_type, stage=stage)
    if not value.isdigit():
        raise DatabaseRestoreError(
            f"{database_type} {stage} result could not be validated"
        )
    return int(value)


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



def restore_mysql(
    dump: Path,
    provisioning_binding: DatabaseBinding,
    target_database: str,
    *,
    source_database: str,
    export_format: str,
    character_set: str,
    collation: str,
    isolated_server_confirmed: bool = False,
    runner: OperationRunner | None = None,
    cancel=None,
    timeout: float = 3600,
) -> str:
    """Restore a native dump on an acknowledged, empty recovery server.

    The dump is imported with the pinned administrator connection because native
    schema SQL can create views, routines, triggers, and DEFINER objects. The
    administrator may affect the entire isolated server; this is not a
    target-only privilege boundary.
    """

    if isolated_server_confirmed is not True:
        raise DatabaseRestoreError(
            "An isolated recovery server must be explicitly confirmed"
        )

    kind = getattr(provisioning_binding, "kind", None)
    database_type = "MySQL" if kind == "mysql" else "MariaDB"
    if kind not in _MYSQL_FORMATS:
        raise DatabaseRestoreError(
            "A MySQL or MariaDB provisioning binding is required"
        )
    if export_format != _MYSQL_FORMATS[kind]:
        raise DatabaseRestoreError(
            "Only an explicit versioned single-database MySQL/MariaDB export is accepted"
        )
    try:
        target_database = validate_mysql_database_name(target_database)
        source_database = validate_mysql_database_name(source_database)
    except DatabaseRestoreError:
        raise DatabaseRestoreError("Source and target database names are invalid") from None
    if target_database != source_database:
        raise DatabaseRestoreError(
            "MySQL/MariaDB restore requires the exact source database name"
        )
    if (
        not isinstance(character_set, str)
        or not _MYSQL_METADATA_NAME.fullmatch(character_set)
        or not isinstance(collation, str)
        or not _MYSQL_METADATA_NAME.fullmatch(collation)
    ):
        raise DatabaseRestoreError(
            "MySQL/MariaDB character set and collation metadata are invalid"
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise DatabaseRestoreError(
            "Restore timeout must be a finite positive number"
        )

    deadline = time.monotonic() + timeout
    dump_path = Path(dump)
    _validate_mysql_dump(dump_path)
    options_path = provisioning_binding.options_file
    if options_path is None:
        raise DatabaseRestoreError(
            "A MySQL or MariaDB provisioning options file is required"
        )

    runner = runner or OperationRunner()
    mutation_started = False
    try:
        with tempfile.TemporaryDirectory(prefix="mysql-restore-") as temporary:
            private_dir = Path(temporary)
            admin_options = private_dir / "provisioner.cnf"
            pinned_options = _read_private_mysql_options(options_path)
            _parse_mysql_transport_options(provisioning_binding, pinned_options)
            _write_private_file(admin_options, pinned_options)
            env = _mysql_environment(private_dir)

            def remaining() -> float:
                return deadline - time.monotonic()

            def query_server_identity(
                stage: str, *, uncertain: bool = False
            ) -> tuple[str, str, str, str]:
                result = _run_mysql(
                    runner,
                    kind,
                    admin_options,
                    env,
                    remaining(),
                    cancel,
                    target_database=target_database,
                    stage=stage,
                    query=(
                        "SELECT @@version, @@version_comment, @@server_id, "
                        "@@hostname, @@port, @@datadir"
                    ),
                    uncertain=uncertain,
                )
                output = _mysql_output(
                    result, database_type=database_type, stage=stage
                )
                product, identity = _mysql_server_details(output, database_type)
                if product != kind:
                    raise DatabaseRestoreError(
                        "Provisioning binding does not match the requested database server kind"
                    )
                return identity

            def verify_identity(
                expected: tuple[str, str, str, str],
                stage: str,
                *,
                uncertain: bool,
            ) -> None:
                actual = query_server_identity(stage, uncertain=uncertain)
                if actual != expected:
                    if uncertain:
                        raise DatabaseRestoreUncertainError(
                            target_database, stage, database_type=database_type
                        )
                    raise DatabaseRestoreError(
                        f"{database_type} recovery server identity changed before creation"
                    )

            if cancel is not None and cancel.is_set():
                raise DatabaseRestoreError(
                    f"{database_type} restore cancelled before database creation"
                )

            server_identity = query_server_identity("server preflight")
            schemas = _mysql_lines(
                _run_mysql(
                    runner,
                    kind,
                    admin_options,
                    env,
                    remaining(),
                    cancel,
                    target_database=target_database,
                    stage="empty-server preflight",
                    query=(
                        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                        "ORDER BY SCHEMA_NAME"
                    ),
                ),
                database_type=database_type,
                stage="empty-server preflight",
            )
            allowed_schemas = {
                "mysql",
                "sys",
                "information_schema",
                "performance_schema",
            }
            if any(schema not in allowed_schemas for schema in schemas):
                raise DatabaseRestoreError(
                    f"{database_type} recovery server contains a user database"
                )

            collation_exists = _mysql_count(
                _run_mysql(
                    runner,
                    kind,
                    admin_options,
                    env,
                    remaining(),
                    cancel,
                    target_database=target_database,
                    stage="collation preflight",
                    query=(
                        "SELECT COUNT(*) FROM information_schema.COLLATIONS "
                        f"WHERE COLLATION_NAME = '{collation}' "
                        f"AND CHARACTER_SET_NAME = '{character_set}'"
                    ),
                ),
                database_type=database_type,
                stage="collation preflight",
            )
            if collation_exists != 1:
                raise DatabaseRestoreError(
                    "Captured MySQL/MariaDB character set and collation are unavailable"
                )

            if cancel is not None and cancel.is_set():
                raise DatabaseRestoreError(
                    f"{database_type} restore cancelled before database creation"
                )
            verify_identity(
                server_identity, "server identity preflight", uncertain=False
            )

            create_timeout = remaining()
            if create_timeout <= 0:
                raise DatabaseRestoreError(
                    f"{database_type} restore timed out before database creation"
                )
            mutation_started = True
            _run_mysql(
                runner,
                kind,
                admin_options,
                env,
                create_timeout,
                cancel,
                target_database=target_database,
                stage="database creation",
                query=(
                    f"CREATE DATABASE `{target_database}` "
                    f"CHARACTER SET `{character_set}` COLLATE `{collation}`"
                ),
                uncertain=True,
            )

            target_query = (
                "SELECT @@version, @@version_comment, @@server_id, @@hostname, "
                "@@port, @@datadir, DEFAULT_CHARACTER_SET_NAME, "
                "DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                f"WHERE SCHEMA_NAME = '{target_database}'"
            )

            def verify_target(stage: str) -> None:
                result = _run_mysql(
                    runner,
                    kind,
                    admin_options,
                    env,
                    remaining(),
                    cancel,
                    target_database=target_database,
                    stage=stage,
                    query=target_query,
                    uncertain=True,
                )
                output = _mysql_output(
                    result, database_type=database_type, stage=stage
                )
                fields = output.split("\t")
                if len(fields) != 8:
                    raise DatabaseRestoreError(
                        f"{database_type} target metadata could not be validated"
                    )
                product, identity = _mysql_server_details(
                    "\t".join(fields[:6]), database_type
                )
                if (
                    product != kind
                    or identity != server_identity
                    or fields[6] != character_set
                    or fields[7] != collation
                ):
                    raise DatabaseRestoreError(
                        f"{database_type} target metadata or server identity changed"
                    )

            verify_target("created-database verification")
            _run_mysql(
                runner,
                kind,
                admin_options,
                env,
                remaining(),
                cancel,
                target_database=target_database,
                stage="archive import",
                database=target_database,
                stdin_path=dump_path,
                uncertain=True,
            )
            verify_target("post-import verification")
    except DatabaseRestoreUncertainError:
        raise
    except DatabaseRestoreError:
        if mutation_started:
            raise DatabaseRestoreUncertainError(
                target_database,
                "post-mutation restore processing",
                database_type=database_type,
            ) from None
        raise
    except Exception:
        if mutation_started:
            raise DatabaseRestoreUncertainError(
                target_database,
                "post-mutation restore processing",
                database_type=database_type,
            ) from None
        raise DatabaseRestoreError(
            f"{database_type} restore failed before database creation"
        ) from None
    return target_database

__all__ = [
    "DatabaseRestoreError",
    "DatabaseRestoreUncertainError",
    "extract_mysql_transport_options",
    "validate_mysql_database_name",
    "restore_mysql",
    "restore_postgresql",
]
