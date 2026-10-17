import json
import gzip
import shutil
import sqlite3
from dataclasses import replace

import pytest

from bbackup.models import Configuration, HostBindings, RepositoryBinding
from bbackup.operations import (
    OperationPlan,
    OperationRun,
    OperationStatus,
    RepositoryConflictError,
    repository_key,
)
from bbackup.service import BackupService, ReplicaJobError, ServiceError


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


def test_replica_failure_isolated_and_later_destination_runs(service, monkeypatch):
    job = replace(service.config.jobs[0], replicas=("first", "second"))
    monkeypatch.setattr(service, "_job", lambda _name: job)
    monkeypatch.setattr(service, "capture", lambda *_args, **_kwargs: {
        "operation_id": "capture", "repository": "local", "snapshot_id": "a" * 64,
        "local_complete": True, "cloud_complete": False,
        "required_replicas": ["first", "second"],
    })
    called = []

    def copy(_source, destination, _snapshot, **_kwargs):
        called.append(destination)
        if destination == "first":
            raise ServiceError("failed")
        return {"destination_repository": destination, "operation_id": "second-copy"}

    monkeypatch.setattr(service, "copy", copy)
    with pytest.raises(ReplicaJobError) as error:
        service.run_job("daily")
    assert called == ["first", "second"]
    assert error.value.result["local_complete"] is True
    assert error.value.result["replicas"] == [{"destination_repository": "second", "operation_id": "second-copy"}]
    assert error.value.result["replica_failures"] == [
        {"destination_repository": "first", "reason": "copy_failed_or_uncertain"}]
    assert error.value.result["cloud_complete"] is False


def test_cancellation_stops_later_replica_dispatch(service, monkeypatch):
    import threading
    cancel = threading.Event()
    job = replace(service.config.jobs[0], replicas=("first", "second"))
    monkeypatch.setattr(service, "_job", lambda _name: job)
    monkeypatch.setattr(service, "capture", lambda *_args, **_kwargs: {
        "operation_id": "capture", "repository": "local", "snapshot_id": "a" * 64,
        "local_complete": True, "cloud_complete": False,
        "required_replicas": ["first", "second"],
    })
    called = []

    def copy(_source, destination, _snapshot, **_kwargs):
        called.append(destination)
        cancel.set()
        raise ServiceError("cancelled")

    monkeypatch.setattr(service, "copy", copy)
    with pytest.raises(ReplicaJobError) as error:
        service.run_job("daily", cancel=cancel)
    assert called == ["first"]
    assert error.value.result["cloud_complete"] is False


def test_copy_failure_leaves_only_destination_uncertain(service, monkeypatch):
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError):
        service._execute("copy", ("restic", "copy"), label="copy", other=("local",), mutating=True)
    statuses = {(row["repository_id"], row["status"]) for row in service.ledger.summary()}
    assert (repository_key(service._identity("local")), "failed") in statuses
    assert (repository_key(service._identity("copy")), "uncertain") in statuses


def test_job_timeout_reaches_source_export_stage(service, monkeypatch):
    service.config = replace(service.config, jobs=(
        replace(service.config.jobs[0], timeout_seconds=73),))
    observed = []

    def capture_sources(*_args, **kwargs):
        observed.append(kwargs["timeout"])
        raise RuntimeError("stop before export")

    monkeypatch.setattr("bbackup.service.capture_sources", capture_sources)
    with pytest.raises(RuntimeError, match="stop before export"):
        service.capture("daily")
    assert observed == [73]


