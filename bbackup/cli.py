"""
bbackup/cli.py
Purpose: Main CLI entry point for bbackup. Provides all backup/restore subcommands
         with AI-agent-friendly JSON I/O via --output json, --input-json, --dry-run,
         and the `skills` subcommand for progressive capability discovery.
Created: 2025-01-01
Last Updated: 2026-03-04
"""

import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, List

import click
from rich.console import Console
from rich.table import Table
from rich import box

from .config import Config, BackupScope, FilesystemTarget
from .docker_backup import DockerBackup
from .tui import BackupTUI, BackupStatus
from .remote import RemoteStorageManager
from .archive import create_solid_archive, is_solid_archive_name, unpack_solid_archive
from .backup_runner import BackupRunner
from .restore import DockerRestore, list_available_backups, list_volume_backup_names
from .logging import setup_logging
from .encryption import EncryptionManager
from .snapshot import (
    SnapshotError,
    expand_path,
    purge_plan,
    retire_repo,
    schedule_units,
    snapshot_check,
    snapshot_init,
    snapshot_plan,
    snapshot_restore,
    snapshot_run,
)
from .resources import read_text_resource, resource_exists
from .cli_utils import (
    output_option,
    input_json_option,
    dry_run_option,
    merge_json_input,
    render_output,
    json_error,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    EXIT_SYSTEM_ERROR,
    EXIT_PARTIAL,
    EXIT_CANCELLED,
    BBACKUP_NO_INTERACTIVE_ENV,
)
from .skills import get_skill


SKILLS_DOC_RESOURCE = "cli-skills.md"
SKILLS_INDEX_RESOURCE = "cli-skills-index.json"


def _find_duplicate_filesystem_target_names(targets: List[FilesystemTarget]) -> List[str]:
    seen = set()
    duplicates = []
    for target in targets:
        if target.name in seen and target.name not in duplicates:
            duplicates.append(target.name)
        seen.add(target.name)
    return duplicates


def _discover_restore_all_targets(
    backup_path: Path,
    config: Config,
) -> tuple[List[str], List[str], List[str], List[str]]:
    """Discover restore targets from a backup directory or solid archive file."""
    discovery_path = backup_path
    temp_root_to_remove: Optional[Path] = None

    try:
        if backup_path.is_file() and is_solid_archive_name(backup_path.name):
            discovery_path, temp_root_to_remove = unpack_solid_archive(
                backup_path,
                dest_dir=None,
                encryption_config=config.encryption if config.encryption.enabled else None,
            )

        configs_dir = discovery_path / "configs"
        networks_dir = discovery_path / "networks"
        filesystems_dir = discovery_path / "filesystems"

        containers = []
        volumes = []
        networks = []
        filesystems = []

        if configs_dir.exists():
            containers = sorted(f.stem.replace("_config", "") for f in configs_dir.glob("*_config.json"))
        if (discovery_path / "volumes").exists():
            volumes = sorted(list_volume_backup_names(discovery_path))
        if networks_dir.exists():
            networks = sorted(f.stem for f in networks_dir.glob("*.json"))
        if filesystems_dir.exists():
            filesystems = sorted(f.name for f in filesystems_dir.iterdir() if f.is_dir())

        return containers, volumes, networks, filesystems
    finally:
        if temp_root_to_remove is not None:
            shutil.rmtree(temp_root_to_remove, ignore_errors=True)


def _backup_encryption_result(status: BackupStatus, backup_path: Path) -> str:
    if status.encryption_status == "encrypted" or str(backup_path).endswith(".enc"):
        return "encrypted"
    if status.encryption_status == "failed":
        return "failed"
    return "disabled"


def _snapshot_profile_or_exit(config: Config, profile: str, output: str):
    snapshot_profile = config.get_snapshot_profile(profile)
    if snapshot_profile is None:
        json_error("snapshot", f"Snapshot profile not found: {profile}", EXIT_USER_ERROR, output)
    return snapshot_profile


def _snapshot_required_param(ctx: click.Context, name: str, command: str, output: str):
    value = ctx.params.get(name)
    if value in (None, ""):
        json_error(command, f"Missing required option: --{name.replace('_', '-')}", EXIT_USER_ERROR, output)
    return value


def _snapshot_result(command: str, result: dict, output: str, console: Console) -> None:
    success = bool(result.get("success", result.get("ok", True)))
    render_output(result, output, command, success=success)
    if output != "json":
        console.print_json(data=result)
    sys.exit(EXIT_SUCCESS if success else EXIT_SYSTEM_ERROR)


