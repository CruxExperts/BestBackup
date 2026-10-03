from __future__ import annotations

from pathlib import Path
import shlex
import threading
import time

import pytest

from bbackup.database_restore import (
    DatabaseRestoreError,
    DatabaseRestoreUncertainError,
    restore_postgresql,
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
