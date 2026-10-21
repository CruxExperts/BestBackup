import json
import gzip
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bbackup.models import Configuration, HostBindings, RepositoryBinding, Source, SourceBinding
from bbackup.enrollment import policy_lease, write_bytes
from bbackup.linux_coverage import (CoverageCheck, InventoryPaths, check_coverage,
                                    discover_coverage, review_coverage,
                                    sqlite_file_identity)
from bbackup.operations import (
    OperationPlan,
    OperationRun,
    OperationStatus,
    ReconciliationOutcome,
    RepositoryConflictError,
    repository_key,
)
from bbackup.service import BackupService, ReplicaJobError, ResourceBudgetExceeded, ServiceError


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


def test_covered_capture_requires_enrollment_and_exact_root_mapping(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    password = data / ".credential"
    password.write_text("private fixture credential")
    password.chmod(0o600)
    scope = {"schema_version": 1, "approved_roots": ["data"],
             "require_privileged": False, "filesystem_boundaries": "explicit",
             "metadata_expectations": {name: True for name in
                                       ("numeric_owner", "mode", "acl", "xattr", "links")}}
    config = Configuration.parse({"schema_version": 3,
        "repositories": [{"name": "primary"}],
        "sources": [{"name": "files", "kind": "files"}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["files"],
                  "coverage_policy": scope}]})
    bindings = HostBindings(tmp_path / "state", {
        "primary": RepositoryBinding(str(tmp_path / "repository"), password),
    }, source_bindings={"files": SourceBinding(paths=(str(data),))}, schema_version=3,
        coverage_declarations={"daily": tmp_path / "declaration.json"})
    service = BackupService(config, bindings)
    with pytest.raises(ServiceError, match="exact enrolled"):
        service.capture("daily")
    service = BackupService(config, bindings, policy_bytes=b'{"policy":1}',
                            binding_bytes=b'{"binding":1}')
    calls = []
    def check(_path, _policy, **kwargs):
        calls.append(kwargs)
        return CoverageCheck("complete", (), "a" * 64, "b" * 64, {},
                             (("data", data, "files", "files"),))
    monkeypatch.setattr("bbackup.linux_coverage.check_coverage", check)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, stdout=(json.dumps({"message_type": "summary", "snapshot_id": "a" * 64}) + "\n").encode()))
    assert service.capture("daily")["snapshot_id"] == "a" * 64
    assert calls[0]["policy_bytes"] == b'{"policy":1}'
    monkeypatch.setattr("bbackup.linux_coverage.check_coverage", lambda *_args, **_kwargs:
        CoverageCheck("complete", (), "a" * 64, "b" * 64, {},
                      (("data", tmp_path / "other", "files", "files"),)))
    with pytest.raises(ServiceError, match="root mappings"):
        service.capture("daily")


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_covered_exact_exclusion_is_absent_from_real_restored_snapshot(tmp_path, monkeypatch):
    data = tmp_path / "data[fixture]"
    data.mkdir()
    (data / "keep.txt").write_text("keep")
    excluded = data / "omit*.txt"
    excluded.write_text("excluded secret")
    password = tmp_path / "password"
    password.write_text("fixture-password")
    password.chmod(0o600)
    scope = {"schema_version": 1, "approved_roots": ["data"],
             "require_privileged": False, "filesystem_boundaries": "explicit",
             "metadata_expectations": {name: True for name in
                                       ("numeric_owner", "mode", "acl", "xattr", "links")}}
    config = Configuration.parse({"schema_version": 3,
        "repositories": [{"name": "primary"}],
        "sources": [{"name": "files", "kind": "files"}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["files"],
                  "coverage_policy": scope}]})
    bindings = HostBindings(tmp_path / "state", {
        "primary": RepositoryBinding(str(tmp_path / "repository"), password),
    }, source_bindings={"files": SourceBinding(paths=(str(data),))}, schema_version=3,
        coverage_declarations={"daily": tmp_path / "declaration.json"})
    service = BackupService(config, bindings, policy_bytes=b'{"policy":1}',
                            binding_bytes=b'{"binding":1}')
    monkeypatch.setattr("bbackup.linux_coverage.check_coverage", lambda *_args, **_kwargs:
        CoverageCheck("complete", (), "a" * 64, "b" * 64, {},
                      (("data", data, "files", "files"),), (excluded,)))
    service.initialize("primary")
    snapshot = service.capture("daily")["snapshot_id"]
    restored = tmp_path / "restored"
    service.restore("primary", snapshot, restored)
    tree = restored / str(data).lstrip("/")
    assert (tree / "keep.txt").read_text() == "keep"
    assert not (tree / "omit*.txt").exists()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_covered_sqlite_export_restores_with_reviewed_metadata_recipe(tmp_path, monkeypatch):
    data = tmp_path / "data[fixture]"
    data.mkdir()
    (data / "keep.txt").write_text("sibling survives\n")
    database_path = data / "database [review]*?.sqlite"
    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE committed (value TEXT)")
    writer.execute("INSERT INTO committed VALUES ('committed row')")
    writer.commit()
    writer.execute("INSERT INTO committed VALUES ('uncommitted row')")
    wal_path = Path(str(database_path) + "-wal")
    assert wal_path.is_file()
    try:
        os.setxattr(database_path, "user.coverage", b"restore-me")
    except (AttributeError, OSError):
        writer.close()
        pytest.skip("Fixture filesystem cannot store SQLite xattrs")
    source_identity = sqlite_file_identity(database_path)
    source_metadata_json = json.dumps(source_identity, sort_keys=True)

    def write_private(path, value):
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        return path

    def recipe(name, procedure_text):
        procedure = tmp_path / f"{name}.py"
        procedure.write_text(procedure_text, encoding="utf-8")
        procedure.chmod(0o600)
        digest = hashlib.sha256(procedure.read_bytes()).hexdigest()
        evidence = write_private(tmp_path / f"{name}.evidence.json", {
            "schema_version": 1, "recipe_id": name, "recipe_sha256": digest,
            "environment": "isolated-local-fixture",
            "checks": {"procedure_executed": True, "result_verified": True},
        })
        return ({"reason": "rebuild covered state", "consequence": "run on recovery",
                 "recipe_id": name, "recipe_sha256": digest},
                {"recipe_id": name, "recipe_sha256": digest,
                 "procedure_path": str(procedure), "evidence_path": str(evidence),
                 "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()},
                procedure)

    metadata_recipe_body = (
        "import base64, json, os, sys\n"
        f"metadata = json.loads({source_metadata_json!r})\n"
        "path = sys.argv[1]\n"
        "os.chown(path, metadata['uid'], metadata['gid'])\n"
        "os.chmod(path, metadata['mode'])\n"
        "for item in metadata['xattrs']:\n"
        "    os.setxattr(path, item['name'], base64.b64decode(item['value_base64']))\n"
    )
    app_recipe, app_evidence, recipe_path = recipe("sqlite_rebuild", metadata_recipe_body)
    package_recipe, package_evidence, _ = recipe(
        "package_rebuild", "from pathlib import Path\nPath(__file__).read_text()\n")
    inventory_root = tmp_path / "inventory"
    inventory_root.mkdir()
    passwd = inventory_root / "passwd"
    passwd.write_text(f"fixture:x:1000:1000::{data}:/bin/sh\n")
    group = inventory_root / "group"
    group.write_text("fixture:x:1000:\n")
    mountinfo = inventory_root / "mountinfo"
    mountinfo.write_text("1 1 0:1 / / rw - ext4 /dev/fixture rw\n")
    packages = inventory_root / "packages"
    packages.write_text("Package: fixture\n")
    units = inventory_root / "units"
    units.mkdir()
    (units / "fixture.service").write_text("[Service]\n")
    inventory = InventoryPaths(passwd, group, mountinfo, packages, units)
    scope = {"schema_version": 1, "approved_roots": ["data", "database"],
             "require_privileged": False, "filesystem_boundaries": "explicit",
             "metadata_expectations": {name: True for name in
                                       ("numeric_owner", "mode", "acl", "xattr", "links")}}
    declaration = write_private(tmp_path / "declaration.json", {
        "schema_version": 1,
        "roots": [{"id": "data", "path": str(data), "source_id": "files", "adapter": "files"},
                  {"id": "database", "path": str(database_path), "source_id": "db", "adapter": "sqlite"}],
        "exclusions": [], "mount_decisions": [], "identity_decisions": [],
        "applications": [{"id": "fixture", "unit": "fixture", "source_id": "db",
                           "state_roots": [str(database_path)], "startup_recipe": app_recipe}],
        "package_reconstruction": package_recipe,
        "reconstruction_evidence": [app_evidence, package_evidence],
    })
    policy_bytes, binding_bytes = b'{"policy":1}', b'{"binding":1}'
    state = tmp_path / "state"
    write_bytes(state, policy_bytes, binding_bytes)
    with policy_lease(state, create=True):
        pass
    discovered = discover_coverage(declaration, scope, state=state,
        policy_bytes=policy_bytes, binding_bytes=binding_bytes, inventory=inventory)
    assert discovered["coverage_status"] == "complete"
    reviewed = review_coverage(declaration, scope, discovered["candidate_digest"],
        state=state, policy_bytes=policy_bytes, binding_bytes=binding_bytes, inventory=inventory)
    assert reviewed["coverage_status"] == "complete"

    password = tmp_path / "password"
    password.write_text("fixture-password")
    password.chmod(0o600)
    config = Configuration.parse({"schema_version": 3,
        "repositories": [{"name": "primary"}],
        "sources": [{"name": "files", "kind": "files"}, {"name": "db", "kind": "sqlite"}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["files", "db"],
                  "coverage_policy": scope}]})
    bindings = HostBindings(state, {
        "primary": RepositoryBinding(str(tmp_path / "repository"), password),
    }, source_bindings={"files": SourceBinding(paths=(str(data),)),
                        "db": SourceBinding(paths=(str(database_path),))},
        schema_version=3, coverage_declarations={"daily": declaration})
    service = BackupService(config, bindings, policy_bytes=policy_bytes,
                            binding_bytes=binding_bytes)
    real_check = check_coverage
    monkeypatch.setattr("bbackup.linux_coverage.check_coverage",
        lambda declaration_path, policy, **kwargs: real_check(
            declaration_path, policy, inventory=inventory, **kwargs))
    service.initialize("primary")
    snapshot = service.capture("daily")["snapshot_id"]
    writer.rollback()
    writer.close()

    restored = tmp_path / "restored"
    service.restore("primary", snapshot, restored)
    restored_manifest, = restored.rglob("bbackup-capture.json")
    manifest = json.loads(restored_manifest.read_text())
    assert manifest["schema_version"] == 2
    entries = [item for item in manifest["exports"] if item["source"] == "db"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["coverage_root_id"] == "database"
    assert entry["original_path"] == str(database_path)
    assert entry["baseline_digest"] == reviewed["baseline_digest"]
    assert entry["recipe_digest"] == app_recipe["recipe_sha256"]
    assert entry["reviewed_identity"] == source_identity
    export = restored / entry["path"].lstrip("/")
    assert hashlib.sha256(export.read_bytes()).hexdigest() == entry["export_sha256"]
    with sqlite3.connect(export) as database:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM committed").fetchall() == [
            ("committed row",)]
    restored_data = restored / str(data).lstrip("/")
    assert (restored_data / "keep.txt").read_text() == "sibling survives\n"
    restored_raw = restored_data / database_path.name
    assert not restored_raw.exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(restored_raw) + suffix).exists()
    assert not (restored / str(wal_path).lstrip("/")).exists()

    fresh = tmp_path / "fresh-database.sqlite"
    shutil.copyfile(export, fresh)
    subprocess.run([sys.executable, str(recipe_path), str(fresh)], check=True)
    with sqlite3.connect(fresh) as database:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM committed").fetchall() == [
            ("committed row",)]
    reconstructed = sqlite_file_identity(fresh)
    for key in ("uid", "gid", "mode", "links", "xattrs"):
        assert reconstructed[key] == source_identity[key]

    direct_scope = {**scope, "approved_roots": ["data", "wal", "database"]}
    direct_declaration_raw = json.loads(declaration.read_text())
    direct_declaration_raw["roots"].insert(1, {"id": "wal", "path": str(wal_path),
        "source_id": "files", "adapter": "files"})
    direct_declaration = write_private(tmp_path / "direct-wal-declaration.json",
                                       direct_declaration_raw)
    direct_candidate = discover_coverage(direct_declaration, direct_scope, state=state,
        policy_bytes=policy_bytes, binding_bytes=binding_bytes, inventory=inventory)
    assert direct_candidate["coverage_status"] == "incomplete"
    assert "sqlite_artifact_explicitly_mapped" in direct_candidate["reasons"]
    direct_config = Configuration.parse({"schema_version": 3,
        "repositories": [{"name": "primary"}],
        "sources": [{"name": "files", "kind": "files"}, {"name": "db", "kind": "sqlite"}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["files", "db"],
                  "coverage_policy": direct_scope}]})
    direct_bindings = HostBindings(state, {
        "primary": RepositoryBinding(str(tmp_path / "repository"), password),
    }, source_bindings={"files": SourceBinding(paths=(str(data), str(wal_path))),
                        "db": SourceBinding(paths=(str(database_path),))},
        schema_version=3, coverage_declarations={"daily": direct_declaration})
    direct_service = BackupService(direct_config, direct_bindings,
                                   policy_bytes=policy_bytes, binding_bytes=binding_bytes)
    monkeypatch.setattr(direct_service.runner, "run",
                        lambda *_args, **_kwargs: pytest.fail("unsafe WAL source reached Restic"))
    with pytest.raises(ServiceError, match="Covered capture is not ready"):
        direct_service.capture("daily")

    def mismatched_review(*args, **kwargs):
        accepted = real_check(*args, inventory=inventory, **kwargs)
        return replace(accepted, approved_sqlite=())
    monkeypatch.setattr("bbackup.linux_coverage.check_coverage", mismatched_review)
    with pytest.raises(ServiceError, match="SQLite sources differ"):
        service.capture("daily")


