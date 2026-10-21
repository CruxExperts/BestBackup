"""Production CLI over the shared version-three operations service."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import sqlite3
import tarfile
import threading

import click
from click.core import ParameterSource

from .capture import CaptureError
from .b2 import B2Error
from .cloud import CloudError
from .recovery import RecoveryError
from .linux_coverage import CoverageError
from .cli_metadata import PRODUCTION_COMMANDS, production_schema
from .configuration_workspace import ConfigurationWorkspace
from .enrollment import parse_pair, read_pair, verify_bytes
from .models import ConfigurationError, strict_json
from .operations import OperationError, RepositoryConflictError, UncertainOperationError
from .service import ReplicaJobError, ResourceBudgetExceeded, ServiceError
from .production_operations import _operation_lease, execute_operation
from .progress import observe_progress


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
    """Backup and restore operations."""
    ctx.ensure_object(dict)
    try:
        default_config, default_bindings = _default_paths()
    except ConfigurationError:
        _emit("input", error={"code": "invalid_input", "message": "XDG_CONFIG_HOME must be an absolute directory"})
        ctx.exit(2)
    defaulted = config is None and bindings is None
    config = config if config is not None else default_config
    bindings = bindings if bindings is not None else default_bindings
    ctx.obj.update(config=config, bindings=bindings, paths_defaulted=defaulted)


def _default_paths() -> tuple[Path, Path]:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        if not Path(xdg).is_absolute() or any(c in xdg for c in "\0\r\n"):
            raise ConfigurationError("XDG_CONFIG_HOME must be an absolute directory")
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    directory = base / "bbackup"
    newer = (directory / "config.v3.json", directory / "bindings.v3.json")
    if all(path.is_file() and not path.is_symlink() for path in newer):
        try:
            policy, bindings = read_pair(*newer)
            configuration, host = parse_pair(policy, bindings)
            if configuration.schema_version != 3:
                raise ConfigurationError("Expected version-three defaults")
            verify_bytes(host.state_dir, policy, bindings)
        except (OSError, ConfigurationError):
            pass
        else:
            return newer
    return directory / "config.json", directory / "bindings.json"


def _workspace(ctx):
    if ctx.obj.get("config") is None or ctx.obj.get("bindings") is None:
        raise ConfigurationError("This command requires --config and --bindings")
    return ConfigurationWorkspace(ctx.obj["config"], ctx.obj["bindings"])



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
            progress_json = kwargs.pop("progress_json")
            if schema:
                _emit(command, production_schema(command))
                return
            values = strict_json(supplied) if supplied is not None else {}
            if not isinstance(values, dict) or set(values) - set(definition["fields"]):
                raise ConfigurationError("Unknown JSON fields or non-object input")
            for name in definition["fields"]:
                if name in values and ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE:
                    raise ConfigurationError("Duplicate flag and JSON input")
                if name not in values and kwargs[name] is not None:
                    values[name] = kwargs[name]
            cancel = threading.Event()
            previous = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: cancel.set())
            try:
                observer = (observe_progress(lambda data: click.echo(json.dumps(
                    {"schema_version": 2, "type": "progress", "data": data}, sort_keys=True
                ))) if progress_json else nullcontext())
                with observer:
                    data = execute_operation(command, values, config_path=ctx.obj.get("config"),
                                             bindings_path=ctx.obj.get("bindings"), cancel=cancel)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            if command == "runs stream":
                for event in data:
                    click.echo(json.dumps({"schema_version": 2, "type": "event", "data": event}, sort_keys=True))
            else:
                _emit(command, data)
        except ConfigurationError as exc:
            message = str(exc).lower()
            if "busy" in message:
                error = {"code": "busy", "message": "Configuration is in use by an active operation",
                         "next_action": "Wait for the active operation to finish and retry"}
            elif "machine identity" in message:
                error = {"code": "machine_identity_unavailable", "message": "Machine identity is unavailable",
                         "next_action": "Restore a valid machine ID, then run service enroll"}
            elif "version 2" in message or "migrate" in message:
                error = {"code": "schema_migration_required", "message": "Version 2 policy requires explicit migration",
                         "next_action": "Run config migrate and use its version 3 paths"}
            elif "enrollment" in message or "enroll" in message:
                error = {"code": "enrollment_required", "message": "Policy enrollment is missing or no longer matches",
                         "next_action": "Review the policy and bindings, then run service enroll"}
            else:
                error = {"code": "invalid_input", "message": "Invalid configuration or command input"}
            _emit(command, error=error)
            ctx.exit(2)
        except RepositoryConflictError:
            _emit(command, error={"code": "busy", "message": "Repository operation is already active",
                                  "next_action": "Wait for the active operation to finish"})
            ctx.exit(3)
        except UncertainOperationError as exc:
            _emit(command, error={"code": "requires_reconciliation", "message": "Review repository state before resolving this operation", "operation_id": exc.operation_id})
            ctx.exit(3)
        except ReplicaJobError as exc:
            click.echo(json.dumps({"schema_version": 2, "command": command, "success": False,
                                   "data": exc.result,
                                   "errors": [{"code": "replica_incomplete", "message": str(exc)}]},
                                  sort_keys=True))
            ctx.exit(3)
        except ResourceBudgetExceeded as exc:
            click.echo(json.dumps({"schema_version": 2, "command": command, "success": False,
                                   "data": exc.result,
                                   "errors": [{"code": "resource_budget_exceeded", "message": str(exc)}]},
                                  sort_keys=True))
            ctx.exit(3)
        except (ServiceError, OperationError, CaptureError, CloudError, B2Error, RecoveryError, CoverageError, tarfile.TarError, sqlite3.Error, OSError, ValueError, KeyError):
            _emit(command, error={"code": "operation_failed", "message": "Operation failed; inspect sanitized run state before retrying"})
            ctx.exit(3)
    params = [click.Option(["--input-json"], type=str), click.Option(["--schema"], is_flag=True),
              click.Option(["--progress-json"], is_flag=True,
                           help="Emit path-free numeric progress JSON lines before the final result.")]
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
    from .dashboard import BackupDashboard, BackupSetup
    try:
        workspace = _workspace(ctx)
        if ctx.obj.get("paths_defaulted") and workspace.config_path.name == "config.json":
            imported = (workspace.config_path.with_name("config.v3.json"),
                        workspace.bindings_path.with_name("bindings.v3.json"))
            if all(path.is_file() and not path.is_symlink() for path in imported):
                # A copied or edited v3 pair gets the guided review path. It
                # is never silently adopted for machine operations.
                workspace = ConfigurationWorkspace(*imported)
        if not workspace.config_path.exists() and not workspace.bindings_path.exists():
            service = BackupSetup(workspace).run()
            if service is None:
                return
        else:
            configuration, _ = parse_pair(*read_pair(workspace.config_path, workspace.bindings_path))
            if configuration.schema_version == 2:
                service = BackupSetup(workspace).run()
            else:
                try:
                    with _operation_lease(workspace.config_path, workspace.bindings_path) as service:
                        pass
                except ConfigurationError as exc:
                    if "enroll" not in str(exc).lower():
                        raise
                    service = BackupSetup(workspace).run()
            if service is None:
                return
        BackupDashboard(service, workspace=workspace).run()
    except (ConfigurationError, ServiceError, OSError):
        _emit("tui", error={"code": "configuration_error", "message": "Dashboard configuration failed"})
        ctx.exit(2)


if __name__ == "__main__":
    cli()
