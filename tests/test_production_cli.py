# ruff: noqa: F811
import json
from dataclasses import asdict

from click.testing import CliRunner
import pytest

from bbackup.production_cli import _default_paths, cli
from bbackup.configuration_workspace import ConfigurationWorkspace
from bbackup.models import ConfigurationError
from bbackup.production_operations import _service_operation, execute_operation
from tests.test_service import service  # noqa: F401


def _without_none(value):
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (tuple, list)):
        return [_without_none(item) for item in value]
    return value


def invoke(service, command):
    config = service.state / "cli-config.json"
    bindings = service.state / "cli-bindings.json"
    policy = _without_none(asdict(service.config))
    policy["schema_version"] = 3
    source_bindings = {}
    for source in policy["sources"]:
        kind, name = source["kind"], source["name"]
        if kind in ("files", "sqlite"):
            source_bindings[name] = {"paths": source.pop("paths")}
        elif kind == "docker":
            source_bindings[name] = {"container": source.pop("container"),
                                     "connection": source.pop("connection")}
        else:
            source_bindings[name] = {"database": source.pop("database"),
                                     "connection": source.pop("connection")}
        source.pop("paths", None)
    config.write_text(json.dumps(policy, default=str))
    bindings.write_text(json.dumps(
        _without_none({**asdict(service.bindings), "schema_version": 3,
                       "source_bindings": source_bindings}), default=str
    ))
    config.chmod(0o600)
    bindings.chmod(0o600)
    ConfigurationWorkspace(config, bindings).enroll()
    return CliRunner().invoke(cli, ["--config", str(config), "--bindings", str(bindings), *command])


def test_json_contract_unknown_and_duplicate(service):
    for args in (["jobs", "run", "--input-json", '{"name":"daily","unknown":true}'],
                 ["jobs", "run", "--name", "daily", "--input-json", '{"name":"daily"}'],
                 ["jobs", "run", "--input-json", '{"name":"daily","name":"again"}'],
                 ["snapshots", "list", "--input-json", '{"limit":true}']):
        result = invoke(service, args)
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["errors"][0]["code"] == "invalid_input"


def test_schema_and_sanitized_listing(service):
    result = invoke(service, ["jobs", "run", "--schema"])
    assert json.loads(result.output)["data"]["additionalProperties"] is False
    result = invoke(service, ["repositories", "list"])
    assert result.exit_code == 0
    assert ".password" not in result.output and "repository" not in json.loads(result.output)["data"][0]
    help_data = execute_operation("skills", {"name": "service install"})["service install"]
    assert help_data["ui"]["activate"]["help"].startswith("Enable and start")


@pytest.mark.parametrize("quiet_window", [False, True])
def test_reconcile_cli_forwards_quiet_window(service, monkeypatch, quiet_window):
    calls = []

    def reconcile(self, repository, operation_id, outcome, *,
                  quiescent_window_confirmed=False, cancel=None):
        calls.append((repository, operation_id, outcome.value, quiescent_window_confirmed))
        return {"replayed": False}

    monkeypatch.setattr(type(service), "reconcile_run", reconcile)
    arguments = ["runs", "reconcile", "--repository", "local", "--operation-id", "a" * 32,
                 "--outcome", "failed", "--repository-reviewed"]
    if quiet_window:
        arguments.append("--quiescent-window-confirmed")
    result = invoke(service, arguments)
    assert result.exit_code == 0, result.output
    assert calls == [("local", "a" * 32, "failed", quiet_window)]


def test_central_acceptance_dispatch_uses_independent_recovery_owner(monkeypatch):
    from pathlib import Path
    from bbackup import production_operations as module

    calls = []
    adapter = object()
    monkeypatch.setattr(module, "S3CloudAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(module, "accept_recovery_checkpoint", lambda *args, **kwargs: calls.append(
        (args, kwargs)) or {"protection_qualified": False})
    values = {
        "kit": "/private/kit.gpg", "target": "/private/acceptance", "source_signer": "A" * 40,
        "verifier": "B" * 40, "repository": "cloud", "repository_id": "a" * 64,
        "snapshot": "b" * 64, "bucket": "backup-bucket", "endpoint": "https://s3.example.test",
    }
    result = execute_operation("recovery accept", values)
    assert result == {"protection_qualified": False}
    assert calls[0][0] == (adapter, Path(values["kit"]), Path(values["target"]))
    assert calls[0][1]["trusted_verifier"] == values["verifier"]
    assert calls[0][1]["source_repository"] == "cloud"
    schema = execute_operation("skills", {"name": "recovery accept"})["recovery accept"]["input_schema"]
    assert schema["additionalProperties"] is False
    assert {"kit", "verifier", "source_signer", "repository_id", "snapshot"} <= set(schema["required"])