def _remote_service(tmp_path, *, with_replica=False):
    data = tmp_path / "large-source"
    with data.open("wb") as stream:
        stream.truncate(2 * 1024**2)
    password = tmp_path / "password"
    password.write_text("fixture-secret")
    password.chmod(0o600)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    config = Configuration.parse({
        "schema_version": 2,
        "repositories": [{"name": "primary", "role": "remote-primary"},
                         *([{"name": "copy", "role": "replica"}] if with_replica else [])],
        "sources": [{"name": "files", "kind": "files", "paths": [str(data)]}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["files"],
                  "resource_budget": {"schema_version": 1, "staging_max_bytes": 1024**3,
                                      "minimum_free_bytes": 64 * 1024**2,
                                      "cache_max_bytes": 0}}],
    })
    bindings = HostBindings(tmp_path / "state", {
        "primary": RepositoryBinding("s3:https://s3.example.test/bucket/repo", password),
        **({"copy": RepositoryBinding(str(tmp_path / "copy"), password)} if with_replica else {}),
    }, staging_dir=staging)
    return BackupService(config, bindings), data, staging


def _remote_sqlite_service(tmp_path, monkeypatch):
    database_path = tmp_path / "live.sqlite"
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE data(value TEXT)")
        database.execute("INSERT INTO data VALUES ('captured')")
    password = tmp_path / "password"
    password.write_text("fixture-secret")
    password.chmod(0o600)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    config = Configuration.parse({
        "schema_version": 2,
        "repositories": [{"name": "primary", "role": "remote-primary"}],
        "sources": [{"name": "db", "kind": "sqlite", "paths": [str(database_path)]}],
        "jobs": [{"name": "daily", "repository": "primary", "sources": ["db"],
                  "resource_budget": {"schema_version": 1, "staging_max_bytes": 1024**3,
                                      "minimum_free_bytes": 1, "cache_max_bytes": 0}}],
    })
    bindings = HostBindings(tmp_path / "state", {
        "primary": RepositoryBinding("s3:https://s3.example.test/bucket/repo", password),
    }, staging_dir=staging)
    monkeypatch.setattr("bbackup.resource_limits.os.path.ismount", lambda path: path == staging)
    monkeypatch.setattr("bbackup.resource_limits._volume", lambda _path: (1024**3, 1024**3 - 10000))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    return BackupService(config, bindings), database_path, staging