@pytest.mark.integration
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
        row = db.execute(
            "SELECT capture_token, required_replicas FROM snapshots"
        ).fetchone()
    assert row == (None, None)
    with pytest.raises(ServiceError, match="recoverable capture token"):
        migrated.copy("local", "copy", "a" * 64)
    archive, = (state / "state-backups").glob("receipts-*.sqlite3.gz")
    assert archive.stat().st_mode & 0o777 == 0o600
    recovered = tmp_path / "pre-migration.sqlite3"
    with gzip.GzipFile(filename=archive, mode="rb") as incoming, recovered.open("xb") as output:
        shutil.copyfileobj(incoming, output)
    with sqlite3.connect(recovered) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert {row[1] for row in db.execute("PRAGMA table_info(snapshots)")} == {
            "repository", "snapshot", "job", "operation",
        }
        assert db.execute("SELECT * FROM snapshots").fetchall() == [
            ("local", "a" * 64, "daily", "old-operation"),
        ]


def test_receipt_upgrade_refuses_nonprivate_backup_without_migrating(service, tmp_path):
    state = tmp_path / "unbacked-state"
    state.mkdir(mode=0o700)
    with sqlite3.connect(state / "snapshots.sqlite3") as db:
        db.execute("CREATE TABLE snapshots (repository TEXT, snapshot TEXT, job TEXT, operation TEXT)")
        db.execute("INSERT INTO snapshots VALUES ('local', 'saved-id', 'daily', 'saved-operation')")
    backup = state / "state-backups"
    backup.mkdir()
    backup.chmod(0o755)
    with pytest.raises(ServiceError, match="backup directory must be private"):
        BackupService(service.config, HostBindings(state, service.bindings.repositories))
    with sqlite3.connect(state / "snapshots.sqlite3") as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(snapshots)")} == {
            "repository", "snapshot", "job", "operation",
        }
        assert db.execute("SELECT * FROM snapshots").fetchall() == [
            ("local", "saved-id", "daily", "saved-operation"),
        ]


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