def test_cloud_retention_dispatch_binds_target_and_existing_ledger(service, tmp_path, monkeypatch):
    from dataclasses import replace
    from threading import Event
    from bbackup import production_operations as module
    from bbackup.models import RepositoryBinding
    from bbackup.operations import repository_key

    identity = "s3:https://retention.example.test/bucket/repo"
    service.bindings.repositories["local"] = RepositoryBinding(identity, tmp_path / "password")
    policy = tmp_path / "central-policy.json"
    policy.write_text(json.dumps({"endpoint_url": "https://retention.example.test", "bucket": "bucket", "prefix": "repo/"}))
    policy.chmod(0o600)
    records = tmp_path / "records.json"
    records.write_text(json.dumps([str(tmp_path / "accepted")]))
    records.chmod(0o600)
    calls = []
    class Adapter:
        def __init__(self, *args, **kwargs):
            calls.append(("adapter", args, kwargs))
        def initialize_central_retention_catalog(self, *args, **kwargs):
            calls.append(("enroll", args, kwargs))
            return {"enrolled": True}
        def plan_central_retention(self, *args, **kwargs):
            calls.append(("plan", args, kwargs))
            return {"candidate_count": 0}
        def apply_central_retention(self, *args, **kwargs):
            calls.append(("apply", args, kwargs))
            return {"status": "applied"}
        def reconcile_central_retention(self, *args, **kwargs):
            calls.append(("reconcile", args, kwargs))
            return {"status": "reconciled_abandoned"}
    monkeypatch.setattr(module, "S3CloudAdapter", Adapter)
    common = {"repository": "local", "policy_file": str(policy)}
    grant = {**common, "grant_file": str(tmp_path / "grant.json")}
    window = {**grant, "records_file": str(records), "quiescent_window_confirmed": True}
    cancel = Event()
    def dispatch(command, values):
        return module._service_operation(service, command, values, None, None, cancel)
    assert dispatch("cloud-retention catalog-enroll", grant)["enrolled"] is True
    assert dispatch("cloud-retention plan", window)["candidate_count"] == 0
    assert calls[-1][2]["repository_lease_id"] == repository_key(identity)
    assert dispatch("cloud-retention apply", {**window, "plan_id": "a" * 32, "plan_sha256": "b" * 64})["status"] == "applied"
    assert calls[-1][2]["ledger"] is service.ledger
    assert calls[-1][2]["cancel_event"] is cancel
    assert calls[-1][2]["expected_repository_lease_id"] == repository_key(identity)
    dispatch("cloud-retention reconcile", {**common, "plan_file": str(tmp_path / "plan.json")})
    assert calls[-1][2] == {"ledger": service.ledger, "expected_repository_lease_id": repository_key(identity)}
    service.bindings.repositories["local"] = replace(service.bindings.repositories["local"], repository=identity + "-other")
    before = len(calls)
    with pytest.raises(ConfigurationError, match="differs from enrolled"):
        dispatch("cloud-retention apply", window)
    assert len(calls) == before