@click.group()
@click.version_option(version=__import__("bbackup").__version__)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to configuration file",
)
@click.pass_context
def cli(ctx, config):
    """bbackup - Docker and filesystem backup tool with Rich TUI."""
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand == "production":
        return
    ctx.obj["config"] = Config(config_path=config)
    ctx.obj["console"] = Console()
    setup_logging(ctx.obj["config"])


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--containers", "-C", multiple=True, help="Container names to backup (repeatable)")
@click.option("--backup-set", "-s", help="Use predefined backup set from config")
@click.option("--config-only", is_flag=True, help="Backup only configurations (no volumes)")
@click.option("--volumes-only", is_flag=True, help="Backup only volumes (no configs)")
@click.option("--no-networks", is_flag=True, help="Skip network backups")
@click.option("--incremental", "-i", is_flag=True, help="Use incremental backup (rsync link-dest)")
@click.option(
    "--no-interactive",
    is_flag=True,
    default=False,
    help=(
        "Disable TUI and interactive prompts. "
        "Set BBACKUP_NO_INTERACTIVE=1 to apply globally."
    ),
)
@click.option("--remote", "-r", multiple=True, help="Remote storage destinations (repeatable)")
@click.option("--paths", "-p", multiple=True, metavar="PATH", help="Filesystem paths to back up (repeatable)")
@click.option("--exclude", multiple=True, metavar="PATTERN", help="Exclude patterns for filesystem backup (repeatable)")
@click.option("--filesystem-set", default=None, help="Named filesystem backup set from config")
@click.option("--solid-archive/--no-solid-archive", "solid_archive_flag", default=None,
              help="Create a single tarball for upload, or disable (overrides config).")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def backup(
    ctx,
    containers,
    backup_set,
    config_only,
    volumes_only,
    no_networks,
    incremental,
    no_interactive,
    remote,
    paths,
    exclude,
    filesystem_set,
    solid_archive_flag,
    skills,
    output,
    input_json,
    dry_run,
):
    """Create Docker and/or filesystem backup."""
    if skills:
        _print_command_skills("bbackup", "backup")
    merge_json_input(ctx, input_json)
    # Re-read possibly overridden values from ctx.params
    containers = ctx.params.get("containers", containers)
    backup_set = ctx.params.get("backup_set", backup_set)
    config_only = ctx.params.get("config_only", config_only)
    volumes_only = ctx.params.get("volumes_only", volumes_only)
    no_networks = ctx.params.get("no_networks", no_networks)
    incremental = ctx.params.get("incremental", incremental)
    no_interactive = ctx.params.get("no_interactive", no_interactive)
    remote = ctx.params.get("remote", remote)
    paths = ctx.params.get("paths", paths)
    exclude = ctx.params.get("exclude", exclude)
    filesystem_set = ctx.params.get("filesystem_set", filesystem_set)
    solid_archive_flag = ctx.params.get("solid_archive_flag", solid_archive_flag)
    dry_run = ctx.params.get("dry_run", dry_run)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    # Gap 1: honour env var for non-interactive mode
    _no_interactive = no_interactive or os.environ.get(BBACKUP_NO_INTERACTIVE_ENV) == "1"
    # When --output json is active, TUI must be off
    use_tui = not _no_interactive and output != "json"

    # Determine backup scope
    scope = BackupScope()
    if config_only:
        scope.volumes = False
        scope.networks = False
    elif volumes_only:
        scope.containers = False
        scope.configs = False
        scope.networks = False
    if no_networks:
        scope.networks = False

    # Resolve containers list
    containers_to_backup: Optional[List[str]] = None

    if backup_set:
        backup_set_obj = config.get_backup_set(backup_set)
        if backup_set_obj:
            containers_to_backup = backup_set_obj.containers
            scope = backup_set_obj.scope
            if output != "json":
                console.print(f"[green]Using backup set: {backup_set}[/green]")
        else:
            msg = f"Backup set '{backup_set}' not found in config"
            if output != "json":
                sys.stderr.write(f"Error: {msg}\n")
            json_error("backup", msg, EXIT_USER_ERROR, output)
    elif containers:
        containers_to_backup = list(containers)
    elif use_tui:
        tui = BackupTUI(config)
        tui.show_header()
        docker_backup = DockerBackup(config)
        all_containers = docker_backup.get_all_containers()
        selected = tui.select_containers(all_containers)
        containers_to_backup = list(selected)
        scope_dict = tui.select_scope()
        scope.containers = scope_dict.get("containers", True)
        scope.volumes = scope_dict.get("volumes", True)
        scope.networks = scope_dict.get("networks", True)
        scope.configs = scope_dict.get("configs", True)

    if not containers_to_backup:
        msg = "No containers selected for backup"
        if output != "json":
            sys.stderr.write(f"Error: {msg}\n")
        json_error("backup", msg, EXIT_USER_ERROR, output)

    # Resolve remotes
    remotes_to_use = []
    if remote:
        for r_name in remote:
            if r_name in config.remotes:
                remotes_to_use.append(config.remotes[r_name])
            elif output != "json":
                sys.stderr.write(f"Warning: Remote '{r_name}' not found in config\n")
    else:
        remotes_to_use = config.get_enabled_remotes()

    if not remotes_to_use and output != "json":
        console.print("[yellow]Warning: No remote storage destinations configured[/yellow]")
        console.print("[dim]Backup will be created locally only[/dim]")

    # Resolve filesystem targets
    filesystem_targets = []
    if filesystem_set and filesystem_set in config.filesystem_sets:
        fs_set_obj = config.filesystem_sets[filesystem_set]
        filesystem_targets = [t for t in fs_set_obj.targets if t.enabled]
        if output != "json":
            console.print(f"[green]Using filesystem set: {filesystem_set}[/green]")
    elif paths:
        filesystem_targets = [
            FilesystemTarget(name=Path(p).name, path=p, excludes=list(exclude))
            for p in paths
        ]
    elif scope.filesystems:
        for fs_set_obj in config.filesystem_sets.values():
            filesystem_targets.extend(t for t in fs_set_obj.targets if t.enabled)

    duplicate_target_names = _find_duplicate_filesystem_target_names(filesystem_targets)
    if duplicate_target_names:
        msg = f"Duplicate filesystem target name: {', '.join(duplicate_target_names)}"
        if output != "json":
            sys.stderr.write(f"Error: {msg}\n")
        json_error("backup", msg, EXIT_USER_ERROR, output)

    # Gap 9: dry-run support
    if dry_run:
        plan = {
            "dry_run": True,
            "would_backup": {
                "containers": list(containers_to_backup or []),
                "filesystem_targets": [
                    {"name": t.name, "path": t.path, "excludes": t.excludes}
                    for t in filesystem_targets
                ],
                "remotes": [r.name for r in remotes_to_use if hasattr(r, "name")],
                "incremental": incremental or config.incremental.enabled,
                "scope": {
                    "volumes": scope.volumes,
                    "configs": scope.configs,
                    "networks": scope.networks,
                },
            },
        }
        render_output(plan, output, "backup", success=True)
        if output != "json":
            console.print("[cyan]Dry-run: no backup created.[/cyan]")
        sys.exit(EXIT_SUCCESS)

    # Build staging dir
    staging_dir = Path(config.get_staging_dir())
    backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{backup_timestamp}"
    backup_dir = staging_dir / backup_name

    status = BackupStatus()
    runner = BackupRunner(config, status)
    use_solid_archive = solid_archive_flag if solid_archive_flag is not None else config.solid_archive

    # Gap 8: capture run_backup result
    run_results = {}

    def backup_operation():
        nonlocal backup_dir, backup_name, run_results
        upload_path = backup_dir
        original_backup_dir = backup_dir
        try:
            status.status = "running"
            run_results = runner.run_backup(
                backup_dir=backup_dir,
                containers=containers_to_backup,
                scope=scope,
                incremental=incremental or config.incremental.enabled,
                filesystem_targets=filesystem_targets,
            ) or {}

            run_errors = run_results.get("errors") if isinstance(run_results, dict) else []
            if status.status in ("partial", "error") or run_errors:
                if status.status != "error":
                    status.status = "partial"
                return

            if use_solid_archive and status.status != "cancelled":
                status.update(action="Creating archive...", item="")
                compression_cfg = config.get_backup_compression()
                enc_cfg = config.encryption if config.encryption.enabled else None
                if enc_cfg:
                    status.update(action="Encrypting archive...", item="")
                upload_path = create_solid_archive(backup_dir, compression_cfg, enc_cfg)
                backup_dir = upload_path
                backup_name = upload_path.name
                if original_backup_dir.exists():
                    shutil.rmtree(original_backup_dir)
                if enc_cfg and str(upload_path).endswith(".enc"):
                    status.encryption_status = "encrypted"
                if remotes_to_use:
                    runner.upload_to_remotes(upload_path, backup_name, remotes_to_use)
                any_ok = any(st == "success" for st in (status.remote_status or {}).values())
                if any_ok:
                    try:
                        if upload_path.exists():
                            upload_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                if config.encryption.enabled and status.status != "cancelled" and not use_solid_archive:
                    original_backup_dir = backup_dir
                    encrypted_backup_dir = runner.encrypt_backup_directory(backup_dir)
                    if encrypted_backup_dir != original_backup_dir:
                        backup_dir = encrypted_backup_dir
                        backup_name = encrypted_backup_dir.name
                        if original_backup_dir.exists():
                            shutil.rmtree(original_backup_dir)

                if remotes_to_use and status.status != "cancelled":
                    runner.upload_to_remotes(backup_dir, backup_name, remotes_to_use)

            if status.status not in ("cancelled", "partial", "error"):
                status.status = "completed"
        except Exception as e:
            if status.status != "partial":
                status.status = "error"
            if str(e) not in status.errors:
                status.add_error(str(e))

    try:
        if use_tui:
            tui = BackupTUI(config)
            tui.status = status
            tui.run_with_live_dashboard(backup_operation)
        else:
            backup_operation()
    except KeyboardInterrupt:
        status.cancel()
        if output != "json":
            console.print("\n[yellow]Backup cancelled by user[/yellow]")
        sys.exit(EXIT_CANCELLED)

    # Build JSON-friendly results dict
    backup_result = {
        "backup_dir": str(backup_dir),
        "containers": status.containers_status or {},
        "volumes": status.volumes_status or {},
        "networks": status.networks_status or {},
        "filesystems": status.filesystems_status or {},
        "remotes": status.remote_status or {},
        "encryption": _backup_encryption_result(status, backup_dir),
        "errors": status.errors or [],
    }

    if status.status == "completed":
        render_output(backup_result, output, "backup", success=True)
        if output != "json":
            console.print(f"\n[green]Backup completed: {backup_dir}[/green]")
            tui_inst = BackupTUI(config)
            tui_inst.show_backup_status(
                {
                    "containers": status.containers_status,
                    "volumes": status.volumes_status,
                    "networks": status.networks_status,
                    "filesystems": status.filesystems_status,
                },
                status.errors,
            )
        sys.exit(EXIT_SUCCESS)
    elif status.status == "cancelled":
        render_output(backup_result, output, "backup", success=False, errors=["Backup cancelled"])
        if output != "json":
            console.print("\n[yellow]Backup was cancelled[/yellow]")
        sys.exit(EXIT_CANCELLED)
    else:
        render_output(backup_result, output, "backup", success=False, errors=status.errors or ["Backup failed"])
        if output != "json":
            console.print("\n[red]Backup failed or was interrupted[/red]")
            for err in status.errors or []:
                console.print(f"  [red]x[/red] {err}")
        sys.exit(EXIT_PARTIAL if run_results else EXIT_SYSTEM_ERROR)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

