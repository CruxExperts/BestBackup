from __future__ import annotations

import shlex
import textwrap
import threading
import time
from pathlib import Path

import pytest

from bbackup.database_restore import (
    DatabaseRestoreError,
    DatabaseRestoreUncertainError,
    extract_mysql_transport_options,
    restore_mysql,
    restore_postgresql,
    validate_mysql_database_name,
)
from bbackup.models import DatabaseBinding


_DATABASE_OPTIONS = {
    "encoding": "UTF8",
    "collate": "C",
    "ctype": "C",
    "provider": "c",
}


def _private_file(path: Path, content: str | bytes) -> Path:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    path.chmod(0o600)
    return path


def _binding(tmp_path: Path) -> DatabaseBinding:
    service = _private_file(tmp_path / "pg_service.conf", "[prod]\nhost=db.example\n")
    password = _private_file(
        tmp_path / "pgpass", "db.example:5432:postgres:backup:credential-secret\n"
    )
    return DatabaseBinding("postgresql", service, "prod", password)


def _toolchain(
    tmp_path: Path,
    *,
    existing=False,
    fail_create=False,
    fail_restore=False,
    fail_validation=False,
    invalid_validation=False,
    slow_restore=False,
):
    clients = tmp_path / "clients"
    clients.mkdir()
    calls = tmp_path / "calls.log"
    environment = tmp_path / "environment.log"
    existing_marker = tmp_path / "existing-target"
    created_marker = tmp_path / "created-target"
    restore_started = tmp_path / "restore-started"
    fail_create_marker = tmp_path / "fail-create"
    fail_restore_marker = tmp_path / "fail-restore"
    fail_validation_marker = tmp_path / "fail-validation"
    invalid_validation_marker = tmp_path / "invalid-validation"
    slow_restore_marker = tmp_path / "slow-restore"
    for enabled, marker in (
        (existing, existing_marker),
        (fail_create, fail_create_marker),
        (fail_restore, fail_restore_marker),
        (fail_validation, fail_validation_marker),
        (invalid_validation, invalid_validation_marker),
        (slow_restore, slow_restore_marker),
    ):
        if enabled:
            marker.touch()
    q = shlex.quote
    psql = (
        "#!/bin/sh\nset -eu\n"
        f"printf '%s\\n' \"$*\" >> {q(str(calls))}\n"
        f"printf 'PGPASSWORD=%s PGPASSFILE=%s MYSQL_PWD=%s\\n' "
        f'"${{PGPASSWORD-}}" "${{PGPASSFILE-}}" "${{MYSQL_PWD-}}" >> {q(str(environment))}\n'
        f'case "$*" in\n'
        f"  *pg_catalog.pg_database*) printf 'preflight\\n' >> {q(str(calls))}; "
        f"if [ -e {q(str(existing_marker))} ]; then printf '1\\n'; fi ;;\n"
        f"  *CREATE*) printf 'create\\n' >> {q(str(calls))}; "
        f": > {q(str(created_marker))}; "
        f"if [ -e {q(str(fail_create_marker))} ]; then printf 'private-error-token\\n' >&2; exit 9; fi ;;\n"
        f"  *\"SELECT 1;\"*) printf 'validation\\n' >> {q(str(calls))}; "
        f"if [ -e {q(str(fail_validation_marker))} ]; then printf 'private-error-token\\n' >&2; exit 8; fi; "
        f"if [ -e {q(str(invalid_validation_marker))} ]; then printf 'unexpected\\n'; else printf '1\\n'; fi ;;\n"
        "  *) exit 40 ;;\nesac\n"
    )
    pg_restore = (
        "#!/bin/sh\nset -eu\n"
        f"printf 'restore\\n' >> {q(str(calls))}\n"
        f"printf '%s\\n' \"$*\" >> {q(str(calls))}\n"
        f"printf 'PGPASSWORD=%s PGPASSFILE=%s MYSQL_PWD=%s\\n' "
        f'"${{PGPASSWORD-}}" "${{PGPASSFILE-}}" "${{MYSQL_PWD-}}" >> {q(str(environment))}\n'
        f": > {q(str(restore_started))}\n"
        f"if [ -e {q(str(fail_restore_marker))} ]; then printf 'private-error-token\\n' >&2; exit 7; fi\n"
        f"if [ -e {q(str(slow_restore_marker))} ]; then sleep 20; fi\n"
    )
    for name, content in (("psql", psql), ("pg_restore", pg_restore)):
        path = clients / name
        path.write_text(content)
        path.chmod(0o700)
    archive = _private_file(tmp_path / "archive.dump", b"PGDMPfixture\n")
    return {
        "clients": clients,
        "calls": calls,
        "environment": environment,
        "existing_marker": existing_marker,
        "created_marker": created_marker,
        "restore_started": restore_started,
        "archive": archive,
    }


