"""Local Restic exercise of the central maintenance command boundary."""

import json
import shutil
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bbackup.models import Configuration, HostBindings, RepositoryBinding
from bbackup.operations import OperationRun, OperationStatus, repository_key
from bbackup.service import BackupService, ServiceError


def _restic(repo, password, *args):
    return subprocess.run(
        ["restic", "-r", str(repo), "--password-file", str(password), *args],
        check=True, capture_output=True, text=True,
    ).stdout


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_real_local_restic_maintenance_forgets_only_exact_target_without_repack(tmp_path, monkeypatch):
    repo, staging, data = (tmp_path / name for name in ("repository", "staging", "data"))
    staging.mkdir(mode=0o700)
    data.mkdir()
    password = tmp_path / "password"
    password.write_text("fixture password")
    password.chmod(0o600)
    _restic(repo, password, "init")
    ids = []
    for value in ("protected", "obsolete", "unknown"):
        (data / "value").write_text(value)
        _restic(repo, password, "backup", str(data))
        ids.append(json.loads(_restic(repo, password, "snapshots", "--json"))[-1]["id"])
    config = Configuration.parse({"schema_version": 3,
        "repositories": [{"name": "replica", "role": "replica"}], "sources": [], "jobs": []})
    service = BackupService(config, HostBindings(tmp_path / "state", {
        "replica": RepositoryBinding(str(repo), password)}, schema_version=3))
    bindings = tmp_path / "maintenance-bindings.json"
    bindings.write_text(json.dumps({"schema_version": 1, "staging_dir": str(staging),
        "access_key_id_env": "TEST_MAINTENANCE_ID", "secret_access_key_env": "TEST_MAINTENANCE_KEY"}))
    bindings.chmod(0o600)
    monkeypatch.setenv("TEST_MAINTENANCE_ID", "separate-key")
    monkeypatch.setenv("TEST_MAINTENANCE_KEY", "separate-secret")
    monkeypatch.setenv("B2_APPLICATION_KEY", "must-not-enter-restic")
    monkeypatch.setattr(service, "_maintenance_inspect_b2", lambda *_args: None)

    class Monitor:
        def __init__(self, *_args, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def check(self):
            pass

    monkeypatch.setattr("bbackup.resource_limits.ResourceMonitor", Monitor)

    class Session:
        selection = SimpleNamespace(resources={"resource_budget": {
            "schema_version": 1, "staging_max_bytes": 1048576,
            "minimum_free_bytes": 65536, "cache_max_bytes": 0},
            "timeout_seconds": 120})
        def __init__(self):
            self.phases = []
            self.plan_data = None
        def plan(self, observed, *, bindings_sha256, credential_identity):
            assert set(observed["visible_snapshot_ids"]) == set(ids)
            assert credential_identity == {"key_id": "separate-key", "profile": "restricted"}
            assert len(bindings_sha256) == 64
            self.plan_data = {"operation_id": "a" * 32, "target_snapshot_ids": [ids[1]],
                              "bindings_sha256": bindings_sha256}
            return {"plan_id": "a" * 32, "plan_sha256": "b" * 64,
                    "target_snapshot_ids": [ids[1]]}
        def validate_plan(self, _id, _sha, observed, **_kwargs):
            assert set(observed["visible_snapshot_ids"]) == set(ids)
            return self.plan_data
        def record_phase(self, _plan, phase, status, *, evidence=None):
            self.phases.append((phase, status, evidence))
        def verify_protected_versions(self):
            return {"protected_version_sha256": "c" * 64, "protected_version_count": 1}
        def finish_reconciliation(self, _plan, observations):
            assert observations["visible_snapshot_ids"] == sorted([ids[0], ids[2]])
            return {"status": "complete", "outcome": "succeeded"}

    session = Session()
    class Adapter:
        _credential_pair = ("inspector", "inspector-secret")
        endpoint_url = "https://s3.us-west-004.backblazeb2.com"
        @contextmanager
        def maintenance_session(self, *_args, **_kwargs):
            yield session

    adapter = Adapter()
    values = {"repository": "replica", "policy_file": str(tmp_path / "policy"),
              "grant_file": str(tmp_path / "grant"), "maintenance_bindings_file": str(bindings),
              "quiescent_window_confirmed": True}
    observed_env = []
    original_run = service.runner.run
    def run(plan, **kwargs):
        observed_env.append((plan.argv, plan.env))
        return original_run(plan, **kwargs)
    monkeypatch.setattr(service.runner, "run", run)
    lease_id = repository_key(str(repo))
    preview = service.cloud_maintenance("cloud-maintenance plan", adapter, values, [], lease_id=lease_id)
    assert preview["target_snapshot_ids"] == [ids[1]]
    assert set(json.loads(_restic(repo, password, "snapshots", "--json"))[-1]) >= {"id"}
    result = service.cloud_maintenance("cloud-maintenance apply", adapter,
        {**values, **{key: preview[key] for key in ("plan_id", "plan_sha256")}},
        [], lease_id=lease_id)
    assert result["status"] == "complete"
    assert {item["id"] for item in json.loads(_restic(repo, password, "snapshots", "--json"))} == {ids[0], ids[2]}
    for index, expected in ((0, "protected"), (2, "unknown")):
        target = tmp_path / f"restored-{index}"
        _restic(repo, password, "restore", ids[index], "--target", str(target))
        assert (target / str(data).lstrip("/") / "value").read_text() == expected
    assert any("prune" in argv and ("--max-repack-size", "0") == argv[-2:] for argv, _env in observed_env)
    assert any("forget" in argv and argv[-2:] == ("--", ids[1]) for argv, _env in observed_env)
    assert all("B2_APPLICATION_KEY" not in env for _argv, env in observed_env)
    assert all(env["AWS_ACCESS_KEY_ID"] == "separate-key" for _argv, env in observed_env)
    assert [name for name, status, _ in session.phases if status == "completed"] == [
        "check_before", "forget", "verify_snapshots", "prune", "check_after", "protected_versions"]


def test_maintenance_bindings_reject_inline_or_reused_credentials(tmp_path, monkeypatch):
    config = Configuration.parse({"schema_version": 3, "repositories": [
        {"name": "replica", "role": "replica"}], "sources": [], "jobs": []})
    password = tmp_path / "password"
    password.write_text("fixture")
    password.chmod(0o600)
    service = BackupService(config, HostBindings(tmp_path / "state", {
        "replica": RepositoryBinding(str(tmp_path / "repo"), password)}, schema_version=3))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    bindings = tmp_path / "bindings.json"
    base = {"schema_version": 1, "staging_dir": str(staging),
            "access_key_id_env": "TEST_MAINTENANCE_ID", "secret_access_key_env": "TEST_MAINTENANCE_KEY"}
    monkeypatch.setenv("TEST_MAINTENANCE_ID", "adapter-key")
    monkeypatch.setenv("TEST_MAINTENANCE_KEY", "adapter-secret")
    bindings.write_text(json.dumps({**base, "secret_access_key": "inline-secret"}))
    bindings.chmod(0o600)
    adapter = SimpleNamespace(_credential_pair=("adapter-key", "adapter-secret"),
                              endpoint_url="https://s3.us-west-004.backblazeb2.com")
    with pytest.raises(ServiceError, match="bindings are invalid"):
        service._maintenance_bindings(bindings, adapter)
    bindings.write_text(json.dumps(base))
    with pytest.raises(ServiceError, match="must differ"):
        service._maintenance_bindings(bindings, adapter)
    monkeypatch.setenv("TEST_MAINTENANCE_ID", "separate-key")
    assert service._maintenance_bindings(bindings, adapter)[1]["profile"] == "restricted"
    bindings.write_text(json.dumps({**base, "credential_profile": "management"}))
    assert service._maintenance_bindings(bindings, adapter)[1]["profile"] == "management"


@pytest.mark.parametrize("profile,scope,accepted", [
    ("restricted", "unqualified", False),
    ("restricted", "eligible_from_observed_metadata", True),
    ("management", "unqualified", True),
])
def test_b2_inspection_requires_restricted_default_or_explicit_management(
    tmp_path, monkeypatch, profile, scope, accepted
):
    config = Configuration.parse({"schema_version": 3, "repositories": [
        {"name": "replica", "role": "replica"}], "sources": [], "jobs": []})
    password = tmp_path / "password"
    password.write_text("fixture")
    password.chmod(0o600)
    service = BackupService(config, HostBindings(tmp_path / "state", {
        "replica": RepositoryBinding(str(tmp_path / "repo"), password)}, schema_version=3))
    adapter = SimpleNamespace(endpoint_url="https://s3.us-west-004.backblazeb2.com",
                              bucket="backup-bucket", prefix="repo/")
    payload = json.dumps({"bucket": "backup-bucket", "prefix": "repo/",
        "s3_endpoint": adapter.endpoint_url, "bucket_access_verified": True,
        "version_listing_verified": True, "write_capability_present": True,
        "read_capability_present": True,
        "restricted_source_profile": {"status": scope}}).encode()
    observed = []
    def run(plan, **_kwargs):
        observed.append(plan)
        return OperationRun(plan.operation_id, plan.repository_id,
            OperationStatus.SUCCEEDED, 0, "2026-10-20T00:00:00Z", "2026-10-20T00:00:00Z",
            0.01, payload, b"", len(payload), 0)
    monkeypatch.setattr(service.runner, "run", run)
    environment = {"PATH": "/usr/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                   "AWS_ACCESS_KEY_ID": "source-key", "AWS_SECRET_ACCESS_KEY": "source-secret"}
    if accepted:
        service._maintenance_inspect_b2(adapter, "local-repo", environment,
                                        ("source-key", "source-secret"), profile, None)
    else:
        with pytest.raises(ServiceError, match="unqualified"):
            service._maintenance_inspect_b2(adapter, "local-repo", environment,
                                            ("source-key", "source-secret"), profile, None)
    assert "AWS_SECRET_ACCESS_KEY" not in observed[0].env
    assert observed[0].env["B2_APPLICATION_KEY_ID"] == "source-key"