def _retained_capture(service, staging):
    stages = [path for path in staging.iterdir() if path.name.startswith("capture-")]
    assert len(stages) == 1
    token = stages[0].name.removeprefix("capture-")
    record = json.loads((service.state / "capture-artifacts" / f"{token}.json").read_text())
    return token, stages[0], record


def test_remote_failed_upload_keeps_exact_export_after_database_changes(tmp_path, monkeypatch):
    service, database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError, match="retained capture token"):
        service.capture("daily")
    token, stage, record = _retained_capture(service, staging)
    exported = stage / "db.sqlite"
    digest = hashlib.sha256(exported.read_bytes()).hexdigest()
    assert record["exports"][0]["sha256"] == digest
    assert record["exports"][0]["size"] == exported.stat().st_size
    with sqlite3.connect(database_path) as database:
        database.execute("UPDATE data SET value='changed after failure'")
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == digest
    with sqlite3.connect(exported) as database:
        assert database.execute("SELECT value FROM data").fetchone() == ("captured",)
    inspected = []

    def inspect(plan, **_kwargs):
        inspected.append(plan.argv)
        assert "snapshots" in plan.argv
        return _run(plan, stdout=b"[]")

    monkeypatch.setattr(service.runner, "run", inspect)
    result = service.reconcile_run("primary", token, ReconciliationOutcome.FAILED)
    assert result["retained_exports"] is True
    assert exported.exists() and inspected
    monkeypatch.setattr(service.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "retained stage triggered another upload"))
    with pytest.raises(ResourceBudgetExceeded):
        service.capture("daily")


