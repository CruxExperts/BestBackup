"""One validated operation boundary for the machine CLI and interactive dashboard."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil
from typing import Any

from .b2 import inspect_b2
from .cli_metadata import PRODUCTION_COMMANDS, production_schema
from .cloud import S3CloudAdapter
from .configuration_workspace import ConfigurationWorkspace, read_document
from .models import Configuration, ConfigurationError, HostBindings
from .operations import ReconciliationOutcome, repository_key
from .recovery import (create_checked_kit, open_kit, verify_checkpoint,
                       verify_reconstructed_repository)
from .scheduling import job_units
from .service import BackupService, ServiceError
from .service_status import operational_status


def validate_operation_input(command: str, values: dict[str, Any]) -> dict[str, Any]:
    """Apply the registry's strict types, defaults, and unknown-field boundary."""
    if command not in PRODUCTION_COMMANDS or not isinstance(values, dict):
        raise ConfigurationError("Unknown operation or non-object input")
    fields = PRODUCTION_COMMANDS[command]["fields"]
    if set(values) - set(fields):
        raise ConfigurationError("Unknown operation fields")
    result = {}
    for name, kind in fields.items():
        default = False if kind == "boolean" else 100 if kind == "limit" else 0 if kind == "offset" else None
        value = values.get(name, default)
        if value is None:
            if kind == "optional-string":
                continue
            raise ConfigurationError("Missing required input")
        if kind == "boolean":
            if type(value) is not bool:
                raise ConfigurationError("Expected boolean")
        elif kind in ("limit", "offset"):
            if type(value) is not int or value < (1 if kind == "limit" else 0) or (kind == "limit" and value > 1000):
                raise ConfigurationError("Invalid pagination")
        elif not isinstance(value, str) or not value or "\0" in value:
            raise ConfigurationError("Expected a nonempty string")
        result[name] = value
    return result


def _load_service(config_path: Path | None, bindings_path: Path | None) -> BackupService:
    if config_path is None or bindings_path is None:
        raise ConfigurationError("This operation requires configuration and bindings")
    return BackupService(Configuration.load(config_path), HostBindings.load(bindings_path))


def execute_operation(command: str, values: dict[str, Any], *, service: BackupService | None = None,
                      config_path: Path | None = None, bindings_path: Path | None = None,
                      cancel=None):
    """Execute exactly one requested operation; never retry or replay mutations."""
    values = validate_operation_input(command, values)
    if command == "skills":
        name = values.get("name")
        if name is not None and name not in PRODUCTION_COMMANDS:
            raise ConfigurationError("Unknown command schema")
        return {key: {"summary": item["summary"], "input_schema": production_schema(key)}
                for key, item in PRODUCTION_COMMANDS.items() if name is None or key == name}
    if command == "setup" or command.startswith("config "):
        if config_path is None or bindings_path is None:
            raise ConfigurationError("This operation requires configuration and bindings paths")
        workspace = ConfigurationWorkspace(config_path, bindings_path)
        if command == "setup":
            workspace.setup(read_document(Path(values["configuration_file"])),
                            read_document(Path(values["bindings_file"])))
            return {"configured": True, "repositories_initialized": False, "services_enabled": False}
        if command == "config show":
            return workspace.read()
        return workspace.save(read_document(Path(values["document_file"])), values["revision"])
    if command.startswith("recovery "):
        if command == "recovery checkpoint" and service is None:
            service = _load_service(config_path, bindings_path)
        return _recovery_dispatch(service, command, values, cancel)
    if service is None:
        service = _load_service(config_path, bindings_path)
    return _service_operation(service, command, values, config_path, bindings_path, cancel)