def test_schema2_retention_freezes_one_pair_for_native_and_s3(service, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bbackup import production_operations as module
    from bbackup.models import RepositoryBinding
    import boto3

    endpoint = "https://s3.us-west-004.backblazeb2.com"
    service.bindings.repositories["local"] = RepositoryBinding(
        f"s3:{endpoint}/backup-bucket/repo", tmp_path / "password",
    )
    policy = tmp_path / "native-policy.json"
    policy.write_text(json.dumps({
        "schema_version": 2, "endpoint_url": endpoint, "bucket": "backup-bucket",
        "prefix": "repo/", "lock_metadata": {"provider": "b2-native", "bucket_id": "bucket-id-1"},
    }))
    policy.chmod(0o600)
    calls = []
    frozen = SimpleNamespace(access_key="key", secret_key="secret", token=None)
    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: frozen)))
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: calls.append(("s3", kwargs)) or object())
    monkeypatch.setattr(module, "B2VersionLockReader", lambda *args, **kwargs: calls.append(("native", kwargs)) or object())
    class Adapter:
        def __init__(self, *args, **kwargs):
            calls.append(("adapter", kwargs))
        def initialize_central_retention_catalog(self, *_args):
            return {"enrolled": True}
    monkeypatch.setattr(module, "S3CloudAdapter", Adapter)
    values = {"repository": "local", "policy_file": str(policy), "grant_file": str(tmp_path / "grant.json")}
    assert module._cloud_retention_operation(service, "cloud-retention catalog-enroll", values, None)["enrolled"]
    assert calls[0][0] == "native" and calls[1][0] == "s3" and calls[2][0] == "adapter"
    assert calls[0][1]["access_key_id"] == calls[1][1]["aws_access_key_id"] == "key"
    assert calls[0][1]["secret_access_key"] == calls[1][1]["aws_secret_access_key"] == "secret"
    assert calls[2][1]["credential_pair"] == ("key", "secret")
    assert calls[2][1]["region_name"] == "us-west-004"
    frozen.token = "session-token"
    calls.clear()
    with pytest.raises(ConfigurationError, match="bind native B2"):
        module._cloud_retention_operation(service, "cloud-retention catalog-enroll", values, None)
    assert calls == []


def test_schema3_maintenance_freezes_adapter_pair_without_native_metadata(service, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bbackup import production_operations as module
    from bbackup.models import RepositoryBinding
    import boto3

    endpoint = "https://s3.us-west-004.backblazeb2.com"
    service.bindings.repositories["local"] = RepositoryBinding(
        f"s3:{endpoint}/backup-bucket/repo", tmp_path / "password")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema_version": 3, "endpoint_url": endpoint,
                                  "bucket": "backup-bucket", "prefix": "repo/"}))
    policy.chmod(0o600)
    calls = []
    frozen = SimpleNamespace(access_key="adapter-key", secret_key="adapter-secret", token=None)
    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: frozen)))
    monkeypatch.setattr(boto3, "client", lambda *_args, **kwargs: calls.append(("s3", kwargs)) or object())
    class Adapter:
        def __init__(self, *_args, **kwargs):
            calls.append(("adapter", kwargs))
        def initialize_central_retention_catalog(self, *_args, **kwargs):
            return {"status": "enrolled"}
    monkeypatch.setattr(module, "S3CloudAdapter", Adapter)
    result = module._cloud_maintenance_operation(service, "cloud-maintenance catalog-enroll",
        {"repository": "local", "policy_file": str(policy), "grant_file": str(tmp_path / "grant")}, None)
    assert result["status"] == "enrolled"
    assert calls[0][1]["aws_access_key_id"] == "adapter-key"
    assert calls[1][1]["credential_pair"] == ("adapter-key", "adapter-secret")
    assert calls[1][1]["native_lock_reader"] is None


def test_cloud_maintenance_cli_has_exact_private_input_schemas():
    for name in ("catalog-enroll", "records-enroll", "plan", "apply", "reconcile"):
        definition = execute_operation("skills", {"name": "cloud-maintenance " + name})
        assert definition["cloud-maintenance " + name]["input_schema"]["additionalProperties"] is False
    catalog = execute_operation("skills", {"name": "cloud-maintenance catalog-enroll"})
    assert set(catalog["cloud-maintenance catalog-enroll"]["input_schema"]["required"]) == {
        "repository", "policy_file", "grant_file"}
    apply = execute_operation("skills", {"name": "cloud-maintenance apply"})
    assert {"records_file", "maintenance_bindings_file", "plan_id", "plan_sha256"} <= set(
        apply["cloud-maintenance apply"]["input_schema"]["required"])