def test_remote_pre_dispatch_failure_retains_completed_export_without_ledger_row(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    service.sources = (*service.sources, Source("missing", "sqlite", (str(tmp_path / "missing"),)))
    original_job = service._job("daily")
    monkeypatch.setattr(service, "_job", lambda _name: replace(original_job, sources=("db", "missing")))
    monkeypatch.setattr(service.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "pre-dispatch failure reached upload"))
    with pytest.raises(ServiceError, match="retained capture token"):
        service.capture("daily")
    token, stage, record = _retained_capture(service, staging)
    assert service.ledger.inspect(repository_key(service._identity("primary")), token) is None
    assert record["exports"][0]["name"] == "db.sqlite"
    assert (stage / "db.sqlite").exists()
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(plan, stdout=b"[]"))
    result = service.reconcile_run("primary", token, ReconciliationOutcome.ABANDONED)
    assert result["retained_exports"] is True and (stage / "db.sqlite").exists()
    listing = service.retained_exports("primary")
    assert listing == [{"repository": "primary", "operation_id": token,
                        "phase": "prepared", "reviewed_outcome": "abandoned",
                        "completed_export_count": 1,
                        "completed_export_bytes": (stage / "db.sqlite").stat().st_size,
                        "artifact_record_sha256": result["artifact_record_sha256"],
                        "file_hashes_verified": False}]
    assert str(stage) not in str(listing)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    disposed = service.discard_exports("primary", token, listing[0]["artifact_record_sha256"])
    assert disposed["discarded_export_count"] == 1
    assert not stage.exists() and service.retained_exports("primary") == []
    assert {path.name for path in staging.iterdir()} == {".bbackup-staging.lock"}