def test_docker_capture_failure_restarts_before_refusing_success_receipt(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from bbackup.models import DockerBinding

    data = tmp_path / "docker-data"
    password = tmp_path / "docker-password"
    password.write_text("docker-password")
    password.chmod(0o600)
    data.mkdir()
    config = Configuration.parse({
        "schema_version": 2,
        "repositories": [{"name": "local"}],
        "sources": [{
            "name": "app", "kind": "docker", "container": "old-app",
            "connection": "local-docker", "stop_policy": "stop-restart",
        }],
        "jobs": [{"name": "daily", "repository": "local", "sources": ["app"]}],
    })
    capture_service = BackupService(
        config,
        HostBindings(
            tmp_path / "docker-state",
            {"local": RepositoryBinding(str(tmp_path / "docker-repo"), password)},
            docker_bindings={"local-docker": DockerBinding("docker", tmp_path / "docker.sock")},
        ),
    )
    state = {"running": True}

    class FakeDockerRuntime:
        @contextmanager
        def capture(self, *_args, **_kwargs):
            state["running"] = False
            try:
                yield ((str(data),), ())
            finally:
                state["running"] = True

    capture_service.docker_runtime = FakeDockerRuntime()
    plans = []

    def failed_restic(plan, **_kwargs):
        plans.append(plan)
        assert state["running"] is False
        return _run(plan, status=OperationStatus.FAILED, exit_code=1)

    monkeypatch.setattr(capture_service.runner, "run", failed_restic)
    with pytest.raises(ServiceError, match="interrupted"):
        capture_service.capture("daily")

    assert state["running"] is True
    assert [event.kind.value for event in capture_service.ledger.events(plans[0].operation_id)] == [
        "started", "uncertain",
    ]
    with capture_service._receipts() as db:
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


def _retention_snapshot(snapshot_id, tags=None):
    return {
        "id": snapshot_id,
        "time": "2026-01-01T00:00:00Z",
        "hostname": "test-host",
        "tags": ["bbackup-job=daily"] if tags is None else tags,
        "paths": ["/data"],
    }


def _record_retention_source(service, snapshot_id, *, required=("copy",), capture_token="capture-token",
                            job="daily"):
    evidence = None if required is None else json.dumps({"schema_version": 1, "bindings": {
        name: repository_key(service._identity(name)) for name in required
    }}, sort_keys=True, separators=(",", ":"))
    with service._receipts() as db:
        db.execute(
            """INSERT INTO snapshots
               (repository, snapshot, job, operation, capture_token, required_replicas)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("local", snapshot_id, job, "capture-operation", capture_token, evidence),
        )


def _record_retention_copy(service, snapshot_id, *, destination="copy", capture_token="capture-token"):
    with service._receipts() as db:
        db.execute(
            """INSERT INTO copies
               (source_repository, source_snapshot, destination_repository,
                destination_snapshot, operation, capture_token, destination_identity)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("local", snapshot_id, destination, "d" * 64, "copy-operation", capture_token,
             repository_key(service._identity(destination))),
        )


def _mock_retention_restic(service, monkeypatch, snapshots, selected, *, apply_status=None):
    state = {"snapshots": list(snapshots), "apply_args": [], "apply_operation_id": None,
             "replica_snapshots": [{"id": "d" * 64, "original": item["id"],
                                    "tags": ["bbackup-capture=capture-token"]} for item in snapshots]}

    def fake_run(plan, **_kwargs):
        argv = list(plan.argv)
        if "snapshots" in argv:
            if plan.repository_id == repository_key(service._identity("copy")):
                return _run(plan, stdout=json.dumps(state["replica_snapshots"]).encode())
            return _run(plan, stdout=json.dumps(state["snapshots"]).encode())
        if "forget" in argv and "--dry-run" in argv:
            payload = [{"remove": [{"id": snapshot_id} for snapshot_id in selected]}]
            return _run(plan, stdout=json.dumps(payload).encode())
        if "forget" in argv:
            state["apply_args"].append(argv)
            state["apply_operation_id"] = plan.operation_id
            if apply_status is not None:
                return _run(plan, status=apply_status, exit_code=-9)
            removed = set(argv[argv.index("--") + 1:])
            state["snapshots"] = [
                item for item in state["snapshots"] if item["id"] not in removed
            ]
        return _run(plan)

    monkeypatch.setattr(service.runner, "run", fake_run)
    return state


def test_capture_keeps_original_replica_obligation_after_job_policy_changes(service, monkeypatch):
    snapshot_id = "e" * 64

    def fake_run(plan, **_kwargs):
        return _run(
            plan,
            stdout=(f'{{"message_type":"summary","snapshot_id":"{snapshot_id}"}}\n').encode(),
        )

    monkeypatch.setattr(service.runner, "run", fake_run)
    service.capture("daily")
    service.config = replace(service.config, jobs=(replace(service.config.jobs[0], replicas=()),))
    _mock_retention_restic(service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id])
    plan = service.retention_plan("daily")
    assert plan["target_snapshot_ids"] == []
    assert plan["protected_snapshots"] == [{
        "snapshot_id": snapshot_id, "reason": "required_copy_pending",
    }]


def test_rebinding_replica_name_cannot_satisfy_original_capture_requirement(service, monkeypatch, tmp_path):
    snapshot_id = "9" * 64
    _record_retention_source(service, snapshot_id)
    repositories = dict(service.bindings.repositories)
    repositories["copy"] = replace(repositories["copy"], repository=str(tmp_path / "different-copy"))
    changed = BackupService(service.config, replace(service.bindings, repositories=repositories))
    _record_retention_copy(changed, snapshot_id)
    state = _mock_retention_restic(changed, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id])
    plan = changed.retention_plan("daily")
    assert plan["target_snapshot_ids"] == []
    assert plan["protected_snapshots"] == [{
        "snapshot_id": snapshot_id, "reason": "missing_required_replica_evidence",
    }]
    changed.retention_apply("daily", plan["plan_token"], True)
    assert state["snapshots"] == [_retention_snapshot(snapshot_id)]