def test_cloud_retention_requires_enrollment_and_private_record_list(service, tmp_path, monkeypatch):
    result = CliRunner().invoke(cli, ["cloud-retention", "apply", "--schema"])
    schema = json.loads(result.output)["data"]
    assert {"repository", "policy_file", "grant_file", "records_file", "plan_sha256"} <= set(schema["required"])
    assert schema["additionalProperties"] is False
    for key in ("catalog-enroll", "plan", "apply", "reconcile"):
        definition = execute_operation("skills", {"name": "cloud-retention " + key})
        assert definition
    with pytest.raises(ConfigurationError):
        execute_operation("cloud-retention catalog-enroll", {
            "repository": "local", "policy_file": str(tmp_path / "policy"),
            "grant_file": str(tmp_path / "grant")})


def test_coverage_cli_uses_enrolled_bytes_and_exclusive_review_owner(service, tmp_path, monkeypatch):
    from dataclasses import replace
    from bbackup import production_operations as module
    scope = {"schema_version": 1, "approved_roots": ["home"], "require_privileged": False,
             "filesystem_boundaries": "explicit", "metadata_expectations": {
                 key: True for key in ("numeric_owner", "mode", "acl", "xattr", "links")}}
    service.config = replace(service.config, jobs=(replace(
        service.config.jobs[0], coverage_policy=scope),))
    declaration = tmp_path / "private-coverage.json"
    service.bindings = replace(service.bindings, coverage_declarations={"daily": declaration})
    calls = []
    def discover(path, policy, **kwargs):
        assert path == declaration and policy == scope
        assert b'"schema_version": 3' in kwargs["policy_bytes"]
        assert str(declaration).encode() in kwargs["binding_bytes"]
        calls.append("discover")
        return {"coverage_status": "incomplete", "candidate_digest": "a" * 64}
    def review(path, policy, candidate, **kwargs):
        module.verify_bytes(kwargs["state"], kwargs["policy_bytes"], kwargs["binding_bytes"])
        assert candidate == "a" * 64
        calls.append("review")
        return {"coverage_status": "complete", "recovery_status": "untested"}
    monkeypatch.setattr(module, "discover_coverage", discover)
    monkeypatch.setattr(module, "review_coverage", review)
    result = invoke(service, ["coverage", "discover", "--name", "daily"])
    assert result.exit_code == 0, result.output
    assert calls == ["discover"]
    monkeypatch.setattr(module, "_operation_lease", lambda *_args: pytest.fail("nested shared lease"))
    result = invoke(service, ["coverage", "review", "--name", "daily", "--candidate-digest", "a" * 64])
    assert result.exit_code == 0, result.output
    assert calls == ["discover", "review"]


def test_coverage_cli_sanitizes_errors_and_does_not_approve_an_ordinary_job(service, monkeypatch):
    result = invoke(service, ["coverage", "check", "--name", "daily"])
    assert result.exit_code == 2
    assert json.loads(result.output)["errors"][0]["code"] == "invalid_input"
    from bbackup import production_operations as module
    from bbackup.linux_coverage import CoverageError
    def failed(*args, **kwargs):
        raise CoverageError("private paths must never escape the CLI")
    monkeypatch.setattr(module, "execute_operation", failed)
    # The CLI imports its owner function; exercise the sanitizing boundary.
    monkeypatch.setattr("bbackup.production_cli.execute_operation", failed)
    result = invoke(service, ["coverage", "discover", "--name", "daily"])
    assert result.exit_code == 3 and "private paths" not in result.output
    assert json.loads(result.output)["errors"][0]["code"] == "operation_failed"


def test_resource_failure_keeps_structured_evidence_without_source_paths(service, monkeypatch):
    from bbackup.service import BackupService, ResourceBudgetExceeded

    evidence = {"resource_budget_exceeded": True, "repository": "cloud", "replayed": False,
                "local_complete": False, "cloud_complete": False}

    def fail(*args, **kwargs):
        raise ResourceBudgetExceeded(evidence)

    monkeypatch.setattr(BackupService, "run_job", fail)
    result = invoke(service, ["jobs", "run", "--name", "daily"])
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["errors"][0]["code"] == "resource_budget_exceeded"
    assert payload["data"] == evidence