@cli.group()
def snapshot():
    """Native deduplicating snapshot backups."""


@snapshot.command("plan")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@output_option
@input_json_option
@click.pass_context
def snapshot_plan_cmd(ctx, profile, output, input_json):
    """Resolve a snapshot profile without executing restic."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    profile = _snapshot_required_param(ctx, "profile", "snapshot plan", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        result = snapshot_plan(snapshot_profile)
        render_output(result, output, "snapshot plan", success=not bool(result.get("alerts")))
        if output != "json":
            console.print_json(data=result)
        sys.exit(EXIT_SYSTEM_ERROR if result.get("alerts") else EXIT_SUCCESS)
    except SnapshotError as exc:
        json_error("snapshot plan", str(exc), EXIT_SYSTEM_ERROR, output)


@snapshot.command("init")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def snapshot_init_cmd(ctx, profile, output, input_json, dry_run):
    """Initialize the restic repository for a snapshot profile."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    dry_run = ctx.params.get("dry_run", dry_run)
    profile = _snapshot_required_param(ctx, "profile", "snapshot init", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        _snapshot_result("snapshot init", snapshot_init(snapshot_profile, dry_run=dry_run), output, console)
    except SnapshotError as exc:
        json_error("snapshot init", str(exc), EXIT_SYSTEM_ERROR, output)


@snapshot.command("run")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def snapshot_run_cmd(ctx, profile, output, input_json, dry_run):
    """Run a native snapshot profile."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    dry_run = ctx.params.get("dry_run", dry_run)
    profile = _snapshot_required_param(ctx, "profile", "snapshot run", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        _snapshot_result("snapshot run", snapshot_run(snapshot_profile, dry_run=dry_run), output, console)
    except SnapshotError as exc:
        json_error("snapshot run", str(exc), EXIT_SYSTEM_ERROR, output)


@snapshot.command("check")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@click.option("--read-data-subset", default=None, help="Pass through to restic check")
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def snapshot_check_cmd(ctx, profile, read_data_subset, output, input_json, dry_run):
    """Check a native snapshot repository."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    dry_run = ctx.params.get("dry_run", dry_run)
    read_data_subset = ctx.params.get("read_data_subset", read_data_subset)
    profile = _snapshot_required_param(ctx, "profile", "snapshot check", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        result = snapshot_check(snapshot_profile, read_data_subset=read_data_subset, dry_run=dry_run)
        _snapshot_result("snapshot check", result, output, console)
    except SnapshotError as exc:
        json_error("snapshot check", str(exc), EXIT_SYSTEM_ERROR, output)


@snapshot.command("restore")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@click.option("--snapshot-id", required=False, help="Restic snapshot ID to restore")
@click.option("--target", required=False, type=click.Path(), help="Empty restore target directory")
@click.option("--include", "include_path", default=None, help="Optional restic include filter")
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def snapshot_restore_cmd(ctx, profile, snapshot_id, target, include_path, output, input_json, dry_run):
    """Restore a restic snapshot into a target directory."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    dry_run = ctx.params.get("dry_run", dry_run)
    include_path = ctx.params.get("include_path", include_path)
    profile = _snapshot_required_param(ctx, "profile", "snapshot restore", output)
    snapshot_id = _snapshot_required_param(ctx, "snapshot_id", "snapshot restore", output)
    target = _snapshot_required_param(ctx, "target", "snapshot restore", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    target_path = expand_path(target)
    if not dry_run and target_path.exists() and any(target_path.iterdir()):
        json_error("snapshot restore", f"Restore target is not empty: {target}", EXIT_USER_ERROR, output)
    try:
        result = snapshot_restore(
            snapshot_profile,
            snapshot_id=snapshot_id,
            target=target,
            include=include_path,
            dry_run=dry_run,
        )
        _snapshot_result("snapshot restore", result, output, console)
    except SnapshotError as exc:
        json_error("snapshot restore", str(exc), EXIT_SYSTEM_ERROR, output)


@snapshot.command("retire")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@click.option("--repo-id", required=False, help="Repo ID to mark retired")
@output_option
@input_json_option
@click.pass_context
def snapshot_retire_cmd(ctx, profile, repo_id, output, input_json):
    """Manually mark a repo as retired after it has a successful snapshot."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    profile = _snapshot_required_param(ctx, "profile", "snapshot retire", output)
    repo_id = _snapshot_required_param(ctx, "repo_id", "snapshot retire", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        result = retire_repo(snapshot_profile, repo_id)
        _snapshot_result("snapshot retire", result, output, console)
    except SnapshotError as exc:
        json_error("snapshot retire", str(exc), EXIT_USER_ERROR, output)


@snapshot.command("purge-plan")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@click.option("--repo-id", required=False, help="Retired repo ID to plan for purge")
@output_option
@input_json_option
@click.pass_context
def snapshot_purge_plan_cmd(ctx, profile, repo_id, output, input_json):
    """Build a dry-run-first purge plan for a retired repo."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    profile = _snapshot_required_param(ctx, "profile", "snapshot purge-plan", output)
    repo_id = _snapshot_required_param(ctx, "repo_id", "snapshot purge-plan", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    try:
        result = purge_plan(snapshot_profile, repo_id)
        _snapshot_result("snapshot purge-plan", result, output, console)
    except SnapshotError as exc:
        json_error("snapshot purge-plan", str(exc), EXIT_USER_ERROR, output)


@snapshot.command("schedule")
@click.option("--profile", "-p", required=False, help="Snapshot profile name")
@output_option
@input_json_option
@click.pass_context
def snapshot_schedule_cmd(ctx, profile, output, input_json):
    """Render user systemd units/timers for a snapshot profile."""
    merge_json_input(ctx, input_json)
    output = ctx.params.get("output", output)
    profile = _snapshot_required_param(ctx, "profile", "snapshot schedule", output)
    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]
    snapshot_profile = _snapshot_profile_or_exit(config, profile, output)
    result = {"profile": profile, "units": schedule_units(snapshot_profile, config_path=config.config_path)}
    _snapshot_result("snapshot schedule", result, output, console)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--backup-path", "-p", type=click.Path(exists=True, path_type=Path), required=True,
              help="Path to backup directory")
@click.option("--containers", "-C", multiple=True, help="Container names to restore (repeatable)")
@click.option("--volumes", "-V", multiple=True, help="Volume names to restore (repeatable)")
@click.option("--networks", "-N", multiple=True, help="Network names to restore (repeatable)")
@click.option("--rename", "-r", multiple=True, help="Rename mapping old_name:new_name (repeatable)")
@click.option("--all", "restore_all", is_flag=True, help="Restore everything from backup")
@click.option("--filesystem", multiple=True, help="Filesystem target names to restore (repeatable)")
@click.option("--filesystem-destination", type=click.Path(), default=None,
              help="Destination path for filesystem restore")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@dry_run_option
@click.pass_context
def restore(
    ctx,
    backup_path,
    containers,
    volumes,
    networks,
    rename,
    restore_all,
    filesystem,
    filesystem_destination,
    skills,
    output,
    input_json,
    dry_run,
):
    """Restore Docker or filesystem backup."""
    if skills:
        _print_command_skills("bbackup", "restore")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    backup_path = Path(backup_path)
    if not backup_path.exists():
        json_error("restore", f"Backup path does not exist: {backup_path}", EXIT_USER_ERROR, output)

    rename_map = {}
    if rename:
        for mapping in rename:
            if ":" in mapping:
                old_name, new_name = mapping.split(":", 1)
                rename_map[old_name.strip()] = new_name.strip()

    containers_to_restore = None
    volumes_to_restore = None
    networks_to_restore = None
    filesystems_to_restore = None

    if restore_all:
        try:
            (
                containers_to_restore,
                volumes_to_restore,
                networks_to_restore,
                filesystems_to_restore,
            ) = _discover_restore_all_targets(backup_path, config)
        except OSError as exc:
            json_error("restore", str(exc), EXIT_USER_ERROR, output)
    else:
        if containers:
            containers_to_restore = list(containers)
        if volumes:
            volumes_to_restore = list(volumes)
        if networks:
            networks_to_restore = list(networks)
        if filesystem:
            filesystems_to_restore = list(filesystem)
    fs_destination = Path(filesystem_destination) if filesystem_destination else None

    if not containers_to_restore and not volumes_to_restore and not networks_to_restore and not filesystems_to_restore:
        json_error(
            "restore",
            "No items specified. Use --all or specify --containers / --volumes / --networks / --filesystem",
            EXIT_USER_ERROR,
            output,
        )

    if filesystems_to_restore and fs_destination is None:
        json_error(
            "restore",
            "Filesystem restore requires --filesystem-destination",
            EXIT_USER_ERROR,
            output,
        )
    if filesystems_to_restore and len(filesystems_to_restore) > 1:
        json_error(
            "restore",
            "Restore one filesystem target at a time; multiple filesystem targets cannot share one destination",
            EXIT_USER_ERROR,
            output,
        )

    # Gap 9: dry-run
    if dry_run:
        plan = {
            "dry_run": True,
            "backup_path": str(backup_path),
            "would_restore": {
                "containers": containers_to_restore or [],
                "volumes": volumes_to_restore or [],
                "networks": networks_to_restore or [],
                "filesystems": filesystems_to_restore or [],
            },
        }
        render_output(plan, output, "restore", success=True)
        if output != "json":
            console.print("[cyan]Dry-run: no restore executed.[/cyan]")
        sys.exit(EXIT_SUCCESS)

    if output != "json":
        console.print(f"[bold]Restoring from backup: {backup_path}[/bold]\n")

    restore_mgr = DockerRestore(config)
    results = restore_mgr.restore_backup(
        backup_path=backup_path,
        containers=containers_to_restore,
        volumes=volumes_to_restore,
        networks=networks_to_restore,
        filesystems=filesystems_to_restore,
        filesystem_destination=fs_destination,
        rename_map=rename_map,
    )

    errors = results.get("errors", [])
    success = len(errors) == 0
    render_output(results, output, "restore", success=success, errors=errors)

    if output != "json":
        console.print("\n[bold]Restore Results:[/bold]\n")
        table = Table(show_header=True, header_style="bold green", box=box.ROUNDED)
        table.add_column("Type", style="cyan", width=15)
        table.add_column("Success", style="green", width=10)
        table.add_column("Failed", style="red", width=10)

        for res_type in ("containers", "volumes", "networks", "filesystems"):
            type_res = results.get(res_type, {})
            if type_res:
                ok = sum(1 for v in type_res.values() if v == "success")
                fail = sum(1 for v in type_res.values() if v == "failed")
                table.add_row(res_type.capitalize(), str(ok), str(fail))
        console.print(table)

        if errors:
            console.print("\n[bold red]Errors:[/bold red]")
            for error in errors:
                console.print(f"  [red]x[/red] {error}")
            sys.exit(EXIT_PARTIAL)
        else:
            console.print("\n[green]Restore completed successfully[/green]")

    sys.exit(EXIT_PARTIAL if errors else EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# list-containers
# ---------------------------------------------------------------------------

@cli.command("list-containers")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def list_containers(ctx, skills, output, input_json):
    """List all Docker containers."""
    if skills:
        _print_command_skills("bbackup", "list-containers")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    docker_backup = DockerBackup(config)
    containers = docker_backup.get_all_containers()

    # Gap 7: include container id in JSON output
    containers_data = [
        {
            "id": c.get("id", ""),
            "name": c["name"],
            "status": c["status"],
            "image": c["image"],
        }
        for c in containers
    ]

    render_output({"containers": containers_data}, output, "list-containers")

    if output != "json":
        tui = BackupTUI(config)
        tui.show_header("Container List")
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Status", width=12)
        table.add_column("Name", style="cyan", width=30)
        table.add_column("Image", style="dim", width=40)
        for c in containers:
            status_color = "green" if c["status"] == "running" else "yellow"
            table.add_row(
                f"[{status_color}]{c['status']}[/{status_color}]",
                c["name"],
                c["image"][:40],
            )
        console.print(table)


# ---------------------------------------------------------------------------
# list-backup-sets
# ---------------------------------------------------------------------------

@cli.command("list-backup-sets")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def list_backup_sets(ctx, skills, output, input_json):
    """List available backup sets."""
    if skills:
        _print_command_skills("bbackup", "list-backup-sets")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    # Gap 14: include full scope in JSON output
    sets_data = []
    for name, bs in config.backup_sets.items():
        sets_data.append({
            "name": name,
            "description": getattr(bs, "description", ""),
            "containers": bs.containers,
            "scope": {
                "volumes": bs.scope.volumes,
                "configs": bs.scope.configs,
                "networks": bs.scope.networks,
                "filesystems": getattr(bs.scope, "filesystems", False),
            },
        })

    render_output({"sets": sets_data}, output, "list-backup-sets")

    if output != "json":
        tui = BackupTUI(config)
        tui.show_header("Backup Sets")
        if not config.backup_sets:
            console.print("[yellow]No backup sets configured[/yellow]")
            return
        for name, bs in config.backup_sets.items():
            console.print(f"\n[bold cyan]{name}[/bold cyan]")
            console.print(f"  Description: {getattr(bs, 'description', '')}")
            console.print(f"  Containers: {', '.join(bs.containers)}")


# ---------------------------------------------------------------------------
# list-backups
# ---------------------------------------------------------------------------

@cli.command("list-backups")
@click.option("--backup-dir", "-d", type=click.Path(exists=True, path_type=Path),
              help="Backup directory to list (default: staging directory)")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def list_backups(ctx, backup_dir, skills, output, input_json):
    """List available local backups."""
    if skills:
        _print_command_skills("bbackup", "list-backups")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    backup_path = Path(backup_dir) if backup_dir else Path(config.get_staging_dir())
    backups = list_available_backups(backup_path)

    backups_data = [
        {
            "name": b["name"],
            "timestamp": b.get("timestamp", ""),
            "path": str(b["path"]),
            "size_bytes": b.get("size_bytes", 0),
        }
        for b in backups
    ]

    render_output({"backups": backups_data}, output, "list-backups")

    if output != "json":
        if not backups:
            console.print(f"[yellow]No backups found in {backup_path}[/yellow]")
            return
        console.print(f"\n[bold]Available Backups in {backup_path}:[/bold]\n")
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Backup Name", style="cyan", width=30)
        table.add_column("Timestamp", style="dim", width=25)
        table.add_column("Path", style="dim", width=40)
        for b in backups:
            table.add_row(b["name"], b.get("timestamp", ""), str(b["path"]))
        console.print(table)


# ---------------------------------------------------------------------------
# list-remote-backups
# ---------------------------------------------------------------------------

@cli.command("list-remote-backups")
@click.option("--remote", "-r", required=True, help="Remote storage name to list backups from")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def list_remote_backups(ctx, remote, skills, output, input_json):
    """List available backups on remote storage."""
    if skills:
        _print_command_skills("bbackup", "list-remote-backups")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    if remote not in config.remotes:
        json_error("list-remote-backups", f"Remote '{remote}' not found in configuration", EXIT_USER_ERROR, output)

    remote_storage = config.remotes[remote]
    if not remote_storage.enabled:
        json_error("list-remote-backups", f"Remote '{remote}' is not enabled", EXIT_USER_ERROR, output)

    remote_mgr = RemoteStorageManager(config)
    backups = remote_mgr.list_backups(remote_storage)

    backups_data = [
        {"name": b, "remote_path": f"{remote_storage.path}/{b}"}
        for b in backups
    ]

    render_output({"remote": remote, "backups": backups_data}, output, "list-remote-backups")

    if output != "json":
        if not backups:
            console.print(f"[yellow]No backups found on remote '{remote}'[/yellow]")
            return
        console.print(f"\n[bold]Available Backups on Remote '{remote}':[/bold]\n")
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Backup Name", style="cyan", width=40)
        table.add_column("Remote Path", style="dim", width=50)
        for b in backups:
            table.add_row(b, f"{remote_storage.path}/{b}")
        console.print(table)


# ---------------------------------------------------------------------------
# list-filesystem-sets
# ---------------------------------------------------------------------------

@cli.command("list-filesystem-sets")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def list_filesystem_sets(ctx, skills, output, input_json):
    """List configured filesystem backup sets."""
    if skills:
        _print_command_skills("bbackup", "list-filesystem-sets")
    merge_json_input(ctx, input_json)

    config: Config = ctx.obj["config"]
    console: Console = ctx.obj["console"]

    sets_data = []
    for name, fs_set in config.filesystem_sets.items():
        sets_data.append({
            "name": name,
            "description": getattr(fs_set, "description", ""),
            "targets": [
                {
                    "name": t.name,
                    "path": t.path,
                    "enabled": t.enabled,
                    "excludes": t.excludes,
                }
                for t in fs_set.targets
            ],
        })

    render_output({"sets": sets_data}, output, "list-filesystem-sets")

    if output != "json":
        if not config.filesystem_sets:
            console.print("[yellow]No filesystem backup sets configured[/yellow]")
            return
        for name, fs_set in config.filesystem_sets.items():
            console.print(f"\n[bold cyan]{name}[/bold cyan]: {getattr(fs_set, 'description', '')}")
            for t in fs_set.targets:
                status_str = "[green]enabled[/green]" if t.enabled else "[dim]disabled[/dim]"
                console.print(f"  {status_str} {t.path}")
                for excl in t.excludes:
                    console.print(f"    [dim]exclude: {excl}[/dim]")


# ---------------------------------------------------------------------------
# init-config
# ---------------------------------------------------------------------------

@cli.command("init-config")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def init_config(ctx, skills, output, input_json):
    """Initialize configuration file from the bundled example template."""
    if skills:
        _print_command_skills("bbackup", "init-config")
    merge_json_input(ctx, input_json)

    console: Console = ctx.obj["console"]

    config_path = os.path.expanduser("~/.config/bbackup/config.yaml")
    config_dir = os.path.dirname(config_path)
    os.makedirs(config_dir, exist_ok=True)

    if resource_exists("config.yaml.example"):
        Path(config_path).write_text(read_text_resource("config.yaml.example"), encoding="utf-8")
        render_output({"config_path": config_path, "created": True}, output, "init-config")
        if output != "json":
            console.print(f"[green]Configuration file created: {config_path}[/green]")
            console.print("[dim]Edit the file to configure your backup settings.[/dim]")
    else:
        render_output({"config_path": config_path, "created": False}, output, "init-config",
                      success=False, errors=["Example config template not found"])
        if output != "json":
            sys.stderr.write("Error: Example config not found\n")
        sys.exit(EXIT_SYSTEM_ERROR)


# ---------------------------------------------------------------------------
# init-encryption
# ---------------------------------------------------------------------------

@cli.command("init-encryption")
@click.option("--method", type=click.Choice(["symmetric", "asymmetric", "both"]),
              default="symmetric", help="Encryption method to use")
@click.option("--key-path", type=click.Path(), help="Directory to save key(s) (default: ~/.config/bbackup/)")
@click.option("--password", help="Password for key encryption (not currently supported)")
@click.option("--algorithm", type=click.Choice(["rsa-4096"]), default="rsa-4096",
              help="Algorithm for asymmetric keys")
@click.option("--upload-github", is_flag=True, help="Remind about uploading public key to GitHub")
@click.option(
    "--skills",
    is_flag=True,
    help="Show skills documentation for this command and exit.",
)
@output_option
@input_json_option
@click.pass_context
def init_encryption(ctx, method, key_path, password, algorithm, upload_github, skills, output, input_json):
    """Initialize encryption keys for backup at-rest protection."""
    if skills:
        _print_command_skills("bbackup", "init-encryption")
    merge_json_input(ctx, input_json)

    console: Console = ctx.obj["console"]

    if password:
        json_error(
            "init-encryption",
            "--password is not currently supported for generated keys",
            EXIT_USER_ERROR,
            output,
        )

    key_dir = Path(key_path).expanduser() if key_path else Path.home() / ".config" / "bbackup"
    key_dir.mkdir(parents=True, exist_ok=True)

    if output != "json":
        console.print("\n[bold cyan]Initializing encryption keys...[/bold cyan]\n")

    result_data: dict = {"method": method, "key_paths": {}, "config_snippet": "", "success": False}

    try:
        if method in ("symmetric", "both"):
            if output != "json":
                console.print("[yellow]Generating symmetric key...[/yellow]")
            symmetric_key = EncryptionManager.generate_symmetric_key()
            key_file = key_dir / "encryption.key"
            key_file.write_bytes(symmetric_key)
            key_file.chmod(0o600)
            result_data["key_paths"]["symmetric"] = str(key_file)
            if output != "json":
                console.print(f"[green]x[/green] Symmetric key saved to: {key_file}")
                console.print(f"   Key size: {len(symmetric_key)} bytes")

        if method in ("asymmetric", "both"):
            if output != "json":
                console.print(f"[yellow]Generating asymmetric keypair ({algorithm})...[/yellow]")
            public_pem, private_pem = EncryptionManager.generate_keypair(algorithm)
            public_key_file = key_dir / "backup_public.pem"
            private_key_file = key_dir / "backup_private.pem"
            public_key_file.write_bytes(public_pem)
            private_key_file.write_bytes(private_pem)
            public_key_file.chmod(0o644)
            private_key_file.chmod(0o600)
            result_data["key_paths"]["public"] = str(public_key_file)
            result_data["key_paths"]["private"] = str(private_key_file)
            if output != "json":
                console.print(f"[green]x[/green] Public key saved to: {public_key_file}")
                console.print(f"[green]x[/green] Private key saved to: {private_key_file}")
                console.print("   [red]WARNING:[/red] Keep private key secure. Never share or upload it.")

        # Gap 6: build machine-readable config snippet; skip prose in JSON mode
        if method in ("symmetric", "both"):
            result_data["config_snippet"] = (
                f"encryption:\n  enabled: true\n  method: symmetric\n"
                f"  symmetric:\n    key_file: {result_data['key_paths'].get('symmetric', '')}\n"
            )
        elif method == "asymmetric":
            result_data["config_snippet"] = (
                f"encryption:\n  enabled: true\n  method: asymmetric\n"
                f"  asymmetric:\n"
                f"    public_key: {result_data['key_paths'].get('public', '')}\n"
                f"    private_key: {result_data['key_paths'].get('private', '')}\n"
                f"    algorithm: {algorithm}\n"
            )

        result_data["success"] = True
        render_output(result_data, output, "init-encryption", success=True)

        if output != "json":
            console.print("\n[bold green]Keys generated successfully![/bold green]\n")
            console.print("[bold]Next steps:[/bold]")
            console.print(f"  Add the following to your config.yaml:\n\n{result_data['config_snippet']}")

    except Exception as e:
        render_output(result_data, output, "init-encryption", success=False, errors=[str(e)])
        if output != "json":
            sys.stderr.write(f"Error generating keys: {e}\n")
        sys.exit(EXIT_SYSTEM_ERROR)


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

@cli.command("skills")
@click.argument("skill_id", required=False)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["json", "markdown"]),
    default="json",
    help="Output as JSON (default) or Markdown skills catalog.",
)
@output_option
def skills(skill_id, format_, output):
    """List available skills for AI agent discovery, or dump the Markdown skills catalog."""
    if format_ == "markdown":
        _print_skills_markdown()
        return

    result = get_skill("bbackup", skill_id)
    if result is None:
        json_error(
            "skills",
            f"Unknown skill: {skill_id!r}. Run 'bbackup skills' for valid ids.",
            EXIT_USER_ERROR,
            output,
        )

    # Skills command always emits JSON directly (not via envelope) for level-0 discovery;
    # For level-1 detail we still use the standard envelope.
    if skill_id is None:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    else:
        render_output(result, output, "skills")
        if output != "json":
            console_inst = Console()
            console_inst.print(f"\n[bold cyan]{result['id']}[/bold cyan]: {result['summary']}")
            console_inst.print(f"\nWorkflow: {' -> '.join(result.get('workflow', []))}")
            console_inst.print("\nExamples:")
            for ex in result.get("examples", []):
                console_inst.print(f"  [dim]{ex}[/dim]")


def _print_command_skills(cli_name: str, command_name: str) -> None:
    """
    Print the skills documentation section for a specific CLI command and exit.
    """
    if not resource_exists(SKILLS_DOC_RESOURCE) or not resource_exists(SKILLS_INDEX_RESOURCE):
        sys.stderr.write("Skills documentation has not been generated yet.\n")
        sys.exit(EXIT_SYSTEM_ERROR)

    try:
        index = json.loads(read_text_resource(SKILLS_INDEX_RESOURCE))
    except Exception as exc:
        sys.stderr.write(f"Failed to read skills index: {exc}\n")
        sys.exit(EXIT_SYSTEM_ERROR)

    cmd_id = f"{cli_name}:{command_name}"
    meta = index.get(cmd_id)
    if not meta:
        sys.stderr.write(f"No skills entry found for command {cmd_id}.\n")
        sys.exit(EXIT_USER_ERROR)

    lines = read_text_resource(SKILLS_DOC_RESOURCE).splitlines()
    start = max(int(meta.get("start", 1)) - 1, 0)
    end = min(int(meta.get("end", len(lines))), len(lines))
    section = "\n".join(lines[start:end]) + "\n"
    sys.stdout.write(section)
    sys.exit(EXIT_SUCCESS)


def _print_skills_markdown() -> None:
    """
    Print the full Markdown skills catalog to stdout and exit.
    """
    if not resource_exists(SKILLS_DOC_RESOURCE):
        sys.stderr.write("Skills documentation has not been generated yet.\n")
        sys.exit(EXIT_SYSTEM_ERROR)
    sys.stdout.write(read_text_resource(SKILLS_DOC_RESOURCE))
    sys.exit(EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

from .production_cli import cli as production_cli

cli.add_command(production_cli, name="production")


if __name__ == "__main__":
    cli()
