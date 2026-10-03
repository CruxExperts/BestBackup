import sqlite3
import shutil

import pytest

from bbackup.models import Configuration, HostBindings, RepositoryBinding
from bbackup.operations import (
    OperationPlan,
    OperationRun,
    OperationStatus,
    RepositoryConflictError,
)
from bbackup.service import BackupService, ServiceError


@pytest.fixture
def service(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "hello.txt").write_text("restore me\n")
    (data / "hello.txt").chmod(0o640)
    (data / "link").symlink_to("hello.txt")
    config = Configuration.parse({"schema_version": 2,
        "repositories": [{"name": "local"}, {"name": "copy", "role": "replica"}],
        "sources": [{"name": "files", "kind": "files", "paths": [str(data)]}],
        "jobs": [{"name": "daily", "repository": "local", "sources": ["files"], "replicas": ["copy"]}]})
    repositories = {}
    for name in ("local", "copy"):
        password = tmp_path / (name + ".password")
        password.write_text(name + "-independent-password")
        password.chmod(0o600)
        repositories[name] = RepositoryBinding(str(tmp_path / (name + "-repo")), password)
    return BackupService(config, HostBindings(tmp_path / "state", repositories))


def _run(plan, *, status=OperationStatus.SUCCEEDED, exit_code=0, stdout=b""):
    return OperationRun(
        operation_id=plan.operation_id,
        repository_id=plan.repository_id,
        status=status,
        exit_code=exit_code,
        started_at="2026-10-03T00:00:00+00:00",
        finished_at="2026-10-03T00:00:00+00:00",
        elapsed_seconds=0.01,
        stdout_tail=stdout[-64 * 1024 :],
        stderr_tail=b"",
        stdout_bytes=len(stdout),
        stderr_bytes=0,
    )


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic is required")
def test_database_restore_uses_snapshot_manifest_without_original_sources(service, tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from bbackup.models import DatabaseBinding

    clients = tmp_path / "clients"
    clients.mkdir()
    dump = clients / "pg_dump"
    dump.write_text("#!/bin/sh\nprintf 'PGDMP-restorable-database'\n")
    dump.chmod(0o700)
    psql = clients / "psql"
    psql.write_text("#!/bin/sh\nprintf '%s' '{\"encoding\":\"UTF8\",\"collate\":\"C\",\"ctype\":\"C\",\"provider\":\"c\"}'\n")
    psql.chmod(0o700)
    monkeypatch.setenv("PATH", str(clients) + os.pathsep + os.environ["PATH"])
    private = tmp_path / "postgres.conf"
    private.write_text("[capture]\nhost=unused\n")
    private.chmod(0o600)
    db_binding = DatabaseBinding("postgresql", private, "capture")
    config = Configuration.parse({"schema_version": 2,
        "repositories": [{"name": "local"}, {"name": "copy", "role": "replica"}],
        "sources": [{"name": "old-source", "kind": "postgresql", "database": "old_database", "connection": "pg"}],
        "jobs": [{"name": "daily", "repository": "local", "sources": ["old-source"]}]})
    captured = BackupService(config, HostBindings(service.state, service.bindings.repositories, {"pg": db_binding}))
    captured.initialize("local")
    snapshot = captured.capture("daily")["snapshot_id"]
    empty_config = Configuration.parse({"schema_version": 2,
        "repositories": [{"name": "local"}, {"name": "copy", "role": "replica"}], "sources": [], "jobs": []})
    fresh = BackupService(empty_config, HostBindings(tmp_path / "fresh-state", service.bindings.repositories, {"destination": db_binding}))

    def restore(archive, binding, target_database, **kwargs):
        assert Path(archive).read_bytes() == b"PGDMP-restorable-database"
        assert binding == db_binding
        assert kwargs["database_options"] == {"encoding": "UTF8", "collate": "C", "ctype": "C", "provider": "c"}
        return target_database

    monkeypatch.setattr("bbackup.database_restore.restore_postgresql", restore)
    with pytest.raises(ServiceError, match="explicitly trusted"):
        fresh.restore_database("local", snapshot, "old-source", "destination", "recovered")
    result = fresh.restore_database("local", snapshot, "old-source", "destination", "recovered", trusted_archive=True)
    assert result["target_database"] == "recovered"
    assert result["database_validated"] is True
    assert result["application_validated"] is False
    assert not list(fresh.state.glob("database-restore-*"))


def test_restore_refuses_existing_and_overlapping(service, tmp_path):
    for target in (tmp_path / "data", tmp_path / "data" / "restored", tmp_path / "state" / "restored"):
        with pytest.raises(ServiceError):
            service.restore("local", "a" * 64, target)
    link = tmp_path / "symlink"
    link.symlink_to(tmp_path / "data")
    with pytest.raises(ServiceError):
        service.restore("local", "a" * 64, link / "restore")


def test_copy_requires_success_receipt(service):
    with pytest.raises(ServiceError, match="recorded successful"):
        service.copy("local", "copy", "a" * 64)


def test_legacy_snapshot_receipt_does_not_guess_missing_capture_token(service, tmp_path):
    state = tmp_path / "legacy-state"
    state.mkdir(mode=0o700)
    db_path = state / "snapshots.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE snapshots (repository TEXT, snapshot TEXT, job TEXT, operation TEXT, PRIMARY KEY(repository,snapshot))"
        )
        db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
            ("local", "a" * 64, "daily", "old-operation"),
        )
        db.execute(
            """CREATE TABLE copies (
                source_repository TEXT, source_snapshot TEXT,
                destination_repository TEXT, destination_snapshot TEXT,
                operation TEXT,
                PRIMARY KEY(source_repository, source_snapshot, destination_repository)
            )"""
        )

    migrated = BackupService(
        service.config,
        HostBindings(state, service.bindings.repositories),
    )
    with migrated._receipts() as db:
        row = db.execute("SELECT capture_token FROM snapshots").fetchone()
    assert row == (None,)
    with pytest.raises(ServiceError, match="recoverable capture token"):
        migrated.copy("local", "copy", "a" * 64)