def _run(
    tmp_path,
    monkeypatch,
    tools,
    *,
    target="recovered_db",
    database_options=None,
    cancel=None,
    timeout=5,
):
    monkeypatch.setenv("PATH", f"{tools['clients']}:/usr/bin:/bin")
    monkeypatch.setenv("PGPASSWORD", "inherited-password-secret")
    monkeypatch.setenv("MYSQL_PWD", "inherited-mysql-secret")
    return restore_postgresql(
        tools["archive"],
        _binding(tmp_path),
        target,
        database_options=(
            _DATABASE_OPTIONS if database_options is None else database_options
        ),
        cancel=cancel,
        timeout=timeout,
    )


def test_restore_creates_new_database_restores_and_validates(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path)

    result = _run(tmp_path, monkeypatch, tools)

    assert result == "recovered_db"
    calls = tools["calls"].read_text()
    assert (
        "SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'recovered_db';" in calls
    )
    assert 'CREATE DATABASE "recovered_db" TEMPLATE' in calls
    assert "TEMPLATE template0" in calls
    assert "ENCODING E'UTF8'" in calls
    assert "LC_COLLATE E'C'" in calls
    assert "LC_CTYPE E'C'" in calls
    assert "LOCALE_PROVIDER libc" in calls
    assert "--exit-on-error" in calls
    assert "--no-password" in calls
    assert "service=prod dbname=recovered_db" in calls
    assert "SELECT 1;" in calls
    assert "DROP DATABASE" not in calls
    assert tools["created_marker"].exists()
    assert "credential-secret" not in calls
    environment = tools["environment"].read_text()
    assert "inherited-password-secret" not in environment
    assert "inherited-mysql-secret" not in environment
    assert str(tmp_path / "pgpass") in environment


def test_existing_target_is_refused_before_any_mutation(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path, existing=True)

    with pytest.raises(DatabaseRestoreError, match="already exists") as caught:
        _run(tmp_path, monkeypatch, tools)

    assert not isinstance(caught.value, DatabaseRestoreUncertainError)
    assert not tools["created_marker"].exists()
    assert not tools["restore_started"].exists()
    assert tools["calls"].read_text().splitlines() == [
        "--no-psqlrc --no-password --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 --dbname=service=prod dbname=postgres --command=SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'recovered_db';",
        "preflight",
    ]


def test_invalid_archive_and_target_are_rejected_before_client_dispatch(
    tmp_path, monkeypatch
):
    tools = _toolchain(tmp_path)
    tools["archive"].write_bytes(b"not an archive")

    with pytest.raises(DatabaseRestoreError, match="PGDMP"):
        _run(tmp_path, monkeypatch, tools)
    assert not tools["calls"].exists()

    tools["archive"].write_bytes(b"PGDMPfixture\n")
    tools["archive"].chmod(0o600)
    for unsafe in ("bad-name", "x" * 64, "db; DROP DATABASE postgres"):
        with pytest.raises(DatabaseRestoreError, match="safe PostgreSQL identifier"):
            _run(tmp_path, monkeypatch, tools, target=unsafe)
    assert not tools["calls"].exists()


def test_archive_symlink_is_rejected_before_client_dispatch(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path)
    linked = tmp_path / "linked.dump"
    linked.symlink_to(tools["archive"])

    with pytest.raises(DatabaseRestoreError, match="private, owner-readable"):
        restore_postgresql(
            linked,
            _binding(tmp_path),
            "recovered_db",
            database_options=_DATABASE_OPTIONS,
        )

    assert not tools["calls"].exists()


def test_nonprivate_archive_is_rejected_before_client_dispatch(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path)
    tools["archive"].chmod(0o644)

    with pytest.raises(DatabaseRestoreError, match="private, owner-readable"):
        _run(tmp_path, monkeypatch, tools)

    assert not tools["calls"].exists()


