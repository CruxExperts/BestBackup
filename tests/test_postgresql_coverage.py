"""Fixture tests for the bounded local PostgreSQL coverage owner."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bbackup.enrollment import policy_lease, write_bytes
from bbackup.linux_coverage import (CoverageError, InventoryPaths, check_coverage,
                                    discover_coverage, review_coverage)
from bbackup.models import DatabaseBinding, Source
from bbackup.operations import OperationStatus
from bbackup.postgresql_coverage import (PostgreSQLCoverageContext,
                                         inspect_postgresql_coverage)


def _private(path: Path, content: bytes | str) -> Path:
    if isinstance(content, str):
        content = content.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _nonconnectable_evidence(databases):
    return [{"name": item["name"], "oid": item["oid"],
             "metadata_sha256": _digest(item),
             "checks": {"procedure_executed": True, "result_verified": True}}
            for item in sorted(databases, key=lambda item: item["name"])
            if not item["allow_connections"]]


class _Runner:
    """A fixture runner with the exact OperationRunner callback contract."""

    def __init__(self, snapshot: dict, system_id: str = "7654321098765432109",
                 local_major: str = "16", *, oversized: bool = False):
        self.snapshot = snapshot
        self.system_id = system_id
        self.local_major = local_major
        self.oversized = oversized
        self.calls = []
        self.per_database = {}

    def run(self, plan, *, on_stdout_chunk=None, cancel_event=None):
        self.calls.append(plan.argv)
        if plan.argv[0] == "psql":
            database = next(arg for arg in plan.argv if arg.startswith("--dbname=")).rsplit("dbname=", 1)[1].strip("'")
            snapshot = {**self.snapshot, **self.per_database.get(database, {}),
                        "current_database": database}
            output = json.dumps(snapshot, sort_keys=True).encode() + b"\n"
        elif plan.argv[0] == "pg_controldata" and plan.argv[1] == "--version":
            output = f"pg_controldata (PostgreSQL) {self.local_major}.3\n".encode()
        elif plan.argv[0] == "pg_controldata":
            output = (f"pg_control version number:            1300\n"
                      f"Database system identifier:           {self.system_id}\n"
                      "Database cluster state:               in production\n").encode()
        else:
            raise AssertionError(f"unexpected native command: {plan.argv[0]}")
        if self.oversized and plan.argv[0] == "psql":
            output = b"x" * (1024 * 1024 + 1)
        if cancel_event is not None and cancel_event.is_set():
            return SimpleNamespace(status=OperationStatus.CANCELLED, exit_code=None,
                                   stdout_bytes=0)
        if on_stdout_chunk is not None:
            for offset in range(0, len(output), 32768):
                on_stdout_chunk(output[offset:offset + 32768])
        return SimpleNamespace(status=OperationStatus.SUCCEEDED, exit_code=0,
                               stdout_bytes=len(output))


def _recipe(directory: Path, name: str) -> tuple[dict, Path, str]:
    procedure = _private(directory / f"{name}.procedure", "restore cluster from reviewed fixture\n")
    digest = hashlib.sha256(procedure.read_bytes()).hexdigest()
    return {"reason": "cluster reconstruction", "consequence": "rebuild cluster before application restore",
            "recipe_id": name, "recipe_sha256": digest}, procedure, digest


def _snapshot(data_directory: Path) -> dict:
    return {
        "system_identifier": "7654321098765432109",
        "server_version_num": 160003,
        "data_directory": str(data_directory),
        "config_file": str(data_directory / "postgresql.conf"),
        "hba_file": str(data_directory / "pg_hba.conf"),
        "ident_file": str(data_directory / "pg_ident.conf"),
        "in_recovery": False,
        "databases": [
            {"name": "app", "oid": 100, "allow_connections": True, "encoding": "UTF8",
             "collate": "C", "ctype": "C", "locale_provider": "c", "owner": "postgres",
             "acl": None, "connection_limit": -1, "is_template": False},
            {"name": "postgres", "oid": 5, "allow_connections": True, "encoding": "UTF8",
             "collate": "C", "ctype": "C", "locale_provider": "c", "owner": "postgres",
             "acl": None, "connection_limit": -1, "is_template": False},
            {"name": "template0", "oid": 4, "allow_connections": False, "encoding": "UTF8",
             "collate": "C", "ctype": "C", "locale_provider": "c", "owner": "postgres",
             "acl": None, "connection_limit": -1, "is_template": True},
            {"name": "template1", "oid": 1, "allow_connections": True, "encoding": "UTF8",
             "collate": "C", "ctype": "C", "locale_provider": "c", "owner": "postgres",
             "acl": None, "connection_limit": -1, "is_template": True},
        ],
        "roles": [
            {"name": "postgres", "superuser": True, "inherit": True, "create_role": True,
             "create_database": True, "can_login": True, "replication": True,
             "bypass_rls": True, "connection_limit": -1, "valid_until": None, "password": None},
            {"name": "backup", "superuser": False, "inherit": True, "create_role": False,
             "create_database": False, "can_login": True, "replication": False,
             "bypass_rls": False, "connection_limit": -1, "valid_until": None, "password": None},
        ],
        "memberships": [],
        "extensions": [{"name": "plpgsql", "version": "1.0"}],
        "languages": [],
        "foreign_tables": 0,
        "foreign_servers": 0,
        "subscriptions": 0,
        "replication_slots": 0,
        "tablespaces": [{"name": "pg_default", "location": ""},
                        {"name": "pg_global", "location": ""}],
        "settings": [["max_connections", "100", "configuration file", "postmaster"]] +
            [[name, value, "default", "postmaster"] for name, value in
             (("ssl", "off"), ("archive_mode", "off"),
              ("shared_preload_libraries", ""), ("session_preload_libraries", ""),
              ("local_preload_libraries", ""), ("restore_command", ""),
              ("archive_command", ""), ("primary_conninfo", ""),
              ("primary_slot_name", ""))],
        "role_settings": [{"database": "", "role": "backup",
                           "settings": ["application_name=private-test-setting"]}],
    }


@pytest.fixture
def pg_fixture(tmp_path):
    data_directory = tmp_path / "cluster"
    data_directory.mkdir(mode=0o700)
    data_directory.chmod(0o700)
    (data_directory / "pg_wal").mkdir()
    (data_directory / "pg_tblspc").mkdir()
    _private(data_directory / "PG_VERSION", "16\n")
    _private(data_directory / "postgresql.conf", "# fixture config\nmax_connections = 100\n")
    _private(data_directory / "pg_hba.conf", "local all all trust\n")
    _private(data_directory / "pg_ident.conf", "# fixture identity map\n")
    service_file = _private(tmp_path / "pg_service.conf", "[local]\nuser=backup\n")
    recovery_dir = tmp_path / "pg_recovery"
    recovery_dir.mkdir(mode=0o700)
    recovery_material = _private(recovery_dir / "roles.sql", "-- fixture roles restore\n")
    material_sha = hashlib.sha256(recovery_material.read_bytes()).hexdigest()
    recipe, procedure, procedure_sha = _recipe(recovery_dir, "pg_cluster_restore")
    evidence_path = recovery_dir / "pg_cluster_restore.evidence.json"
    # Initial evidence is deliberately stale so the fixture can calculate the
    # exact inspection digests before writing a valid reviewed reconstruction record.
    evidence = {"schema_version": 1, "recipe_id": "pg_cluster_restore",
        "recipe_sha256": procedure_sha, "environment": "isolated-local-fixture",
        "cluster_inventory_sha256": "0" * 64, "global_inventory_sha256": "0" * 64,
        "role_inventory_sha256": "0" * 64, "configuration_sha256": "0" * 64,
        "nonconnectable_databases": _nonconnectable_evidence(_snapshot(data_directory)["databases"]),
        "recovery_material": [{"path": str(recovery_material), "sha256": material_sha}],
        "checks": {"procedure_executed": True, "result_verified": True,
                   "quiet_administrative_window": True, "schema_changes_frozen": True}}
    evidence_bytes = json.dumps(evidence, sort_keys=True).encode()
    _private(evidence_path, evidence_bytes)
    declaration = {
        "schema_version": 2,
        "roots": [{"id": "pg_recovery", "path": str(recovery_dir),
                   "source_id": "recovery_source", "adapter": "files"}],
        "database_roots": [{"id": "app_db", "adapter": "postgresql", "source_id": "app_source",
                            "cluster_id": "primary", "connection": "pg_local", "database": "app"}],
        "postgresql_clusters": [{"id": "primary", "connection": "pg_local",
            "data_directory": str(data_directory),
            "consistency_window": {"quiet_administrative_window": True,
                                   "schema_changes_frozen": True},
            "databases": [
                {"name": "app", "oid": 100, "decision": "capture", "root_id": "app_db"},
                {"name": "postgres", "oid": 5, "decision": "reconstruct"},
                {"name": "template0", "oid": 4, "decision": "reconstruct"},
                {"name": "template1", "oid": 1, "decision": "reconstruct"},
            ],
            "reconstruction": {"recipe": recipe, "procedure_path": str(procedure),
                "evidence_path": str(evidence_path),
                "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "recovery_material": [{"path": str(recovery_material), "sha256": material_sha}]}}],
        "exclusions": [], "mount_decisions": [], "identity_decisions": [],
        "applications": [], "package_reconstruction": None, "reconstruction_evidence": [],
    }
    declaration_path = _private(tmp_path / "coverage.json", json.dumps(declaration, sort_keys=True).encode())
    source = Source(name="app_source", kind="postgresql", database="app", connection="pg_local")
    binding = DatabaseBinding("postgresql", service_file=service_file, service_name="local")
    runner = _Runner(_snapshot(data_directory))
    context = PostgreSQLCoverageContext({"app_source": source}, {"pg_local": binding}, runner,
                                        quiet_administrative_window=True)
    inventory_dir = tmp_path / "inventory"
    inventory_dir.mkdir()
    passwd = _private(inventory_dir / "passwd", b"")
    group = _private(inventory_dir / "group", b"")
    mountinfo = _private(inventory_dir / "mountinfo", b"1 1 0:1 / / rw - ext4 /dev/fixture rw\n")
    packages = _private(inventory_dir / "packages", b"Package: fixture\n")
    units = inventory_dir / "units"
    units.mkdir()
    linux_inventory = InventoryPaths(passwd, group, mountinfo, packages, units)
    package_procedure = _private(tmp_path / "package.procedure", "restore fixture packages\n")
    package_sha = hashlib.sha256(package_procedure.read_bytes()).hexdigest()
    package_evidence = {"schema_version": 1, "recipe_id": "package_restore",
        "recipe_sha256": package_sha, "environment": "isolated-local-fixture",
        "checks": {"procedure_executed": True, "result_verified": True}}
    package_evidence_bytes = json.dumps(package_evidence, sort_keys=True).encode()
    package_evidence_path = _private(tmp_path / "package.evidence.json", package_evidence_bytes)
    declaration["package_reconstruction"] = {"reason": "fixture packages",
        "consequence": "reinstall from inventory", "recipe_id": "package_restore",
        "recipe_sha256": package_sha}
    declaration["reconstruction_evidence"] = [{"recipe_id": "package_restore",
        "recipe_sha256": package_sha, "procedure_path": str(package_procedure),
        "evidence_path": str(package_evidence_path),
        "evidence_sha256": hashlib.sha256(package_evidence_bytes).hexdigest()}]
    declaration_path.write_text(json.dumps(declaration, sort_keys=True))
    declaration_path.chmod(0o600)
    state = tmp_path / "state"
    policy_bytes, binding_bytes = b'{"policy":1}', b'{"binding":1}'
    write_bytes(state, policy_bytes, binding_bytes)
    with policy_lease(state, create=True):
        pass
    policy = {"schema_version": 1, "approved_roots": ["app_db", "pg_recovery"],
        "require_privileged": False, "filesystem_boundaries": "explicit",
        "metadata_expectations": {key: True for key in
            ("numeric_owner", "mode", "acl", "xattr", "links")}}
    fixture = {"data_directory": data_directory, "declaration_path": declaration_path,
        "declaration": declaration, "context": context, "runner": runner,
        "policy": policy, "state": state, "policy_bytes": policy_bytes,
        "binding_bytes": binding_bytes, "inventory": linux_inventory,
        "evidence_path": evidence_path, "recovery_material": recovery_material}
    _write_valid_pg_evidence(fixture)
    return fixture


def _write_valid_pg_evidence(fixture):
    declaration = fixture["declaration"]
    pg = declaration["postgresql_clusters"][0]
    inspected = inspect_postgresql_coverage(declaration["database_roots"], [pg], fixture["context"],
                                            declaration["roots"], declaration["exclusions"])
    cluster = inspected["clusters"][0]
    evidence = {"schema_version": 1, "recipe_id": pg["reconstruction"]["recipe"]["recipe_id"],
        "recipe_sha256": pg["reconstruction"]["recipe"]["recipe_sha256"],
        "environment": "isolated-local-fixture",
        "cluster_inventory_sha256": cluster["cluster_inventory_sha256"],
        "global_inventory_sha256": cluster["global_inventory_sha256"],
        "role_inventory_sha256": cluster["role_inventory_sha256"],
        "configuration_sha256": cluster["configuration_sha256"],
        "nonconnectable_databases": _nonconnectable_evidence(cluster["databases"]),
        "recovery_material": [{"path": str(fixture["recovery_material"]),
                               "sha256": hashlib.sha256(fixture["recovery_material"].read_bytes()).hexdigest()}],
        "checks": {"procedure_executed": True, "result_verified": True,
                   "quiet_administrative_window": True, "schema_changes_frozen": True}}
    evidence_bytes = json.dumps(evidence, sort_keys=True).encode()
    _private(fixture["evidence_path"], evidence_bytes)
    pg["reconstruction"]["evidence_sha256"] = hashlib.sha256(evidence_bytes).hexdigest()
    fixture["declaration_path"].write_text(json.dumps(declaration, sort_keys=True))
    fixture["declaration_path"].chmod(0o600)


def _call(function, fixture, *args):
    return function(fixture["declaration_path"], fixture["policy"], *args,
        state=fixture["state"], policy_bytes=fixture["policy_bytes"],
        binding_bytes=fixture["binding_bytes"], inventory=fixture["inventory"],
        postgresql_context=fixture["context"])


def _review(fixture):
    candidate = _call(discover_coverage, fixture)
    reviewed = _call(review_coverage, fixture, candidate["candidate_digest"])
    return candidate, reviewed


def _add_captured_database(fixture, name="analytics"):
    snapshot = fixture["runner"].snapshot
    snapshot["databases"].append({"name": name, "oid": 201,
        "allow_connections": True, "encoding": "UTF8", "collate": "C", "ctype": "C",
        "locale_provider": "c", "owner": "postgres", "acl": None,
        "connection_limit": -1, "is_template": False})
    fixture["declaration"]["database_roots"].append({"id": "analytics_db",
        "adapter": "postgresql", "source_id": "analytics_source", "cluster_id": "primary",
        "connection": "pg_local", "database": name})
    fixture["declaration"]["postgresql_clusters"][0]["databases"].append(
        {"name": name, "oid": 201, "decision": "capture", "root_id": "analytics_db"})
    fixture["policy"]["approved_roots"].append("analytics_db")
    sources = {**fixture["context"].sources, "analytics_source": Source(
        "analytics_source", "postgresql", database=name, connection="pg_local")}
    fixture["context"] = PostgreSQLCoverageContext(sources,
        fixture["context"].database_bindings, fixture["runner"],
        quiet_administrative_window=True)
    fixture["declaration_path"].write_text(json.dumps(fixture["declaration"], sort_keys=True))
    fixture["declaration_path"].chmod(0o600)
    _write_valid_pg_evidence(fixture)


def test_schema2_reviews_logical_source_and_excludes_accounted_pgdata(pg_fixture):
    candidate, reviewed = _review(pg_fixture)
    checked = _call(check_coverage, pg_fixture)
    assert candidate["coverage_status"] == "complete"
    assert checked.capture_ready
    assert reviewed["baseline_digest"] == checked.baseline_digest
    assert checked.approved_roots[0][0] == "pg_recovery"
    assert checked.approved_postgresql[0]["database"] == "app"
    assert checked.approved_postgresql[0]["database_oid"] == 100
    assert checked.postgresql_capture_exclusions == (pg_fixture["data_directory"],)
    assert checked.approved_exclusions == (pg_fixture["data_directory"],)
    serialized = json.dumps(candidate, sort_keys=True)
    assert "private-test-setting" not in serialized
    assert "password" not in serialized.lower()


def test_selector_mismatch_and_unaccounted_database_fail_closed(pg_fixture):
    source = pg_fixture["context"].sources["app_source"]
    pg_fixture["context"] = PostgreSQLCoverageContext(
        {"app_source": Source("app_source", "postgresql", database="other", connection="pg_local")},
        pg_fixture["context"].database_bindings, pg_fixture["runner"],
        quiet_administrative_window=True)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_database_selector_mismatch" in candidate["reasons"]
    pg_fixture["context"] = PostgreSQLCoverageContext({"app_source": source},
        pg_fixture["context"].database_bindings, pg_fixture["runner"],
        quiet_administrative_window=True)
    pg_fixture["runner"].snapshot["databases"].append(
        {"name": "newdb", "oid": 200, "allow_connections": True, "encoding": "UTF8",
         "collate": "C", "ctype": "C", "locale_provider": "c", "owner": "postgres",
         "acl": None, "connection_limit": -1, "is_template": False})
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_database_accounting_changed" in candidate["reasons"]


def test_sql_system_identifier_must_match_local_control_file(pg_fixture):
    pg_fixture["runner"].system_id = "7654321098765432190"
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_sql_local_identity_mismatch" in candidate["reasons"]
    assert candidate["coverage_status"] == "incomplete"


@pytest.mark.parametrize("change,reason", [
    ("remote", "postgresql_remote_endpoint_unsupported"),
    ("translated", "postgresql_data_directory_namespace_mismatch"),
])
def test_remote_connection_and_path_translation_are_rejected(pg_fixture, change, reason):
    if change == "remote":
        service_file = pg_fixture["context"].database_bindings["pg_local"].service_file
        _private(service_file, "[local]\nhost=db.example.invalid\nuser=backup\n")
    else:
        pg_fixture["runner"].snapshot["data_directory"] = str(
            pg_fixture["data_directory"].parent / "translated-cluster")
        pg_fixture["runner"].snapshot["config_file"] = str(pg_fixture["data_directory"] / "postgresql.conf")
        pg_fixture["runner"].snapshot["hba_file"] = str(pg_fixture["data_directory"] / "pg_hba.conf")
        pg_fixture["runner"].snapshot["ident_file"] = str(pg_fixture["data_directory"] / "pg_ident.conf")
    candidate = _call(discover_coverage, pg_fixture)
    assert reason in candidate["reasons"]


def test_changed_connection_material_and_identity_drift(pg_fixture):
    _, reviewed = _review(pg_fixture)
    service_file = pg_fixture["context"].database_bindings["pg_local"].service_file
    _private(service_file, "[local]\nuser=another-local-role\n")
    checked = _call(check_coverage, pg_fixture)
    assert checked.coverage_status == "drift"
    assert checked.baseline_digest == reviewed["baseline_digest"]


def test_reviewed_data_directory_inode_owner_mode_are_baselined(pg_fixture):
    _review(pg_fixture)
    pg_fixture["data_directory"].chmod(0o750)
    checked = _call(check_coverage, pg_fixture)
    assert checked.coverage_status == "drift"


@pytest.mark.parametrize("mutation,reason", [
    (lambda snapshot: snapshot["roles"].append({"name": "new_role", "superuser": False,
        "inherit": True, "create_role": False, "create_database": False, "can_login": True,
        "replication": False, "bypass_rls": False, "connection_limit": -1, "valid_until": None,
        "password": None}),
     "postgresql_reconstruction_evidence_stale"),
    (lambda snapshot: snapshot["settings"].append(["work_mem", "8MB", "configuration file", "user"]),
     "postgresql_reconstruction_evidence_stale"),
    (lambda snapshot: snapshot.update({"extensions": [{"name": "fixture_ext", "version": "1"}]}),
     "postgresql_extensions_unsupported"),
    (lambda snapshot: snapshot.update({"in_recovery": True}), "postgresql_standby_unsupported"),
])
def test_global_config_and_unsupported_state_are_detected(pg_fixture, mutation, reason):
    mutation(pg_fixture["runner"].snapshot)
    candidate = _call(discover_coverage, pg_fixture)
    assert reason in candidate["reasons"]


@pytest.mark.parametrize("mutation", [
    lambda snapshot: snapshot["roles"].append({"name": "new_role", "superuser": False,
        "inherit": True, "create_role": False, "create_database": False, "can_login": True,
        "replication": False, "bypass_rls": False, "connection_limit": -1, "valid_until": None,
        "password": None}),
    lambda snapshot: snapshot["settings"].append(["work_mem", "8MB", "configuration file", "user"]),
    lambda snapshot: snapshot["databases"][0].update({"oid": 101}),
])
def test_reviewed_role_config_and_database_identity_changes_are_drift(pg_fixture, mutation):
    _review(pg_fixture)
    mutation(pg_fixture["runner"].snapshot)
    checked = _call(check_coverage, pg_fixture)
    assert checked.coverage_status == "drift"


def test_changed_recovery_material_invalidates_recipe_evidence(pg_fixture):
    _review(pg_fixture)
    _private(pg_fixture["recovery_material"], "changed material\n")
    checked = _call(check_coverage, pg_fixture)
    assert checked.coverage_status == "drift"
    assert "postgresql_recovery_material_changed" in checked.reasons


def test_malformed_and_oversized_runner_output_fail_closed(pg_fixture):
    pg_fixture["runner"].snapshot = {"unexpected": "shape"}
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_sql_inventory_malformed" in candidate["reasons"]
    pg_fixture["runner"].oversized = True
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_inspection_output_limit" in candidate["reasons"]


def test_external_wal_and_configuration_are_rejected(pg_fixture, tmp_path):
    wal = pg_fixture["data_directory"] / "pg_wal"
    saved_wal = pg_fixture["data_directory"] / "pg_wal.saved"
    wal.rename(saved_wal)
    external_wal = tmp_path / "external-wal"
    external_wal.mkdir()
    wal.symlink_to(external_wal, target_is_directory=True)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_external_wal_unsupported" in candidate["reasons"]

    wal.unlink()
    saved_wal.rename(wal)
    external_config = _private(tmp_path / "external.conf", "max_connections = 100\n")
    pg_fixture["runner"].snapshot["config_file"] = str(external_config)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_external_configuration" in candidate["reasons"]


def test_nested_mount_under_pgdata_is_rejected(pg_fixture):
    nested = pg_fixture["data_directory"] / "pg_wal"
    pg_fixture["inventory"].mountinfo.write_text(
        f"1 1 0:1 / / rw - ext4 /dev/fixture rw\n"
        f"2 1 0:2 / {nested} rw - tmpfs fixture rw\n")
    pg_fixture["inventory"].mountinfo.chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_external_storage_mount_unsupported" in candidate["reasons"]


def test_schema2_requires_all_capture_roots_and_recovery_recipe(pg_fixture):
    declaration = pg_fixture["declaration"]
    declaration["postgresql_clusters"][0]["databases"].pop()
    pg_fixture["declaration_path"].write_text(json.dumps(declaration))
    pg_fixture["declaration_path"].chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_database_accounting_changed" in candidate["reasons"]
    del declaration["postgresql_clusters"][0]["reconstruction"]["recipe"]
    pg_fixture["declaration_path"].write_text(json.dumps(declaration))
    pg_fixture["declaration_path"].chmod(0o600)
    with pytest.raises(CoverageError):
        _call(discover_coverage, pg_fixture)


def test_pgdata_conflicting_raw_root_is_rejected(pg_fixture, tmp_path):
    declaration = pg_fixture["declaration"]
    raw_path = pg_fixture["data_directory"] / "global"
    raw_path.mkdir()
    declaration["roots"].append({"id": "raw", "path": str(raw_path),
                                 "source_id": "raw_source", "adapter": "files"})
    declaration["postgresql_clusters"][0]["data_directory"] = str(pg_fixture["data_directory"])
    pg_fixture["policy"]["approved_roots"] = ["raw", "app_db", "pg_recovery"]
    pg_fixture["declaration_path"].write_text(json.dumps(declaration))
    pg_fixture["declaration_path"].chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_raw_storage_root_conflict" in candidate["reasons"]
    record = json.loads((pg_fixture["state"] / "coverage" / "candidates" /
                         f"{candidate['candidate_digest']}.json").read_text())
    assert record["postgresql_capture_exclusions"] == []


def test_enclosing_files_root_receives_literal_pgdata_exclusion(pg_fixture, tmp_path):
    enclosing = tmp_path / "enclosing"
    enclosing.mkdir()
    new_data_directory = enclosing / "cluster"
    pg_fixture["data_directory"].rename(new_data_directory)
    pg_fixture["data_directory"] = new_data_directory
    declaration = pg_fixture["declaration"]
    cluster = declaration["postgresql_clusters"][0]
    cluster["data_directory"] = str(new_data_directory)
    pg_fixture["runner"].snapshot["data_directory"] = str(new_data_directory)
    pg_fixture["runner"].snapshot["config_file"] = str(new_data_directory / "postgresql.conf")
    pg_fixture["runner"].snapshot["hba_file"] = str(new_data_directory / "pg_hba.conf")
    pg_fixture["runner"].snapshot["ident_file"] = str(new_data_directory / "pg_ident.conf")
    declaration["roots"].append({"id": "outer", "path": str(enclosing),
                                 "source_id": "file_source", "adapter": "files"})
    pg_fixture["policy"]["approved_roots"] = ["outer", "app_db", "pg_recovery"]
    pg_fixture["declaration_path"].write_text(json.dumps(declaration))
    pg_fixture["declaration_path"].chmod(0o600)
    _write_valid_pg_evidence(pg_fixture)
    candidate, _ = _review(pg_fixture)
    checked = _call(check_coverage, pg_fixture)
    assert candidate["coverage_status"] == "complete"
    assert checked.capture_ready
    assert checked.approved_exclusions == (new_data_directory,)


def test_postgresql_inspection_uses_bounded_native_runner(pg_fixture):
    pg_fixture["runner"].calls.clear()
    result = inspect_postgresql_coverage(pg_fixture["declaration"]["database_roots"],
        pg_fixture["declaration"]["postgresql_clusters"], pg_fixture["context"],
        pg_fixture["declaration"]["roots"], pg_fixture["declaration"]["exclusions"])
    assert not result["reasons"]
    assert any(call[0] == "psql" for call in pg_fixture["runner"].calls)
    assert sum(call[0] == "pg_controldata" for call in pg_fixture["runner"].calls) == 2
    query = next(arg for call in pg_fixture["runner"].calls if call[0] == "psql"
                 for arg in call if arg.startswith("--command="))
    assert "(pg_control_system()).system_identifier" in query
    assert "d.oid::bigint" in query
    assert "member_name.oid=m.member" in query
    assert "m.memberid" not in query


def test_every_captured_database_is_inspected_and_second_only_extension_rejected(pg_fixture):
    _add_captured_database(pg_fixture)
    candidate, _ = _review(pg_fixture)
    assert candidate["coverage_status"] == "complete"
    assert len([call for call in pg_fixture["runner"].calls if call[0] == "psql"]) >= 2
    pg_fixture["runner"].per_database["analytics"] = {
        "extensions": [{"name": "plpgsql", "version": "1.0"},
                       {"name": "foreign_ext", "version": "1"}]}
    checked = _call(check_coverage, pg_fixture)
    assert "postgresql_extensions_unsupported" in checked.reasons
    assert checked.coverage_status == "drift"


@pytest.mark.parametrize("database,override,reason", [
    ("postgres", {"extensions": [{"name": "plpgsql", "version": "1.0"},
                                  {"name": "foreign_ext", "version": "1"}]},
     "postgresql_extensions_unsupported"),
    ("template1", {"foreign_servers": 1}, "postgresql_foreign_servers_unsupported"),
])
def test_connectable_reconstructed_database_is_inspected(pg_fixture, database, override, reason):
    pg_fixture["runner"].per_database[database] = override
    candidate = _call(discover_coverage, pg_fixture)
    assert reason in candidate["reasons"]
    inspected = {arg.rsplit("dbname=", 1)[1].strip("'")
                 for call in pg_fixture["runner"].calls if call[0] == "psql"
                 for arg in call if arg.startswith("--dbname=")}
    assert inspected >= {"app", "postgres", "template1"}
    assert "template0" not in inspected


def test_nonconnectable_reconstruction_requires_database_bound_evidence(pg_fixture):
    evidence = json.loads(pg_fixture["evidence_path"].read_text())
    evidence["nonconnectable_databases"] = []
    content = json.dumps(evidence, sort_keys=True).encode()
    _private(pg_fixture["evidence_path"], content)
    pg_fixture["declaration"]["postgresql_clusters"][0]["reconstruction"][
        "evidence_sha256"] = hashlib.sha256(content).hexdigest()
    pg_fixture["declaration_path"].write_text(json.dumps(pg_fixture["declaration"]))
    pg_fixture["declaration_path"].chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_reconstruction_evidence_stale" in candidate["reasons"]


@pytest.mark.parametrize("capture_first", [True, False])
def test_template_decision_order_does_not_change_connectability(pg_fixture, capture_first):
    decisions = pg_fixture["declaration"]["postgresql_clusters"][0]["databases"]
    decisions.sort(key=lambda item: item["decision"] == "capture", reverse=capture_first)
    pg_fixture["declaration_path"].write_text(json.dumps(pg_fixture["declaration"]))
    pg_fixture["declaration_path"].chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_database_not_connectable" not in candidate["reasons"]
    assert candidate["coverage_status"] == "complete"


@pytest.mark.parametrize("file,content,reason", [
    ("postgresql.conf", "include 'nested.conf'\n", "postgresql_external_configuration"),
    ("pg_hba.conf", "include 'hba-extra.conf'\n", "postgresql_hba_dependencies_unsupported"),
    ("postgresql.auto.conf", "ssl = 'on'\n", "postgresql_reconstruction_evidence_stale"),
])
def test_configuration_dependencies_are_accounted(pg_fixture, file, content, reason):
    path = pg_fixture["data_directory"] / file
    _private(path, content)
    candidate = _call(discover_coverage, pg_fixture)
    assert reason in candidate["reasons"]


@pytest.mark.parametrize("archive_mode,complete", [("off", True), ("on", False)])
def test_native_disabled_archive_command_requires_archiving_off(pg_fixture, archive_mode, complete):
    settings = pg_fixture["runner"].snapshot["settings"]
    next(item for item in settings if item[0] == "archive_command")[1] = "(disabled)"
    next(item for item in settings if item[0] == "archive_mode")[1] = archive_mode
    if complete:
        _write_valid_pg_evidence(pg_fixture)
    candidate = _call(discover_coverage, pg_fixture)
    assert (candidate["coverage_status"] == "complete") is complete
    assert ("postgresql_configuration_dependencies_unsupported" in candidate["reasons"]) is not complete


def test_recovery_artifact_must_be_inside_approved_file_root(pg_fixture):
    pg_fixture["declaration"]["roots"] = []
    pg_fixture["policy"]["approved_roots"] = ["app_db"]
    pg_fixture["declaration_path"].write_text(json.dumps(pg_fixture["declaration"]))
    pg_fixture["declaration_path"].chmod(0o600)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_recovery_artifact_route_uncovered" in candidate["reasons"]
    assert candidate["coverage_status"] == "incomplete"


def test_password_state_and_database_metadata_are_in_reviewed_inventory(pg_fixture):
    _review(pg_fixture)
    pg_fixture["runner"].snapshot["roles"][1]["password"] = "SCRAM-SHA-256$private"
    checked = _call(check_coverage, pg_fixture)
    assert "postgresql_reconstruction_evidence_stale" in checked.reasons
    assert "SCRAM-SHA-256$private" not in json.dumps(checked.public_result())
    pg_fixture["runner"].snapshot["roles"][1]["password"] = None
    pg_fixture["runner"].snapshot["databases"][0]["connection_limit"] = 7
    checked = _call(check_coverage, pg_fixture)
    assert "postgresql_reconstruction_evidence_stale" in checked.reasons


def test_quiet_administrative_window_is_required_at_inspection_time(pg_fixture):
    context = pg_fixture["context"]
    pg_fixture["context"] = PostgreSQLCoverageContext(context.sources,
        context.database_bindings, context.runner)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_quiet_administrative_window_required" in candidate["reasons"]
    assert candidate["coverage_status"] == "incomplete"


def test_pg_hba_auth_file_reference_and_ssl_dependency_fail_closed(pg_fixture):
    _private(pg_fixture["data_directory"] / "pg_hba.conf", "local all @roles trust\n")
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_hba_dependencies_unsupported" in candidate["reasons"]
    _private(pg_fixture["data_directory"] / "pg_hba.conf", "local all all trust\n")
    settings = pg_fixture["runner"].snapshot["settings"]
    next(item for item in settings if item[0] == "ssl")[1] = "on"
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_configuration_dependencies_unsupported" in candidate["reasons"]


def test_custom_reported_hba_file_rejects_auth_reference(pg_fixture):
    directory = pg_fixture["data_directory"]
    custom = _private(directory / "auth.conf", "local all @external_roles trust\n")
    pg_fixture["runner"].snapshot["hba_file"] = str(custom)
    candidate = _call(discover_coverage, pg_fixture)
    assert "postgresql_hba_dependencies_unsupported" in candidate["reasons"]


def test_approval_carries_exact_local_directory_identity(pg_fixture):
    _review(pg_fixture)
    checked = _call(check_coverage, pg_fixture)
    identity = checked.approved_postgresql[0]["data_directory_identity"]
    info = pg_fixture["data_directory"].stat()
    assert identity == {"device": info.st_dev, "inode": info.st_ino,
                        "uid": info.st_uid, "gid": info.st_gid,
                        "mode": info.st_mode & 0o7777}