def test_cli_reports_enrollment_next_action(service):
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    config = service.state / "cli-config.json"
    policy = json.loads(config.read_text())
    policy["jobs"][0]["overdue_hours"] = 48
    config.write_text(json.dumps(policy))
    result = CliRunner().invoke(cli, ["--config", str(config), "--bindings",
                                      str(service.state / "cli-bindings.json"), "jobs", "list"])
    assert result.exit_code == 2
    error = json.loads(result.output)["errors"][0]
    assert error["code"] == "enrollment_required"
    assert error["next_action"] == "Review the policy and bindings, then run service enroll"


def test_operation_reloads_current_policy_even_when_supplied_stale_service(service):
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    config = service.state / "cli-config.json"
    bindings = service.state / "cli-bindings.json"
    workspace = ConfigurationWorkspace(config, bindings)
    original = workspace.read()
    policy = json.loads(original["document"])
    policy["jobs"][0]["overdue_hours"] = 48
    workspace.save(json.dumps(policy), original["revision"])
    jobs = execute_operation("jobs list", {}, service=service, config_path=config,
                             bindings_path=bindings)
    assert jobs[0]["overdue_hours"] == 48
    assert service.config.jobs[0].overdue_hours == 26


def test_operation_refuses_binding_edit_between_snapshot_validation_and_dispatch(service, monkeypatch):
    from bbackup import production_operations as module
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    config = service.state / "cli-config.json"
    bindings = service.state / "cli-bindings.json"
    verify = module.verify_bytes

    def edit_after_verify(state, policy_bytes, binding_bytes):
        verify(state, policy_bytes, binding_bytes)
        private = json.loads(bindings.read_text())
        private["repositories"]["copy"]["repository"] += "-unreviewed"
        bindings.write_text(json.dumps(private))

    monkeypatch.setattr(module, "verify_bytes", edit_after_verify)
    monkeypatch.setattr(module, "BackupService", lambda *_args: pytest.fail(
        "service constructed from an unreviewed binding generation"))
    with pytest.raises(ConfigurationError, match="changed during enrollment validation"):
        execute_operation("jobs list", {}, config_path=config, bindings_path=bindings)


def test_default_paths_prefer_only_verified_v3_pair(service, tmp_path, monkeypatch):
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "bbackup"
    directory.mkdir()
    policy = directory / "config.v3.json"
    bindings = directory / "bindings.v3.json"
    policy.write_bytes((service.state / "cli-config.json").read_bytes())
    bindings.write_bytes((service.state / "cli-bindings.json").read_bytes())
    bindings.chmod(0o600)
    assert _default_paths() == (policy, bindings)
    private = json.loads(bindings.read_text())
    private["repositories"]["copy"]["repository"] += "-unreviewed"
    bindings.write_text(json.dumps(private))
    assert _default_paths() == (directory / "config.json", directory / "bindings.json")