@pytest.mark.parametrize(
    "options",
    [
        {"encoding": "UTF8", "collate": "C", "ctype": "C"},
        {**_DATABASE_OPTIONS, "provider": "icu"},
        {**_DATABASE_OPTIONS, "encoding": ""},
        {**_DATABASE_OPTIONS, "collate": "x" * 129},
        {**_DATABASE_OPTIONS, "ctype": "C\0malformed"},
    ],
)
def test_unsupported_database_options_are_rejected_before_mutation(
    tmp_path, monkeypatch, options
):
    tools = _toolchain(tmp_path)

    with pytest.raises(DatabaseRestoreError):
        _run(tmp_path, monkeypatch, tools, database_options=options)

    assert not tools["calls"].exists()


def test_locale_values_are_quoted_as_sql_literals(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path)
    options = {**_DATABASE_OPTIONS, "collate": "C\\' locale"}

    result = _run(tmp_path, monkeypatch, tools, database_options=options)

    assert result == "recovered_db"
    assert "LC_COLLATE E'C\\\\\\' locale'" in tools["calls"].read_text()


def test_create_dispatch_failure_is_reported_uncertain_and_target_is_retained(
    tmp_path, monkeypatch
):
    tools = _toolchain(tmp_path, fail_create=True)

    with pytest.raises(
        DatabaseRestoreUncertainError, match="database creation"
    ) as caught:
        _run(tmp_path, monkeypatch, tools)

    assert caught.value.target_database == "recovered_db"
    assert tools["created_marker"].exists()
    assert not tools["restore_started"].exists()
    assert "private-error-token" not in str(caught.value)


def test_archive_restore_failure_keeps_new_database_and_is_uncertain(
    tmp_path, monkeypatch
):
    tools = _toolchain(tmp_path, fail_restore=True)

    with pytest.raises(
        DatabaseRestoreUncertainError, match="archive restore"
    ) as caught:
        _run(tmp_path, monkeypatch, tools)

    assert caught.value.stage == "archive restore"
    assert tools["created_marker"].exists()
    assert tools["restore_started"].exists()
    calls = tools["calls"].read_text()
    assert "DROP DATABASE" not in calls
    assert "private-error-token" not in str(caught.value)


def test_validation_failure_keeps_new_database_and_is_uncertain(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path, fail_validation=True)

    with pytest.raises(
        DatabaseRestoreUncertainError, match="post-restore validation"
    ) as caught:
        _run(tmp_path, monkeypatch, tools)

    assert caught.value.target_database == "recovered_db"
    assert tools["created_marker"].exists()
    assert tools["restore_started"].exists()
    assert "DROP DATABASE" not in tools["calls"].read_text()


def test_unexpected_validation_result_is_uncertain(tmp_path, monkeypatch):
    tools = _toolchain(tmp_path, invalid_validation=True)

    with pytest.raises(DatabaseRestoreUncertainError, match="validation"):
        _run(tmp_path, monkeypatch, tools)


def test_cancel_during_restore_is_uncertain_and_target_is_retained(
    tmp_path, monkeypatch
):
    tools = _toolchain(tmp_path, slow_restore=True)
    cancel = threading.Event()

    def cancel_after_dispatch():
        deadline = time.monotonic() + 3
        while not tools["restore_started"].exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    canceller = threading.Thread(target=cancel_after_dispatch)
    canceller.start()
    try:
        with pytest.raises(DatabaseRestoreUncertainError, match="archive restore"):
            _run(tmp_path, monkeypatch, tools, cancel=cancel, timeout=5)
    finally:
        canceller.join(timeout=3)

    assert tools["restore_started"].exists()
    assert tools["created_marker"].exists()
    assert "DROP DATABASE" not in tools["calls"].read_text()