def test_capture_receipt_is_committed_before_ledger_success(service, monkeypatch):
    snapshot_id = "a" * 64
    plans = []

    def fake_run(plan, **_kwargs):
        plans.append(plan)
        capture_tag = next(
            argument for argument in plan.argv if argument.startswith("bbackup-capture=")
        )
        assert capture_tag == f"bbackup-capture={plan.operation_id}"
        output = (f'{{"message_type":"summary","snapshot_id":"{snapshot_id}"}}\n').encode()
        return _run(plan, stdout=output)

    monkeypatch.setattr(service.runner, "run", fake_run)
    with service._receipts() as db:
        db.execute(
            """CREATE TRIGGER reject_snapshot BEFORE INSERT ON snapshots
               BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        service.capture("daily")

    assert len(plans) == 1
    events = service.ledger.events(plans[0].operation_id)
    assert [event.kind.value for event in events] == ["started", "uncertain"]
    with service._receipts() as db:
        assert db.execute("SELECT 1 FROM snapshots").fetchone() is None


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(OperationStatus.CANCELLED, -15), (OperationStatus.TIMED_OUT, -9)],
)
def test_interrupted_mutator_remains_uncertain(service, monkeypatch, status, exit_code):
    plans = []

    def fake_run(plan, **_kwargs):
        plans.append(plan)
        return _run(plan, status=status, exit_code=exit_code)

    monkeypatch.setattr(service.runner, "run", fake_run)
    with pytest.raises(ServiceError, match="interrupted"):
        service._execute("local", ["unused"], label="capture", mutating=True)

    assert [event.kind.value for event in service.ledger.events(plans[0].operation_id)] == [
        "started",
        "uncertain",
    ]


def test_second_repository_lock_conflict_does_not_mark_first_uncertain(service, monkeypatch):
    first, second = sorted(("local", "copy"), key=service._identity)
    held_plan = OperationPlan.create(service._identity(second), "held", ("unused",))
    original_begin = service.ledger.begin
    attempted = []

    def recording_begin(plan):
        attempted.append(plan)
        return original_begin(plan)

    monkeypatch.setattr(service.ledger, "begin", recording_begin)
    with original_begin(held_plan) as held:
        with pytest.raises(RepositoryConflictError):
            service._execute(
                first,
                ["unused"],
                label="copy",
                other=(second,),
                mutating=True,
            )
        assert len(attempted) == 2
        first_events = service.ledger.events(attempted[0].operation_id)
        assert [event.kind.value for event in first_events] == ["started", "failed"]
        held.finish(OperationStatus.FAILED, None)


def test_unknown_repository_identity_is_a_service_error(service):
    with pytest.raises(ServiceError, match="Unknown repository"):
        service._identity("missing")
    with pytest.raises(ServiceError, match="Unknown repository"):
        service._execute("missing", ["unused"], label="check")


def test_copy_uses_capture_tag_and_latest_to_bound_large_listing(service, monkeypatch):
    source_snapshot = "a" * 64
    destination_snapshot = "b" * 64
    capture_token = "c" * 32
    with service._receipts() as db:
        db.execute(
            """INSERT INTO snapshots
               (repository, snapshot, job, operation, capture_token)
               VALUES (?, ?, ?, ?, ?)""",
            ("local", source_snapshot, "daily", capture_token, capture_token),
        )

    plans = []
    inspected = []

    def fake_run(plan, **_kwargs):
        plans.append(plan)
        if "copy" in plan.argv:
            assert "bbackup-capture=" + capture_token in plan.argv
            return _run(plan)
        assert "snapshots" in plan.argv
        assert "--tag" in plan.argv
        assert "bbackup-capture=" + capture_token in plan.argv
        latest_at = plan.argv.index("--latest")
        assert plan.argv[latest_at + 1] == "1"
        inspected.append(plan)
        # The selector narrows an otherwise >64 KiB repository history to one row.
        listing = [{"id": destination_snapshot, "original": source_snapshot,
                    "tags": ["bbackup-capture=" + capture_token]}]
        import json
        return _run(plan, stdout=json.dumps(listing).encode())

    monkeypatch.setattr(service.runner, "run", fake_run)
    result = service.copy("local", "copy", source_snapshot)

    assert result["destination_snapshot_id"] == destination_snapshot
    assert len(plans) == 2 and len(inspected) == 1
    with service._receipts() as db:
        row = db.execute(
            "SELECT capture_token FROM copies WHERE source_snapshot = ?",
            (source_snapshot,),
        ).fetchone()
    assert row == (capture_token,)


def test_copy_receipt_failure_leaves_operation_uncertain(service, monkeypatch):
    source_snapshot = "a" * 64
    destination_snapshot = "b" * 64
    capture_token = "c" * 32
    with service._receipts() as db:
        db.execute(
            """INSERT INTO snapshots
               (repository, snapshot, job, operation, capture_token)
               VALUES (?, ?, ?, ?, ?)""",
            ("local", source_snapshot, "daily", capture_token, capture_token),
        )
        db.execute(
            """CREATE TRIGGER reject_copy BEFORE INSERT ON copies
               BEGIN SELECT RAISE(ABORT, 'injected copy receipt failure'); END"""
        )

    plans = []

    def fake_run(plan, **_kwargs):
        plans.append(plan)
        if "copy" in plan.argv:
            return _run(plan)
        import json
        return _run(plan, stdout=json.dumps([{
            "id": destination_snapshot,
            "original": source_snapshot,
            "tags": [f"bbackup-capture={capture_token}"],
        }]).encode())

    monkeypatch.setattr(service.runner, "run", fake_run)
    with pytest.raises(sqlite3.IntegrityError, match="injected copy receipt failure"):
        service.copy("local", "copy", source_snapshot)

    assert [event.kind.value for event in service.ledger.events(plans[0].operation_id)] == [
        "started",
        "uncertain",
    ]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_real_capture_copy_check_restore(service, tmp_path):
    service.initialize("local")
    service.initialize("copy", source="local")
    captured = service.capture("daily")
    assert captured["local_complete"] and not captured["cloud_complete"]
    copied = service.copy("local", "copy", captured["snapshot_id"])
    assert service.copy("local", "copy", captured["snapshot_id"])["destination_snapshot_id"] == copied["destination_snapshot_id"]
    assert copied["source_snapshot_id"] == captured["snapshot_id"]
    assert service.check("copy")["repository_verified"]
    restored = tmp_path / "restored"
    result = service.restore("copy", copied["destination_snapshot_id"], restored)
    assert result["files_verified"] and not result["application_validated"]
    path = restored / str(tmp_path / "data" / "hello.txt").lstrip("/")
    assert path.read_text() == "restore me\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert (path.parent / "link").is_symlink()
    assert len(service.snapshots()) == 1