def test_remote_discard_refuses_unknown_outcome_stale_digest_and_foreign_file(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError):
        service.capture("daily")
    token, stage, _record = _retained_capture(service, staging)
    before = service.retained_exports("primary")[0]["artifact_record_sha256"]
    with pytest.raises(ServiceError, match="reconciled"):
        service.discard_exports("primary", token, before)
    assert (stage / "db.sqlite").exists()
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(plan, stdout=b"[]"))
    service.reconcile_run("primary", token, ReconciliationOutcome.FAILED)
    current = service.retained_exports("primary")[0]["artifact_record_sha256"]
    assert current != before
    with pytest.raises(ServiceError, match="refused"):
        service.discard_exports("primary", token, before)
    foreign = stage / "foreign"
    foreign.write_text("keep")
    with pytest.raises(ServiceError, match="refused"):
        service.discard_exports("primary", token, current)
    assert foreign.read_text() == "keep" and (stage / "db.sqlite").exists()
    foreign.unlink()
    service.discard_exports("primary", token, current)
    assert not stage.exists()


def test_remote_restart_orphan_does_not_dispatch_or_discard(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda _plan, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("fixture process lost")))
    with pytest.raises(ServiceError, match="retained capture token"):
        service.capture("daily")
    token, stage, _record = _retained_capture(service, staging)
    restarted = BackupService(service.config, service.bindings)
    monkeypatch.setattr(restarted.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "restart replayed uncertain upload"))
    with pytest.raises(ResourceBudgetExceeded):
        restarted.capture("daily")
    assert restarted.ledger.inspect(repository_key(restarted._identity("primary")), token)[1] is OperationStatus.UNCERTAIN
    assert (stage / "db.sqlite").exists()


def test_remote_receipt_failure_retains_completed_export(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    snapshot = "a" * 64
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, stdout=(json.dumps({"message_type": "summary", "snapshot_id": snapshot}) + "\n").encode()))
    monkeypatch.setattr(service, "_receipts", lambda: (_ for _ in ()).throw(sqlite3.OperationalError(
        "fixture receipt failure")))
    with pytest.raises(ServiceError, match="retained capture token"):
        service.capture("daily")
    _, stage, record = _retained_capture(service, staging)
    assert record["phase"] == "ready"
    assert (stage / "db.sqlite").exists()


def test_remote_success_reconciliation_repairs_receipt_and_cleans_exact_stage(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError):
        service.capture("daily")
    token, stage, record = _retained_capture(service, staging)
    snapshot = "b" * 64
    manifest = (stage / record["manifest"]["name"]).read_bytes()
    exported = (stage / "db.sqlite").read_bytes()
    inspected = []

    def inspect(plan, **kwargs):
        inspected.append(plan.argv)
        if "snapshots" in plan.argv:
            return _run(plan, stdout=json.dumps([{"id": snapshot,
                "tags": [f"bbackup-capture={token}", "bbackup-job=daily"]}]).encode())
        assert "dump" in plan.argv
        payload = exported if plan.argv[-1].endswith("db.sqlite") else manifest
        kwargs["on_stdout_chunk"](payload)
        return replace(_run(plan, stdout=payload), stdout_tail=b"")

    monkeypatch.setattr(service.runner, "run", inspect)
    result = service.reconcile_run("primary", token, ReconciliationOutcome.SUCCEEDED)
    assert result["snapshot_id"] == snapshot
    assert {"snapshots", "dump"}.issubset({word for argv in inspected for word in argv})
    assert not stage.exists()
    assert {path.name for path in staging.iterdir()} == {".bbackup-staging.lock"}
    assert service.snapshots()[0]["snapshot_id"] == snapshot
    assert service.ledger.inspect(repository_key(service._identity("primary")), token)[1] is OperationStatus.RECONCILED_SUCCEEDED