def test_default_paths_reject_relative_xdg_home(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/private")
    result = CliRunner().invoke(cli, ["skills"])
    assert result.exit_code == 2
    assert json.loads(result.output)["errors"][0]["code"] == "invalid_input"




def test_syntax_failure_stays_machine_readable():
    for args in (["unknown-command"], ["jobs", "run", "--unknown", "sensitive-input"], ["--unknown"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 2
        assert json.loads(result.output)["errors"][0]["code"] == "invalid_input"
        assert "sensitive-input" not in result.output


def test_reconciliation_requires_explicit_review_and_does_not_run_command(service):
    from bbackup.operations import OperationPlan
    plan = OperationPlan.create(service._identity("local"), "capture", ["never-run"])
    with service.ledger.begin(plan):
        pass
    args = ["runs", "reconcile", "--repository", "local", "--operation-id", plan.operation_id, "--outcome", "abandoned"]
    assert invoke(service, args).exit_code == 2
    result = invoke(service, [*args, "--repository-reviewed"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["replayed"] is False
    missing = invoke(service, ["runs", "reconcile", "--repository", "local", "--operation-id", "missing",
                               "--outcome", "abandoned", "--repository-reviewed"])
    assert missing.exit_code == 3
    assert json.loads(missing.output)["success"] is False


def test_retained_export_commands_have_closed_schema_and_route_exact_digest(service, monkeypatch):
    listed = CliRunner().invoke(cli, ["runs", "retained-exports", "--schema"])
    assert listed.exit_code == 0
    listing_schema = json.loads(listed.output)["data"]
    assert set(listing_schema["properties"]) == {"repository", "limit", "offset"}
    disposal = CliRunner().invoke(cli, ["runs", "discard-exports", "--schema"])
    assert disposal.exit_code == 0
    disposal_schema = json.loads(disposal.output)["data"]
    assert set(disposal_schema["required"]) == {
        "repository", "operation_id", "artifact_record_sha256"}
    assert disposal_schema["additionalProperties"] is False
    calls = []
    monkeypatch.setattr(service, "retained_exports", lambda *args, **kwargs: calls.append(
        ("list", args, kwargs)) or [])
    monkeypatch.setattr(service, "discard_exports", lambda *args: calls.append(
        ("discard", args)) or {"discarded_export_count": 1})
    assert _service_operation(service, "runs retained-exports",
                              {"repository": "primary", "limit": 5, "offset": 2},
                              None, None, None) == []
    assert _service_operation(service, "runs discard-exports", {
        "repository": "primary", "operation_id": "a" * 32,
        "artifact_record_sha256": "b" * 64}, None, None, None) == {
        "discarded_export_count": 1}
    assert calls == [("list", ("primary",), {"limit": 5, "offset": 2}),
                     ("discard", ("primary", "a" * 32, "b" * 64))]



def test_docker_command_schemas_are_closed_and_require_explicit_identity(service):
    expectations = {
        "restore": {"repository", "snapshot", "source", "connection", "target", "container"},
        "recover": {"connection"},
        "reconcile": {"connection", "container", "operation_id", "outcome"},
    }
    acknowledgements = {
        "restore": "trusted_archive",
        "recover": "reviewed",
        "reconcile": "docker_reviewed",
    }
    for operation, required in expectations.items():
        result = invoke(service, ["docker", operation, "--schema"])
        assert result.exit_code == 0, result.output
        schema = json.loads(result.output)["data"]
        assert schema["additionalProperties"] is False
        assert required <= set(schema["required"])
        acknowledgement = acknowledgements[operation]
        assert schema["properties"][acknowledgement]["default"] is False


@pytest.mark.parametrize("mismatch", ["policy", "bindings", "machine", "uid"])
def test_enrollment_mismatch_refused_before_service_construction(service, monkeypatch, mismatch):
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    config = service.state / "cli-config.json"
    bindings = service.state / "cli-bindings.json"
    if mismatch == "policy":
        policy = json.loads(config.read_text())
        policy["jobs"][0]["overdue_hours"] = 48
        config.write_text(json.dumps(policy))
    elif mismatch == "bindings":
        private = json.loads(bindings.read_text())
        private["repositories"]["copy"]["repository"] += "-changed"
        bindings.write_text(json.dumps(private))
    else:
        from bbackup import enrollment
        machine, uid = enrollment.machine_identity()
        monkeypatch.setattr(enrollment, "machine_identity", lambda: (
            "f" * 64 if mismatch == "machine" else machine,
            uid + 1 if mismatch == "uid" else uid,
        ))
    monkeypatch.setattr("bbackup.production_operations.BackupService", lambda *_args: pytest.fail(
        "service constructed before enrollment validation"))
    with pytest.raises(ConfigurationError, match="enrollment"):
        execute_operation("jobs list", {}, config_path=config, bindings_path=bindings)


def test_doctor_can_explain_missing_enrollment_without_constructing_runtime(service, monkeypatch):
    assert invoke(service, ["jobs", "list"]).exit_code == 0
    (service.state / "enrollment.json").unlink()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unenrolled doctor must not construct runtime state")
    monkeypatch.setattr("bbackup.production_operations.BackupService", forbidden)
    result = execute_operation("doctor", {}, config_path=service.state / "cli-config.json",
                               bindings_path=service.state / "cli-bindings.json")
    assert result["enrollment_valid"] is False
    assert "service enroll" in result["next_action"]
    assert not (service.state / "enrollment.json").exists()