def test_retention_keeps_snapshot_with_pending_required_copy(service, monkeypatch):
    snapshot_id = "a" * 64
    _record_retention_source(service, snapshot_id)
    state = _mock_retention_restic(
        service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id],
    )

    plan = service.retention_plan("daily")
    assert plan["target_snapshot_ids"] == []
    assert plan["protected_snapshots"] == [{
        "snapshot_id": snapshot_id, "reason": "required_copy_pending",
    }]
    applied = service.retention_apply("daily", plan["plan_token"], True)
    assert applied["deleted_snapshot_ids"] == []
    assert state["snapshots"] == [_retention_snapshot(snapshot_id)]
    assert state["apply_args"] == []


def test_retention_refuses_nonlocal_repository_bindings(service):
    repositories = dict(service.bindings.repositories)
    repositories["local"] = RepositoryBinding(
        "rclone:remote:repository", repositories["local"].password_file
    )
    remote = BackupService(
        service.config, replace(service.bindings, repositories=repositories)
    )
    with pytest.raises(ServiceError, match="absolute local repository path"):
        remote.retention_plan("daily")


def test_retention_preserves_foreign_unrecorded_and_legacy_snapshots(service, monkeypatch):
    unrecorded_id, foreign_id, legacy_id = "a" * 64, "b" * 64, "c" * 64
    _record_retention_source(service, foreign_id, required=(), job="other-job")
    _record_retention_source(service, legacy_id, required=None)
    snapshot_ids = [unrecorded_id, foreign_id, legacy_id]
    state = _mock_retention_restic(
        service,
        monkeypatch,
        [_retention_snapshot(snapshot_id) for snapshot_id in snapshot_ids],
        snapshot_ids,
    )

    plan = service.retention_plan("daily")
    assert plan["target_snapshot_ids"] == []
    assert plan["protected_snapshots"] == [
        {"snapshot_id": unrecorded_id, "reason": "unrecorded_or_foreign"},
        {"snapshot_id": foreign_id, "reason": "unrecorded_or_foreign"},
        {"snapshot_id": legacy_id, "reason": "missing_required_replica_evidence"},
    ]
    service.retention_apply("daily", plan["plan_token"], True)
    assert {item["id"] for item in state["snapshots"]} == set(snapshot_ids)
    assert state["apply_args"] == []


def test_retention_policy_change_invalidates_approval(service, monkeypatch):
    snapshot_id = "f" * 64
    _record_retention_source(service, snapshot_id)
    _record_retention_copy(service, snapshot_id)
    state = _mock_retention_restic(
        service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id],
    )
    plan = service.retention_plan("daily")
    job = service.config.jobs[0]
    changed_job = replace(
        job, retention=replace(job.retention, local_daily=job.retention.local_daily + 1)
    )
    service.config = replace(service.config, jobs=(changed_job,))

    with pytest.raises(ServiceError, match="approval is stale"):
        service.retention_apply("daily", plan["plan_token"], True)
    assert state["apply_args"] == []


def test_lost_replica_invalidates_approval_and_holds_remaining_local_snapshot(service, monkeypatch):
    snapshot_id = "7" * 64
    _record_retention_source(service, snapshot_id)
    _record_retention_copy(service, snapshot_id)
    state = _mock_retention_restic(service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id])
    approved = service.retention_plan("daily")
    assert approved["target_snapshot_ids"] == [snapshot_id]
    state["replica_snapshots"] = []
    with pytest.raises(ServiceError):
        service.retention_apply("daily", approved["plan_token"], True)
    current = service.retention_plan("daily")
    assert current["target_snapshot_ids"] == []
    assert current["protected_snapshots"] == [{
        "snapshot_id": snapshot_id, "reason": "required_copy_not_verified",
    }]
    assert state["snapshots"] == [_retention_snapshot(snapshot_id)]
    assert state["apply_args"] == []