@pytest.mark.parametrize("remote_export", ["missing", "changed", "oversize"])
def test_remote_success_reconciliation_requires_each_export_bytes(tmp_path, monkeypatch, remote_export):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError):
        service.capture("daily")
    token, stage, record = _retained_capture(service, staging)
    snapshot = "d" * 64
    manifest = (stage / record["manifest"]["name"]).read_bytes()
    exported = (stage / "db.sqlite").read_bytes()

    def inspect(plan, **kwargs):
        if "snapshots" in plan.argv:
            return _run(plan, stdout=json.dumps([{"id": snapshot,
                "tags": [f"bbackup-capture={token}", "bbackup-job=daily"]}]).encode())
        if plan.argv[-1].endswith("db.sqlite"):
            if remote_export == "missing":
                return _run(plan, status=OperationStatus.FAILED, exit_code=1)
            payload = (exported + b"extra" if remote_export == "oversize"
                       else bytes([exported[0] ^ 1]) + exported[1:])
        else:
            payload = manifest
        kwargs["on_stdout_chunk"](payload)
        return replace(_run(plan, stdout=payload), stdout_tail=b"")

    monkeypatch.setattr(service.runner, "run", inspect)
    with pytest.raises(ServiceError, match="Remote capture export"):
        service.reconcile_run("primary", token, ReconciliationOutcome.SUCCEEDED)
    assert (stage / "db.sqlite").exists()
    assert service.snapshots() == []
    assert service.ledger.inspect(repository_key(service._identity("primary")), token)[1] is OperationStatus.UNCERTAIN


@pytest.mark.parametrize("foreign", [False, True])
def test_remote_reconcile_recovers_only_empty_owned_inode_gap(tmp_path, monkeypatch, foreign):
    from bbackup.capture import DurableCaptureArtifacts
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    token = "e" * 32
    artifact = DurableCaptureArtifacts(staging, service.state,
        repository_key(service._identity("primary")), token, "daily")
    artifact._save()
    artifact.directory.mkdir(mode=0o700)
    if foreign:
        (artifact.directory / "unexpected").write_text("keep")
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(plan, stdout=b"[]"))
    if foreign:
        with pytest.raises(ServiceError, match="empty owned stage"):
            service.reconcile_run("primary", token, ReconciliationOutcome.ABANDONED)
        assert (artifact.directory / "unexpected").read_text() == "keep"
        assert json.loads(artifact.record_path.read_text())["stage_inode"] is None
    else:
        result = service.reconcile_run("primary", token, ReconciliationOutcome.ABANDONED)
        assert result["retained_exports"] is True
        assert json.loads(artifact.record_path.read_text())["stage_inode"] == artifact.directory.stat().st_ino
        service.discard_exports("primary", token, result["artifact_record_sha256"])
        assert not artifact.directory.exists()


def test_legacy_remote_capture_reconciles_without_record_but_marked_new_refuses(tmp_path, monkeypatch):
    service, _database_path, _staging = _remote_sqlite_service(tmp_path, monkeypatch)
    legacy = OperationPlan.create(service._identity("primary"), "capture", ["never-run"])
    with service.ledger.begin(legacy):
        pass
    monkeypatch.setattr(service.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "record-less legacy reconcile dispatched a command"))
    result = service.reconcile_run("primary", legacy.operation_id, ReconciliationOutcome.ABANDONED)
    assert result["legacy_capture"] is True
    marked = OperationPlan.create(service._identity("primary"), "capture", ["never-run"])
    service.ledger.mark_capture_artifacts(marked.repository_id, marked.operation_id)
    with service.ledger.begin(marked):
        pass
    with pytest.raises(ServiceError, match="New remote capture artifact record is missing"):
        service.reconcile_run("primary", marked.operation_id, ReconciliationOutcome.ABANDONED)
    assert service.ledger.inspect(marked.repository_id, marked.operation_id)[1] is OperationStatus.UNCERTAIN


def test_remote_success_cleans_only_recorded_stage(tmp_path, monkeypatch):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    snapshot = "c" * 64
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, stdout=(json.dumps({"message_type": "summary", "snapshot_id": snapshot}) + "\n").encode()))
    response = service.capture("daily")
    record = json.loads((service.state / "capture-artifacts" /
                         f"{response['operation_id']}.json").read_text())
    assert record["phase"] == "cleaned" and record["exports"][0]["sha256"]
    assert {path.name for path in staging.iterdir()} == {".bbackup-staging.lock"}
    assert service.snapshots()[0]["snapshot_id"] == snapshot
    from bbackup.capture import DurableCaptureArtifacts
    token = response["operation_id"]
    artifact = DurableCaptureArtifacts.load(staging, service.state,
        repository_key(service._identity("primary")), token)
    with pytest.raises(ServiceError, match="reconciled"):
        service.discard_exports("primary", token, artifact.record_sha256())


