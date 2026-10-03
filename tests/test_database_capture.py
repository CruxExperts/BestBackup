from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest

from bbackup.capture import CaptureError, capture_sources
from bbackup.models import DatabaseBinding, Source


def _private_file(path: Path, content: str = "private credential") -> Path:
    path.write_text(content)
    path.chmod(0o600)
    return path


def _script(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nset -eu\n" + content)
    path.chmod(0o700)
    return path


def _source(kind: str, *, database="app_db") -> Source:
    quiesce = "read-lock" if kind in ("mysql", "mariadb") else None
    return Source("database", kind, (), database, "production", quiesce)


def _postgres_binding(tmp_path: Path) -> DatabaseBinding:
    clients = tmp_path / "clients"
    clients.mkdir(exist_ok=True)
    _script(
        clients,
        "psql",
        'printf \'%s\' \'{"encoding":"UTF8","collate":"C","ctype":"C","provider":"c"}\'\n',
    )
    service = _private_file(tmp_path / "pg_service.conf", "[prod]\nhost=db.example\n")
    password = _private_file(
        tmp_path / "pgpass", "db.example:5432:app_db:user:secret-token\n"
    )
    return DatabaseBinding("postgresql", service, "prod", password)


def _mysql_binding(tmp_path: Path, kind="mysql") -> DatabaseBinding:
    options = _private_file(
        tmp_path / f"{kind}.cnf",
        "[client]\nhost=db.example\nuser=backup\npassword=secret-token\n",
    )
    return DatabaseBinding(kind, options_file=options)


def _capture(source, binding, state_dir, *, cancel=None, timeout=5):
    return capture_sources(
        (source,),
        state_dir,
        timeout=timeout,
        cancel=cancel,
        database_bindings={"production": binding},
    )


def test_postgresql_custom_dump_streams_to_private_temporary_file(
    tmp_path, monkeypatch
):
    clients = tmp_path / "clients"
    clients.mkdir()
    _script(
        clients,
        "pg_dump",
        "printf 'PGDMP-custom-dump\\n'\n"
        'printf \'%s\\n\' "$*" "$PGSERVICEFILE" "$PGPASSFILE" '
        '"${PGPASSWORD-}" "${MYSQL_PWD-}"\n',
    )
    monkeypatch.setenv("PATH", str(clients))
    monkeypatch.setenv("PGPASSWORD", "inherited-pg-secret")
    monkeypatch.setenv("MYSQL_PWD", "inherited-mysql-secret")
    exported = None

    with _capture(
        _source("postgresql"), _postgres_binding(tmp_path), tmp_path
    ) as paths:
        exported = Path(paths[0])
        content = exported.read_text()
        assert content.startswith("PGDMP-custom-dump\n")
        assert "--format=custom" in content
        assert "--no-password" in content
        assert "--file" not in content
        assert "service=prod dbname=app_db" in content
        assert "pg_service.conf" in content
        assert "pgpass" in content
        assert "inherited-pg-secret" not in content
        assert "inherited-mysql-secret" not in content
        assert exported.stat().st_mode & 0o777 == 0o600

    assert exported is not None and not exported.exists()


@pytest.mark.parametrize("output", ["", "not-a-custom-archive"])
def test_postgresql_rejects_successful_client_with_invalid_archive(
    tmp_path, monkeypatch, output
):
    clients = tmp_path / "clients"
    clients.mkdir()
    _script(clients, "pg_dump", f"printf '%s' '{output}'\n")
    monkeypatch.setenv("PATH", str(clients))
    with pytest.raises(CaptureError):
        with _capture(_source("postgresql"), _postgres_binding(tmp_path), tmp_path):
            pytest.fail("invalid archive unexpectedly became eligible for backup")
    assert not list(tmp_path.glob("capture-*"))


@pytest.mark.parametrize(
    ("kind", "client", "dump_client"),
    [("mysql", "mysql", "mysqldump"), ("mariadb", "mariadb", "mariadb-dump")],
)
def test_mysql_family_checks_engines_and_streams_complete_dump(
    tmp_path, monkeypatch, kind, client, dump_client
):
    clients = tmp_path / "clients"
    clients.mkdir()
    preflight_args = tmp_path / f"{kind}-preflight-args"
    _script(
        clients,
        client,
        f"printf '%s\\n' \"$*\" > '{preflight_args}'\nprintf 'InnoDB\\nInnoDB\\n'\n",
    )
    dump = _script(
        clients,
        dump_client,
        "printf 'database-dump\\n'\n"
        'printf \'%s\\n\' "$*" "${MYSQL_PWD-}" "${PGPASSWORD-}" '
        '"${HOME-}" "${MYSQL_TEST_LOGIN_FILE-}"\n',
    )
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    (ambient_home / ".mylogin.cnf").write_text(
        "user=ambient\npassword=ambient-secret\n"
    )
    monkeypatch.setenv("HOME", str(ambient_home))
    monkeypatch.setenv("PATH", str(clients))
    monkeypatch.setenv("MYSQL_PWD", "inherited-mysql-secret")
    monkeypatch.setenv("PGPASSWORD", "inherited-pg-secret")
    exported = None

    with _capture(_source(kind), _mysql_binding(tmp_path, kind), tmp_path) as paths:
        exported = Path(paths[0])
        content = exported.read_text()
        assert content.startswith("database-dump\n")
        assert "--defaults-file=" in content
        assert "--lock-all-tables" in content
        assert "--single-transaction" not in content
        assert "--quick" in content
        assert "--routines" in content
        assert "--events" in content
        assert "--triggers" in content
        assert "--databases app_db" in content
        assert "inherited-mysql-secret" not in content
        assert "inherited-pg-secret" not in content
        assert "ambient-secret" not in content
        if kind == "mysql":
            assert "--no-login-paths" in content
            assert preflight_args.read_text().split()[0].startswith("--defaults-file=")
            assert "--no-login-paths" in preflight_args.read_text()
        else:
            private_home, login_path = content.rstrip().splitlines()[-2:]
            assert "/capture-" in private_home
            assert login_path == "/dev/null"
            assert "--no-login-paths" not in preflight_args.read_text()
        assert exported.stat().st_mode & 0o777 == 0o600

    assert exported is not None and not exported.exists()
    assert dump.exists()


def test_mysql_capture_requires_explicit_read_lock_before_client_dispatch(tmp_path):
    binding = _mysql_binding(tmp_path)
    source = Source("database", "mysql", (), "app_db", "production")

    with pytest.raises(CaptureError, match="explicit read-lock quiescence"):
        with _capture(source, binding, tmp_path):
            pytest.fail("MySQL source without read-lock policy unexpectedly yielded")

    assert not list(tmp_path.glob("capture-*"))


def test_mysql_refuses_non_innodb_before_running_dump(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    _script(clients, "mysql", "printf 'MyISAM\\n'\n")
    dump_marker = tmp_path / "dump-was-run"
    _script(clients, "mysqldump", f"touch '{dump_marker}'\n")
    monkeypatch.setenv("PATH", str(clients))

    with pytest.raises(CaptureError, match="requires InnoDB tables"):
        with _capture(_source("mysql"), _mysql_binding(tmp_path), tmp_path):
            pytest.fail("nontransactional database unexpectedly yielded")

    assert not dump_marker.exists()
    assert not list(tmp_path.glob("capture-*"))


def test_failed_native_dump_is_sanitized_and_partial_export_is_removed(
    tmp_path, monkeypatch
):
    clients = tmp_path / "clients"
    clients.mkdir()
    _script(clients, "pg_dump", "printf 'private-error-token' >&2\nexit 7\n")
    monkeypatch.setenv("PATH", str(clients))
    exported_paths = []

    with pytest.raises(CaptureError) as caught:
        with _capture(
            _source("postgresql"), _postgres_binding(tmp_path), tmp_path
        ) as paths:
            exported_paths.extend(paths)

    assert "private-error-token" not in str(caught.value)
    assert len(exported_paths) == 0
    assert not list(tmp_path.glob("capture-*"))


def test_cancelled_native_dump_is_reaped_and_temporary_export_removed(
    tmp_path, monkeypatch
):
    clients = tmp_path / "clients"
    clients.mkdir()
    started = tmp_path / "pg-dump-started"
    _script(clients, "pg_dump", f": > '{started}'\nprintf 'partial'\nsleep 20\n")
    monkeypatch.setenv("PATH", f"{clients}:/usr/bin:/bin")
    cancel = threading.Event()

    def cancel_after_launch():
        deadline = time.monotonic() + 3
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    timer = threading.Thread(target=cancel_after_launch)
    timer.start()

    try:
        with pytest.raises(CaptureError, match="cancelled"):
            with _capture(
                _source("postgresql"),
                _postgres_binding(tmp_path),
                tmp_path,
                cancel=cancel,
                timeout=5,
            ):
                pytest.fail("cancelled database dump unexpectedly yielded")
    finally:
        timer.join(timeout=3)

    assert started.exists()
    assert not list(tmp_path.glob("capture-*"))


def test_database_client_files_must_be_private_regular_files(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    _script(clients, "pg_dump", "printf 'dump'\n")
    monkeypatch.setenv("PATH", str(clients))
    binding = _postgres_binding(tmp_path)
    binding.password_file.chmod(0o644)

    with pytest.raises(CaptureError, match="mode 0600"):
        with _capture(_source("postgresql"), binding, tmp_path):
            pytest.fail("insecure client file unexpectedly yielded")

    assert not list(tmp_path.glob("capture-*"))


def test_postgresql_refuses_unsupported_locale_before_export(tmp_path, monkeypatch):
    binding = _postgres_binding(tmp_path)
    clients = tmp_path / "clients"
    _script(
        clients,
        "psql",
        'printf \'%s\' \'{"encoding":"UTF8","collate":"C","ctype":"C","provider":"i"}\'\n',
    )
    marker = tmp_path / "unexpected-dump"
    _script(clients, "pg_dump", f"printf 'bad' > '{marker}'\n")
    monkeypatch.setenv("PATH", str(clients))
    with pytest.raises(CaptureError, match="locale metadata"):
        with _capture(_source("postgresql"), binding, tmp_path):
            pytest.fail("unsupported locale unexpectedly yielded")
    assert not marker.exists()