def test_retention_rechecks_snapshot_state_under_apply_lease(service, monkeypatch):
    snapshot_id = "1" * 64
    _record_retention_source(service, snapshot_id)
    _record_retention_copy(service, snapshot_id)
    state = _mock_retention_restic(
        service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id],
    )
    original_plan = service.retention_plan
    reviewed = original_plan("daily")

    def change_after_review(job_name, *, cancel=None):
        result = original_plan(job_name, cancel=cancel)
        state["snapshots"].append(_retention_snapshot("2" * 64, tags=[]))
        return result

    monkeypatch.setattr(service, "retention_plan", change_after_review)
    with pytest.raises(ServiceError, match="became stale"):
        service.retention_apply("daily", reviewed["plan_token"], True)
    assert state["apply_args"] == []


def test_interrupted_retention_mutation_remains_uncertain(service, monkeypatch):
    snapshot_id = "3" * 64
    _record_retention_source(service, snapshot_id)
    _record_retention_copy(service, snapshot_id)
    state = _mock_retention_restic(
        service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id],
        apply_status=OperationStatus.TIMED_OUT,
    )
    plan = service.retention_plan("daily")

    with pytest.raises(ServiceError, match="uncertain"):
        service.retention_apply("daily", plan["plan_token"], True)
    events = service.ledger.events(state["apply_operation_id"])
    assert [event.kind.value for event in events] == ["started", "uncertain"]
    with service._receipts() as db:
        assert db.execute(
            "SELECT 1 FROM snapshots WHERE repository='local' AND snapshot=?",
            (snapshot_id,),
        ).fetchone()


def test_retention_forgets_only_reviewed_eligible_snapshot_without_prune(service, monkeypatch):
    snapshot_id = "4" * 64
    _record_retention_source(service, snapshot_id)
    _record_retention_copy(service, snapshot_id)
    state = _mock_retention_restic(
        service, monkeypatch, [_retention_snapshot(snapshot_id)], [snapshot_id],
    )
    plan = service.retention_plan("daily")
    assert plan["target_snapshot_ids"] == [snapshot_id]
    with pytest.raises(ServiceError, match="explicit approval"):
        service.retention_apply("daily", plan["plan_token"], False)
    assert state["apply_args"] == []

    applied = service.retention_apply("daily", plan["plan_token"], True)
    assert applied["deleted_snapshot_ids"] == [snapshot_id]
    assert state["snapshots"] == []
    forget_args = state["apply_args"][0]
    assert forget_args[forget_args.index("--") + 1:] == [snapshot_id]
    assert "--prune" not in forget_args
    with service._receipts() as db:
        assert db.execute(
            "SELECT 1 FROM snapshots WHERE repository='local' AND snapshot=?",
            (snapshot_id,),
        ).fetchone() is None
    events = service.ledger.events(applied["operation_id"])
    assert [event.kind.value for event in events] == ["started", "succeeded"]


def test_docker_reconcile_is_reviewed_ledger_only_and_scoped_to_daemon_and_name(service):
    from bbackup.operations import ReconciliationOutcome

    scope = service._docker_scope("local", "daemon-id", "web-recovered")
    plan = OperationPlan.create(scope, "docker-restore", ["docker", "create", "web-recovered"])
    lease = service.ledger.begin(plan)
    lease.close()  # Simulate a dispatched operation whose outcome was not recorded.
    daemon_checks = []

    class ReadOnlyDockerRuntime:
        def daemon_identity(self, connection):
            daemon_checks.append(connection)
            return "daemon-id"

    service.docker_runtime = ReadOnlyDockerRuntime()
    with pytest.raises(ServiceError, match="explicit review"):
        service.docker_reconcile(
            "local", "web-recovered", plan.operation_id, ReconciliationOutcome.SUCCEEDED, False
        )

    result = service.docker_reconcile(
        "local", "web-recovered", plan.operation_id, ReconciliationOutcome.SUCCEEDED, True
    )

    assert daemon_checks == ["local"]
    assert result["replayed"] is False
    assert [event.kind.value for event in service.ledger.events(plan.operation_id)] == [
        "started", "uncertain", "reconciled_succeeded",
    ]
