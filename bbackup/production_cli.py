"""Version-two noninteractive CLI over the shared operations service."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import tarfile
import threading

import click
from click.core import ParameterSource

from .capture import CaptureError
from .cloud import CloudError, S3CloudAdapter
from .recovery import RecoveryError, create_checked_kit, open_kit, verify_checkpoint, verify_reconstructed_repository
from .cli_metadata import PRODUCTION_COMMANDS, production_schema
from .models import Configuration, ConfigurationError, HostBindings, strict_json
from .operations import OperationError, ReconciliationOutcome, repository_key, UncertainOperationError
from .service import BackupService, ServiceError
from .scheduling import job_units


def _emit(command, data=None, error=None):
    click.echo(json.dumps({"schema_version": 2, "command": command, "success": error is None,
                           "data": data, "errors": [error] if error else []}, sort_keys=True))


class MachineGroup(click.Group):
    """Keep production syntax failures in the same sanitized machine envelope."""
    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.ClickException:
            _emit("input", error={"code": "invalid_input", "message": "Invalid command syntax"})
            ctx.exit(2)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            _emit("input", error={"code": "invalid_input", "message": "Invalid command syntax"})
            ctx.exit(2)


@click.group(cls=MachineGroup)
@click.option("--config", type=click.Path(path_type=Path), required=False)
@click.option("--bindings", type=click.Path(path_type=Path), required=False)
@click.pass_context
def cli(ctx, config, bindings):
    """Production preview: encrypted local capture and explicit snapshot replication."""
    ctx.ensure_object(dict)
    ctx.obj.update(config=config, bindings=bindings)


def _service(ctx):
    if ctx.obj.get("config") is None or ctx.obj.get("bindings") is None:
        raise ConfigurationError("This command requires --config and --bindings")
    return BackupService(Configuration.load(ctx.obj["config"]), HostBindings.load(ctx.obj["bindings"]))


def dispatch(service, command, values, ctx, cancel):
    if command == "doctor":
        return {"restic_available": shutil.which("restic") is not None,
                "jobs": len(service.config.jobs), "production_qualified": False}
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
    if command == "snapshots copy":
        return service.copy(**values, cancel=cancel)
    if command == "restore":
        return service.restore(values["repository"], values["snapshot"], Path(values["target"]), cancel=cancel)
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
        return job_units(service._job(values["name"]), executable=Path(values["executable"]),
                         config=ctx.obj["config"].absolute(), bindings=ctx.obj["bindings"].absolute())
    raise ServiceError("Unsupported command")


def _recovery_dispatch(ctx, command, values, cancel):
    if command == "recovery open":
        return open_kit(Path(values["kit"]), Path(values["target"]), signer=values["signer"], cancel=cancel)
    adapter = S3CloudAdapter(values["bucket"], endpoint_url=values["endpoint"],
                             prefix=values.get("prefix", ""), region_name=values.get("region"))
    if command == "recovery inspect":
        return asdict(adapter.inspect_lifecycle())
    if command == "recovery checkpoint":
        return create_checked_kit(_service(ctx), adapter, values["repository"], values["snapshot"], Path(values["target"]),
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


def _register(command, definition):
    parts = command.split()
    parent = cli
    if len(parts) == 2:
        if parts[0] not in cli.commands:
            cli.add_command(click.Group(parts[0]))
        parent = cli.commands[parts[0]]
    def callback(**kwargs):
        ctx = click.get_current_context()
        try:
            supplied = kwargs.pop("input_json")
            schema = kwargs.pop("schema")
            if schema:
                _emit(command, production_schema(command))
                return
            values = strict_json(supplied) if supplied is not None else {}
            if not isinstance(values, dict) or set(values) - set(definition["fields"]):
                raise ConfigurationError("Unknown JSON fields or non-object input")
            for name, kind in definition["fields"].items():
                if name in values and ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE:
                    raise ConfigurationError("Duplicate flag and JSON input")
                value = values.get(name, kwargs[name])
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
                values[name] = value
            cancel = threading.Event()
            previous = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: cancel.set())
            try:
                if command == "skills":
                    name = values.get("name")
                    if name is not None and name not in PRODUCTION_COMMANDS:
                        raise ConfigurationError("Unknown command schema")
                    data = {key: {"summary": item["summary"], "input_schema": production_schema(key)}
                            for key, item in PRODUCTION_COMMANDS.items() if name is None or key == name}
                elif command.startswith("recovery "):
                    data = _recovery_dispatch(ctx, command, values, cancel)
                else:
                    data = dispatch(_service(ctx), command, values, ctx, cancel)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            if command == "runs stream":
                for event in data:
                    click.echo(json.dumps({"schema_version": 2, "type": "event", "data": event}, sort_keys=True))
            else:
                _emit(command, data)
        except ConfigurationError:
            _emit(command, error={"code": "invalid_input", "message": "Invalid configuration or command input"})
            ctx.exit(2)
        except UncertainOperationError as exc:
            _emit(command, error={"code": "requires_reconciliation", "message": "Review repository state before resolving this operation", "operation_id": exc.operation_id})
            ctx.exit(3)
        except (ServiceError, OperationError, CaptureError, CloudError, RecoveryError, tarfile.TarError, sqlite3.Error, OSError, ValueError):
            _emit(command, error={"code": "operation_failed", "message": "Operation failed; inspect sanitized run state before retrying"})
            ctx.exit(3)
    params = [click.Option(["--input-json"], type=str), click.Option(["--schema"], is_flag=True)]
    for name, kind in definition["fields"].items():
        params.append(click.Option(["--" + name.replace("_", "-")], is_flag=kind == "boolean",
                                   type=bool if kind == "boolean" else int if kind in ("limit", "offset") else str,
                                   default=False if kind == "boolean" else 100 if kind == "limit" else 0 if kind == "offset" else None))
    parent.add_command(click.Command(parts[-1], callback=callback, params=params, help=definition["summary"]))


for _command, _definition in PRODUCTION_COMMANDS.items():
    _register(_command, _definition)


@cli.command("tui")
@click.pass_context
def tui(ctx):
    """Open the mouse and keyboard dashboard."""
    from .dashboard import BackupDashboard
    try:
        BackupDashboard(_service(ctx)).run()
    except (ConfigurationError, ServiceError, OSError):
        _emit("tui", error={"code": "configuration_error", "message": "Dashboard configuration failed"})
        ctx.exit(2)


if __name__ == "__main__":
    cli()