def _mysql_toolchain(
    tmp_path: Path,
    *,
    kind: str = "mysql",
    schemas: tuple[str, ...] = (
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
    ),
    fail_import: bool = False,
    slow_import: bool = False,
    fail_verification: bool = False,
    identity_change_at: int | None = None,
    options_text: str | None = None,
    rotate_options_on_create: bool = False,
    expected_database: str = "source_db",
):
    clients = tmp_path / "mysql-clients"
    clients.mkdir()
    options = _private_file(
        tmp_path / "provisioner.cnf",
        options_text
        or "[client]\nhost=db.example\nport=3306\nuser=provisioner\npassword=admin-secret\n",
    )
    dump = _private_file(
        tmp_path / "native.sql",
        b"CREATE DEFINER=`owner`@`%` VIEW v AS SELECT 1;\n"
        b"CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET @x = 1;\n"
        b"CREATE PROCEDURE p() SELECT 1;\n",
    )
    paths = {
        name: tmp_path / name
        for name in (
            "mysql-calls.log",
            "mysql-environment.log",
            "mysql-target-created",
            "mysql-import-started",
            "mysql-imported-dump",
            "mysql-import-database",
            "mysql-identity-count",
            "mysql-verification-count",
        )
    }
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import os
        from pathlib import Path
        import sys
        import time

        PATHS = { {name: str(path) for name, path in paths.items()}!r }
        KIND = {kind!r}
        SCHEMAS = {schemas!r}
        FAIL_IMPORT = {fail_import!r}
        SLOW_IMPORT = {slow_import!r}
        FAIL_VERIFICATION = {fail_verification!r}
        IDENTITY_CHANGE_AT = {identity_change_at!r}
        OPTIONS_PATH = {str(options)!r}
        ROTATE_OPTIONS_ON_CREATE = {rotate_options_on_create!r}
        EXPECTED_DATABASE = {expected_database!r}

        def marker(name):
            return Path(PATHS[name])

        def record(name, text):
            with marker(name).open("a", encoding="utf-8") as output:
                output.write(text + "\\n")

        args = sys.argv[1:]
        options_arg = next(arg for arg in args if arg.startswith("--defaults-file="))
        options_file = Path(options_arg.split("=", 1)[1])
        query_arg = next((arg for arg in args if arg.startswith("--execute=")), None)
        query = query_arg.split("=", 1)[1] if query_arg is not None else None
        settings = dict()
        for line in options_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("["):
                key, value = line.split("=", 1)
                settings[key.strip()] = value.strip().strip("'").strip('"')
        endpoint = settings.get("host", "unknown")
        record(
            "mysql-calls.log",
            "admin endpoint=" + endpoint + " defaults=" + str(options_file)
            + " args=" + repr(args),
        )
        record(
            "mysql-environment.log",
            "MYSQL_PWD=" + os.environ.get("MYSQL_PWD", "<missing>")
            + " PGPASSWORD=" + os.environ.get("PGPASSWORD", "<missing>"),
        )

        if query is None:
            database_arg = next(
                (arg for arg in args if arg.startswith("--database=")), ""
            )
            database = database_arg.split("=", 1)[1] if database_arg else ""
            if database != EXPECTED_DATABASE:
                print("unexpected import database", file=sys.stderr)
                raise SystemExit(44)
            marker("mysql-import-database").write_text(database, encoding="utf-8")
            marker("mysql-import-started").touch()
            with marker("mysql-imported-dump").open("wb") as output:
                output.write(sys.stdin.buffer.read())
            if SLOW_IMPORT:
                time.sleep(30)
            if FAIL_IMPORT:
                print("private-error-token", file=sys.stderr)
                raise SystemExit(7)
            raise SystemExit(0)

        if "@@version" in query:
            count_path = marker("mysql-identity-count")
            count = int(count_path.read_text() if count_path.exists() else "0") + 1
            count_path.write_text(str(count), encoding="ascii")
            server_id = (
                "43"
                if IDENTITY_CHANGE_AT is not None and count >= IDENTITY_CHANGE_AT
                else "42"
            )
            version = (
                "8.0.36\\tMySQL Community Server - GPL"
                if KIND == "mysql"
                else "10.11.7-MariaDB\\tMariaDB Server"
            )
            identity = version.split("\\t") + [
                server_id,
                "db-host",
                "0" if KIND == "mariadb" else "3306",
                "/var/lib/mysql",
            ]
            if "DEFAULT_CHARACTER_SET_NAME" in query:
                verify_path = marker("mysql-verification-count")
                verify_count = (
                    int(verify_path.read_text() if verify_path.exists() else "0") + 1
                )
                verify_path.write_text(str(verify_count), encoding="ascii")
                character_set = (
                    "latin1"
                    if FAIL_VERIFICATION and verify_count >= 2
                    else "utf8mb4"
                )
                print(
                    "\\t".join(
                        identity + [character_set, "utf8mb4_general_ci"]
                    )
                )
            else:
                print("\\t".join(identity))
        elif query == (
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME"
        ):
            for schema in SCHEMAS:
                print(schema)
        elif "information_schema.COLLATIONS" in query:
            print("1")
        elif query.startswith("CREATE DATABASE "):
            marker("mysql-target-created").touch()
            if ROTATE_OPTIONS_ON_CREATE:
                Path(OPTIONS_PATH).write_text(
                    "[client]\\nhost=attacker.example\\nport=3306\\n"
                    "user=provisioner\\npassword=rotated-secret\\n",
                    encoding="utf-8",
                )
        else:
            print("unexpected administrator query", file=sys.stderr)
            raise SystemExit(43)
        """
    )
    for name in ("mysql", "mariadb"):
        client = clients / name
        client.write_text(script, encoding="utf-8")
        client.chmod(0o700)
    return {
        "binding": DatabaseBinding(kind, options_file=options),
        "calls": paths["mysql-calls.log"],
        "environment": paths["mysql-environment.log"],
        "target_created": paths["mysql-target-created"],
        "import_started": paths["mysql-import-started"],
        "imported_dump": paths["mysql-imported-dump"],
        "import_database": paths["mysql-import-database"],
        "options": options,
        "dump": dump,
        "clients": clients,
    }


def _run_mysql_restore(
    tools,
    monkeypatch,
    *,
    target="source_db",
    source="source_db",
    export_format=None,
    isolated_server_confirmed=True,
    cancel=None,
    timeout=20,
    runner=None,
):
    monkeypatch.setenv("PATH", f"{tools['clients']}:/usr/bin:/bin")
    monkeypatch.setenv("PGPASSWORD", "inherited-pg-secret")
    monkeypatch.setenv("MYSQL_PWD", "inherited-mysql-secret")
    kind = tools["binding"].kind
    return restore_mysql(
        tools["dump"],
        tools["binding"],
        target,
        source_database=source,
        export_format=export_format or f"{kind}-single-database-sql-v1",
        character_set="utf8mb4",
        collation="utf8mb4_general_ci",
        isolated_server_confirmed=isolated_server_confirmed,
        cancel=cancel,
        timeout=timeout,
        runner=runner,
    )


@pytest.mark.parametrize("kind", ["mysql", "mariadb"])
def test_mysql_family_restore_imports_full_dump_with_captured_schema_metadata(
    tmp_path, monkeypatch, kind
):
    tools = _mysql_toolchain(tmp_path, kind=kind)
    original_dump = tools["dump"].read_bytes()

    result = _run_mysql_restore(tools, monkeypatch)

    calls = tools["calls"].read_text()
    assert result == "source_db"
    assert tools["target_created"].exists()
    assert tools["import_started"].exists()
    assert tools["imported_dump"].read_bytes() == original_dump
    assert tools["import_database"].read_text() == "source_db"
    assert "CHARACTER SET `utf8mb4` COLLATE `utf8mb4_general_ci`" in calls
    assert "SELECT 1" not in calls
    environment = tools["environment"].read_text()
    assert "MYSQL_PWD=<missing>" in environment
    assert "PGPASSWORD=<missing>" in environment
    assert "inherited-mysql-secret" not in environment
    assert "inherited-pg-secret" not in environment


def test_mysql_restore_pins_provisioning_options_for_every_admin_command(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, rotate_options_on_create=True)

    assert _run_mysql_restore(tools, monkeypatch) == "source_db"

    calls = tools["calls"].read_text().splitlines()
    assert "host=attacker.example" in tools["options"].read_text()
    assert calls
    assert all("endpoint=db.example" in line for line in calls)
    assert all(str(tools["options"]) not in line for line in calls)
    pinned_files = {
        line.split(" defaults=", 1)[1].split(" ", 1)[0]
        for line in calls
    }
    assert len(pinned_files) == 1
    assert all("attacker.example" not in line for line in calls)


@pytest.mark.parametrize("name", ["db", "restore-db.v2", "a" * 64])
def test_validate_mysql_database_name_returns_valid_name(name):
    assert validate_mysql_database_name(name) == name


@pytest.mark.parametrize("name", ["", "_db", "-db", "a" * 65, "db name", "db`"])
def test_validate_mysql_database_name_rejects_invalid_name(name):
    with pytest.raises(DatabaseRestoreError):
        validate_mysql_database_name(name)


def test_extract_mysql_transport_options_keeps_only_validated_connection_settings(
    tmp_path
):
    tools = _mysql_toolchain(
        tmp_path,
        options_text=(
            "[client]\nhost=db.example\nport=3306\nuser=provisioner\n"
            "password=admin-secret\n"
        ),
    )

    assert extract_mysql_transport_options(tools["binding"]) == {
        "host": "db.example",
        "port": "3306",
    }


def test_mysql_restore_requires_explicit_isolated_server_approval(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path)

    with pytest.raises(DatabaseRestoreError, match="explicitly confirmed"):
        _run_mysql_restore(tools, monkeypatch, isolated_server_confirmed=False)

    assert not tools["calls"].exists()
    assert not tools["target_created"].exists()


@pytest.mark.parametrize("schemas", [("source_db",), ("other_db",)])
def test_mysql_restore_refuses_any_existing_user_database_before_mutation(
    tmp_path, monkeypatch, schemas
):
    tools = _mysql_toolchain(tmp_path, schemas=schemas)

    with pytest.raises(DatabaseRestoreError, match="contains a user database"):
        _run_mysql_restore(tools, monkeypatch)

    assert not tools["target_created"].exists()
    assert not tools["import_started"].exists()


def test_mysql_restore_requires_exact_source_database_name_before_dispatch(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path)

    with pytest.raises(DatabaseRestoreError, match="exact source database name"):
        _run_mysql_restore(tools, monkeypatch, target="renamed_db")

    assert not tools["calls"].exists()


def test_mysql_restore_rejects_legacy_generic_sql_manifest_before_dispatch(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path)

    with pytest.raises(DatabaseRestoreError, match="explicit versioned"):
        _run_mysql_restore(tools, monkeypatch, export_format="sql")

    assert not tools["calls"].exists()


def test_mysql_restore_refuses_server_identity_change_before_creation(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, identity_change_at=2)

    with pytest.raises(DatabaseRestoreError, match="identity changed"):
        _run_mysql_restore(tools, monkeypatch)

    assert not tools["target_created"].exists()
    assert not tools["import_started"].exists()


def test_mysql_restore_marks_post_creation_identity_change_uncertain(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, identity_change_at=3)

    with pytest.raises(DatabaseRestoreUncertainError):
        _run_mysql_restore(tools, monkeypatch)

    assert tools["target_created"].exists()
    assert not tools["import_started"].exists()


def test_mysql_restore_sanitizes_failed_import_and_retains_partial_target(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, fail_import=True)

    with pytest.raises(
        DatabaseRestoreUncertainError, match="archive import"
    ) as caught:
        _run_mysql_restore(tools, monkeypatch)

    assert tools["target_created"].exists()
    assert tools["import_started"].exists()
    assert tools["imported_dump"].read_bytes() == tools["dump"].read_bytes()
    assert "private-error-token" not in str(caught.value)


def test_mysql_restore_cancellation_is_uncertain_and_keeps_target(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, slow_import=True)
    cancel = threading.Event()

    def cancel_after_import_starts():
        deadline = time.monotonic() + 5
        while not tools["import_started"].exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    canceller = threading.Thread(target=cancel_after_import_starts)
    canceller.start()
    try:
        with pytest.raises(DatabaseRestoreUncertainError, match="archive import"):
            _run_mysql_restore(tools, monkeypatch, cancel=cancel, timeout=15)
    finally:
        canceller.join(timeout=5)

    assert tools["target_created"].exists()
    assert tools["import_started"].exists()


def test_mysql_restore_verification_failure_is_uncertain_and_retains_target(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(tmp_path, fail_verification=True)

    with pytest.raises(DatabaseRestoreUncertainError):
        _run_mysql_restore(tools, monkeypatch)

    assert tools["target_created"].exists()
    assert tools["import_started"].exists()


def test_mysql_restore_rejects_unsafe_provisioning_option_before_dispatch(
    tmp_path, monkeypatch
):
    tools = _mysql_toolchain(
        tmp_path,
        options_text=(
            "[client]\nhost=db.example\nuser=provisioner\npassword=admin-secret\n"
            "init-command=DROP DATABASE production\n"
        ),
    )

    with pytest.raises(DatabaseRestoreError):
        _run_mysql_restore(tools, monkeypatch)

    assert not tools["calls"].exists()
