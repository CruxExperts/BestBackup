# ruff: noqa: F811
import json
import shlex
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bbackup.production_cli import cli
from bbackup.scheduling import job_units
from tests.test_service import service  # noqa: F401


def invoke(service, command):
    with patch("bbackup.production_cli._service", return_value=service):
        return CliRunner().invoke(cli, ["--config", "/unused", "--bindings", "/unused", *command])


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


def test_systemd_unit_uses_real_cli_parser(service):
    units = job_units(service.config.jobs[0], executable=Path("/usr/bin/bbackup"),
                      config=Path("/etc/bbackup/config.json"), bindings=Path("/etc/bbackup/bindings.json"))
    command = next(line[10:] for line in units["bbackup-daily.service"].splitlines() if line.startswith("ExecStart="))
    argv = shlex.split(command)
    assert argv[1] == "production"
    with patch("bbackup.production_cli._service", return_value=service), patch.object(service, "run_job", return_value={"ok": True}):
        result = CliRunner().invoke(cli, argv[2:])
    assert result.exit_code == 0, result.output
    assert "Persistent=true" in units["bbackup-daily.timer"]


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