@pytest.mark.parametrize("foreign", ["file", "symlink"])
def test_remote_reconciliation_rejects_foreign_stage_entry(tmp_path, monkeypatch, foreign):
    service, _database_path, staging = _remote_sqlite_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.runner, "run", lambda plan, **_kwargs: _run(
        plan, status=OperationStatus.FAILED, exit_code=1))
    with pytest.raises(ServiceError):
        service.capture("daily")
    token, stage, _record = _retained_capture(service, staging)
    extra = stage / "unexpected"
    if foreign == "file":
        extra.write_text("foreign")
    else:
        extra.symlink_to(tmp_path / "outside")
    monkeypatch.setattr(service.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "unsafe stage reached remote inspection"))
    with pytest.raises(ServiceError, match="artifact"):
        service.reconcile_run("primary", token, ReconciliationOutcome.FAILED)
    assert extra.is_symlink() if foreign == "symlink" else extra.exists()
    assert (stage / "db.sqlite").exists()


def test_remote_primary_capture_direct_source_no_cache_and_explicit_provider_env(tmp_path, monkeypatch):
    service, data, staging = _remote_service(tmp_path)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    monkeypatch.setenv("RESTIC_REPOSITORY", "poisoned")
    monkeypatch.setenv("AWS_PROFILE", "poisoned")
    monkeypatch.setattr("bbackup.service.shutil.disk_usage", lambda _path: SimpleNamespace(free=1))

    class Monitor:
        def __init__(self, *_args):
            self.checks = 0

        def check(self, _upcoming=0):
            self.checks += 1

        def result(self):
            return {"staging_capacity_bounded": True, "free_space_reserve_hard_guaranteed": False}

        def close(self):
            pass

    monkeypatch.setattr("bbackup.service.ResourceMonitor", Monitor)
    observed = {}

    def run(plan, **kwargs):
        observed["argv"] = plan.argv
        observed["env"] = plan.env
        observed["tick"] = kwargs.get("on_tick")
        kwargs["on_tick"]()
        return _run(plan, stdout=(json.dumps({"message_type": "summary", "snapshot_id": "a" * 64}) + "\n").encode())

    monkeypatch.setattr(service.runner, "run", run)
    captured = service.capture("daily")
    assert captured["primary_destination"] == {"repository": "primary", "kind": "remote"}
    assert captured["cloud_complete"] and not captured["local_complete"]
    assert captured["resource_observation"]["staging_capacity_bounded"]
    assert "--no-cache" in observed["argv"]
    assert str(data) in observed["argv"]
    assert observed["env"]["TMPDIR"] == str(staging)
    assert "RESTIC_REPOSITORY" not in observed["env"]
    assert "AWS_PROFILE" not in observed["env"]
    assert observed["env"]["AWS_ACCESS_KEY_ID"] == "fixture-key"
    assert list(staging.iterdir()) == []


def test_manual_remote_primary_copy_is_refused_before_any_dispatch(tmp_path, monkeypatch):
    service, _data, _staging = _remote_service(tmp_path, with_replica=True)
    monkeypatch.setattr(service.runner, "run", lambda *_args, **_kwargs: pytest.fail(
        "unbudgeted copy child was dispatched"))
    with pytest.raises(ServiceError, match="bounded resource support"):
        service.copy("primary", "copy", "a" * 64)


def test_remote_capture_retains_real_staging_lease_until_receipt_finalization(tmp_path, monkeypatch):
    from bbackup.resource_limits import ResourceMonitor

    service, _data, staging = _remote_service(tmp_path)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    monkeypatch.setattr("bbackup.resource_limits.os.path.ismount", lambda path: path == staging)
    budget = service.config.jobs[0].resource_budget
    monkeypatch.setattr("bbackup.resource_limits._volume", lambda path: (
        budget.staging_max_bytes, budget.staging_max_bytes))

    def run(plan, **kwargs):
        kwargs["on_tick"]()
        return _run(plan, stdout=(json.dumps({"message_type": "summary", "snapshot_id": "b" * 64}) + "\n").encode())

    monkeypatch.setattr(service.runner, "run", run)
    captured = service.capture("daily")
    assert captured["snapshot_id"] == "b" * 64
    assert any(row["status"] == "succeeded" for row in service.ledger.summary())
    # Capture completed and released the shared lease; its stable lock remains.
    with ResourceMonitor(staging, service.state, budget) as next_run:
        next_run.check()