def _service_operation(service, command, values, config_path, bindings_path, cancel):
    if command == "doctor":
        return {"restic_available": shutil.which("restic") is not None,
                "jobs": len(service.config.jobs), "production_qualified": False,
                "status": operational_status(service)}
    if command == "jobs list":
        return [asdict(job) for job in service.config.jobs]
    if command == "jobs run":
        return service.run_job(values["name"], cancel=cancel)
    if command == "repositories list":
        return [asdict(repo) for repo in service.config.repositories]
    if command == "repositories init":
        return service.initialize(values["name"], source=values.get("source"), cancel=cancel)
    if command == "repositories check":
        return service.check(values["name"], cancel=cancel)
    if command == "snapshots list":
        return service.snapshots(**values)
    if command == "retention plan":
        return service.retention_plan(values["job"], cancel=cancel)
    if command == "retention apply":
        return service.retention_apply(
            values["job"], values["plan_token"], values["approved"], cancel=cancel
        )
    if command == "snapshots copy":
        return service.copy(**values, cancel=cancel)
    if command == "restore":
        return service.restore(values["repository"], values["snapshot"], Path(values["target"]), cancel=cancel)
    if command == "databases restore":
        return service.restore_database(**values, cancel=cancel)
    if command == "docker restore":
        return service.docker_restore(
            values["repository"], values["snapshot"], values["source"],
            values["connection"], Path(values["target"]), values["container"],
            values["trusted_archive"], cancel=cancel,
        )
    if command == "docker recover":
        return service.docker_recover(
            values["connection"], values["reviewed"], cancel=cancel
        )
    if command == "docker reconcile":
        if values["docker_reviewed"] is not True:
            raise ConfigurationError("Docker inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid Docker reconciliation outcome") from None
        return service.docker_reconcile(
            values["connection"], values["container"], values["operation_id"],
            outcome, values["docker_reviewed"],
        )
    if command == "databases reconcile":
        if values["database_reviewed"] is not True:
            raise ConfigurationError("Database inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid reconciliation outcome") from None
        service.ledger.reconcile(repository_key(service.database_identity(values["connection"], values["target_database"])), values["operation_id"], outcome)
        return {"operation_id": values["operation_id"], "reviewed_outcome": outcome.value, "replayed": False}
    if command == "runs reconcile":
        if values["repository_reviewed"] is not True:
            raise ConfigurationError("Repository inspection must be explicitly acknowledged")
        try:
            outcome = ReconciliationOutcome(values["outcome"])
        except ValueError:
            raise ConfigurationError("Invalid reconciliation outcome") from None
        service.ledger.reconcile(repository_key(service._identity(values["repository"])), values["operation_id"], outcome)
        return {"operation_id": values["operation_id"], "reviewed_outcome": outcome.value, "replayed": False}
    if command in ("runs list", "runs stream"):
        events = service.ledger.events(**values)
        return [asdict(event) for event in events]
    if command == "service render":
        if config_path is None or bindings_path is None:
            raise ConfigurationError("Rendering services requires configuration paths")
        return job_units(service._job(values["name"]), executable=Path(values["executable"]),
                         config=config_path.absolute(), bindings=bindings_path.absolute())
    raise ServiceError("Unsupported command")


def _recovery_dispatch(service, command, values, cancel):
    if command == "recovery b2-inspect":
        return inspect_b2(**values, cancel=cancel)
    if command == "recovery open":
        return open_kit(Path(values["kit"]), Path(values["target"]), signer=values["signer"], cancel=cancel)
    adapter = S3CloudAdapter(values["bucket"], endpoint_url=values["endpoint"],
                             prefix=values.get("prefix", ""), region_name=values.get("region"))
    if command == "recovery inspect":
        return asdict(adapter.inspect_lifecycle())
    if command == "recovery checkpoint":
        return create_checked_kit(service, adapter, values["repository"], values["snapshot"], Path(values["target"]),
                                  signer=values["signer"], recipient=values["recipient"],
                                  quiescent_window_confirmed=values["quiescent_window_confirmed"], cancel=cancel)
    digest = verify_checkpoint(Path(values["manifest"]), Path(values["signature"]), values["signer"], cancel=cancel)
    result = asdict(adapter.restore_checkpoint(values["manifest"], values["target"],
                     expected_manifest_sha256=digest, expected_source_repository=values["repository"],
                     expected_repository_id=values["repository_id"], expected_source_snapshot_id=values["snapshot"], cancel_event=cancel))
    result["destination"] = str(result["destination"])
    verify_reconstructed_repository(Path(values["target"]), Path(values["password_file"]),
                                    values["repository_id"], values["snapshot"], cancel=cancel)
    result["repository_verified"] = True
    result["recovery_demonstrated"] = False
    return result
