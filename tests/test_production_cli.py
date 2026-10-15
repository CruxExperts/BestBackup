# ruff: noqa: F811
import json
from dataclasses import asdict

from click.testing import CliRunner

from bbackup.production_cli import cli
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
    config.write_text(json.dumps(_without_none(asdict(service.config)), default=str))
    bindings.write_text(json.dumps(
        _without_none({"schema_version": 2, **asdict(service.bindings)}), default=str
    ))
    config.chmod(0o600)
    bindings.chmod(0o600)
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