def test_remote_primary_budget_failure_is_structured_and_uncertain_after_dispatch(tmp_path, monkeypatch):
    from bbackup.resource_limits import ResourceLimitError
    service, _data, _staging = _remote_service(tmp_path)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")

    class Monitor:
        calls = 0

        def __init__(self, *_args):
            pass

        def check(self, _upcoming=0):
            self.calls += 1
            if self.calls > 1:
                raise ResourceLimitError("Observed free-space reserve fell below the configured threshold")

        def result(self):
            return {"staging_capacity_bounded": True}

        def close(self):
            pass

    monkeypatch.setattr("bbackup.service.ResourceMonitor", Monitor)

    def run(_plan, **kwargs):
        kwargs["on_tick"]()

    monkeypatch.setattr(service.runner, "run", run)
    with pytest.raises(ResourceBudgetExceeded) as failure:
        service.capture("daily")
    assert failure.value.result["resource_budget_exceeded"] is True
    assert failure.value.result["local_complete"] is False
    assert failure.value.result["cloud_complete"] is False
    assert "large-source" not in str(failure.value.result)
    assert any(row["status"] == "uncertain" for row in service.ledger.summary())


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


def test_indefinite_checkpoint_receipt_and_legacy_expiry_migration(service, tmp_path):
    columns = ("repository", "repository_identity", "snapshot", "operation",
               "manifest_sha256", "captured_at", "expires_at")
    prior = ("local", "identity", "a" * 64, "old-checkpoint", "b" * 64,
             "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    with service._receipts() as db:
        db.execute("DROP TABLE checkpoints")
        db.execute(
            """CREATE TABLE checkpoints (
                repository TEXT NOT NULL, repository_identity TEXT NOT NULL,
                snapshot TEXT NOT NULL, operation TEXT PRIMARY KEY,
                manifest_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)", prior)
    migrated = BackupService(service.config, service.bindings)
    with migrated._receipts() as db:
        assert db.execute("SELECT * FROM checkpoints").fetchall() == [prior]
        assert db.execute("PRAGMA table_info(checkpoints)").fetchall()[-1][3] == 0
        indefinite = ("local", "identity", "c" * 64, "indefinite-checkpoint",
                      "d" * 64, "2026-10-02T00:00:00Z", None)
        db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)", indefinite)
    archive, = (service.state / "state-backups").glob("receipts-*.sqlite3.gz")
    recovered = tmp_path / "checkpoint-pre-migration.sqlite3"
    with gzip.GzipFile(filename=archive, mode="rb") as incoming, recovered.open("xb") as output:
        shutil.copyfileobj(incoming, output)
    with sqlite3.connect(recovered) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert tuple(row[1] for row in db.execute("PRAGMA table_info(checkpoints)")) == columns
        assert db.execute("PRAGMA table_info(checkpoints)").fetchall()[-1][3] == 1
        assert db.execute("SELECT * FROM checkpoints").fetchall() == [prior]
    again = BackupService(service.config, service.bindings)
    with again._receipts() as db:
        assert db.execute("SELECT * FROM checkpoints ORDER BY operation").fetchall() == [indefinite, prior]
    assert len(list((service.state / "state-backups").glob("receipts-*.sqlite3.gz"))) == 1


@pytest.mark.parametrize("extra", [
    "repository TEXT NOT NULL COLLATE NOCASE",
    "repository TEXT NOT NULL CHECK(length(repository) > 0)",
    "repository TEXT NOT NULL DEFAULT 'local'",
    "repository TEXT NOT NULL UNIQUE",
])
def test_checkpoint_expiry_migration_refuses_extra_constraints(service, extra):
    ddl = f"""CREATE TABLE checkpoints (
        {extra}, repository_identity TEXT NOT NULL,
        snapshot TEXT NOT NULL, operation TEXT PRIMARY KEY,
        manifest_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )"""
    prior = ("local", "identity", "a" * 64, "original-operation",
             "b" * 64, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    with service._receipts() as db:
        db.execute("DROP TABLE checkpoints")
        db.execute(ddl)
        db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)", prior)
        original_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='checkpoints'"
        ).fetchone()[0]
    with pytest.raises(ServiceError, match="unsupported constraints"):
        BackupService(service.config, service.bindings)
    with service._receipts() as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='checkpoints'").fetchone()[0] == original_sql
        assert db.execute("SELECT * FROM checkpoints").fetchall() == [prior]
        assert db.execute("PRAGMA table_info(checkpoints)").fetchall()[-1][3] == 1
    assert not (service.state / "state-backups").exists()


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
