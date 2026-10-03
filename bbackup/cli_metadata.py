"""
bbackup/cli_metadata.py
Purpose: Structured metadata for all bbackup and bbman CLI commands, parameters,
         JSON fields, and examples. This is the single source of truth for the
         skills ecosystem, the generated Markdown catalog, and future tooling.
Created: 2026-03-04
Last Updated: 2026-03-04
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


CliName = Literal["bbackup", "bbman"]
ParamKind = Literal["flag", "positional", "json_field", "env_var"]
ShapeKind = Literal["scalar", "enum", "list", "map", "object"]


@dataclass
class Parameter:
    """
    Canonical description of a single parameter exposed by a CLI command.

    This covers both CLI flags/positionals and JSON / env inputs so that a
    single registry can drive skills JSON, Markdown docs, and future tooling.
    """

    name: str
    kind: ParamKind
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Optional[Any] = None
    allowed_values: Optional[List[Any]] = None
    shape: ShapeKind = "scalar"
    cli_flag: Optional[str] = None
    json_key: Optional[str] = None
    env_var: Optional[str] = None
    aliases: List[str] = field(default_factory=list)


@dataclass
class Example:
    """
    Example usage for a command in both CLI and JSON forms.
    """

    description: str
    cli: Optional[str] = None
    input_json: Optional[Dict[str, Any]] = None


@dataclass
class CliCommand:
    """
    Structured description of a single CLI command.
    """

    cli: CliName
    name: str
    summary: str
    description: str
    category: str
    parameters: List[Parameter] = field(default_factory=list)
    examples: List[Example] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.cli}:{self.name}"


# ---------------------------------------------------------------------------
# bbackup command registry (normalized surface)
# ---------------------------------------------------------------------------


BBACKUP_COMMANDS: Dict[str, CliCommand] = {}


def _register_bbackup(cmd: CliCommand) -> None:
    BBACKUP_COMMANDS[cmd.id] = cmd


def _snapshot_common_parameters(
    *,
    profile_required: bool = True,
    dry_run: bool = False,
    repo_id: bool = False,
    snapshot_id: bool = False,
    target: bool = False,
    include_path: bool = False,
    read_data_subset: bool = False,
) -> List[Parameter]:
    params = [
        Parameter(
            name="profile",
            kind="flag",
            type="string",
            description="Snapshot profile name from config.yaml.",
            cli_flag="--profile",
            json_key="profile",
            required=profile_required,
        ),
    ]
    if repo_id:
        params.append(Parameter(
            name="repo_id",
            kind="flag",
            type="string",
            description="Tracked Git repository ID from the snapshot state ledger.",
            cli_flag="--repo-id",
            json_key="repo_id",
            required=True,
        ))
    if snapshot_id:
        params.append(Parameter(
            name="snapshot_id",
            kind="flag",
            type="string",
            description="Restic snapshot ID or snapshot:path selector to restore.",
            cli_flag="--snapshot-id",
            json_key="snapshot_id",
            required=True,
        ))
    if target:
        params.append(Parameter(
            name="target",
            kind="flag",
            type="path",
            description="Empty restore target directory.",
            cli_flag="--target",
            json_key="target",
            required=True,
        ))
    if include_path:
        params.append(Parameter(
            name="include_path",
            kind="flag",
            type="string",
            description="Optional restic include filter for targeted restore.",
            cli_flag="--include",
            json_key="include_path",
        ))
    if read_data_subset:
        params.append(Parameter(
            name="read_data_subset",
            kind="flag",
            type="string",
            description="Optional restic check --read-data-subset value, such as 5%.",
            cli_flag="--read-data-subset",
            json_key="read_data_subset",
        ))
    if dry_run:
        params.append(Parameter(
            name="dry_run",
            kind="flag",
            type="bool",
            description="Return the resolved restic plan without executing it.",
            cli_flag="--dry-run",
            json_key="dry_run",
            default=False,
        ))
    params.extend([
        Parameter(
            name="output",
            kind="flag",
            type="string",
            description="Output format: text or json.",
            cli_flag="--output",
            json_key="output",
            allowed_values=["text", "json"],
            shape="enum",
        ),
        Parameter(
            name="input_json",
            kind="json_field",
            type="object",
            description="Flat JSON object providing all parameters.",
            shape="object",
        ),
    ])
    return params


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="backup",
        summary="Create Docker and/or filesystem backup.",
        description=(
            "Back up one or more Docker containers and optional filesystem paths. "
            "Supports incremental rsync (--link-dest), multiple remotes, and "
            "non-interactive JSON-driven operation."
        ),
        category="backup",
        parameters=[
            Parameter(
                name="containers",
                kind="flag",
                type="string[]",
                description="Container names to back up (repeatable).",
                cli_flag="--containers",
                json_key="containers",
                shape="list",
            ),
            Parameter(
                name="backup_set",
                kind="flag",
                type="string",
                description="Named backup set from config.yaml.",
                cli_flag="--backup-set",
                json_key="backup_set",
            ),
            Parameter(
                name="config_only",
                kind="flag",
                type="bool",
                description="Back up only container configs (no volumes).",
                cli_flag="--config-only",
                json_key="config_only",
                default=False,
            ),
            Parameter(
                name="volumes_only",
                kind="flag",
                type="bool",
                description="Back up only volumes (no configs).",
                cli_flag="--volumes-only",
                json_key="volumes_only",
                default=False,
            ),
            Parameter(
                name="no_networks",
                kind="flag",
                type="bool",
                description="Skip network backups.",
                cli_flag="--no-networks",
                json_key="no_networks",
                default=False,
            ),
            Parameter(
                name="incremental",
                kind="flag",
                type="bool",
                description="Enable incremental backup via rsync --link-dest.",
                cli_flag="--incremental",
                json_key="incremental",
                default=False,
            ),
            Parameter(
                name="no_interactive",
                kind="flag",
                type="bool",
                description="Disable TUI and prompts; required for agent use.",
                cli_flag="--no-interactive",
                json_key="no_interactive",
                env_var="BBACKUP_NO_INTERACTIVE",
                default=False,
            ),
            Parameter(
                name="solid_archive",
                kind="flag",
                type="bool",
                description="Create a single compressed tarball for upload (overrides config backup.solid_archive). Use --no-solid-archive to disable.",
                cli_flag="--solid-archive/--no-solid-archive",
                json_key="solid_archive_flag",
                default=None,
            ),
            Parameter(
                name="remote",
                kind="flag",
                type="string[]",
                description="Remote storage destinations (repeatable).",
                cli_flag="--remote",
                json_key="remote",
                shape="list",
            ),
            Parameter(
                name="paths",
                kind="flag",
                type="string[]",
                description="Filesystem paths to back up (repeatable).",
                cli_flag="--paths",
                json_key="paths",
                shape="list",
            ),
            Parameter(
                name="exclude",
                kind="flag",
                type="string[]",
                description="Exclude patterns for filesystem backup (repeatable).",
                cli_flag="--exclude",
                json_key="exclude",
                shape="list",
            ),
            Parameter(
                name="filesystem_set",
                kind="flag",
                type="string",
                description="Named filesystem backup set from config.yaml.",
                cli_flag="--filesystem-set",
                json_key="filesystem_set",
            ),
            Parameter(
                name="dry_run",
                kind="flag",
                type="bool",
                description="Resolve targets and return a plan without executing.",
                cli_flag="--dry-run",
                json_key="dry_run",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                json_key=None,
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Backup specific containers non-interactively with JSON output.",
                cli="bbackup backup --containers myapp --no-interactive --output json",
            ),
            Example(
                description="Incremental backup of a named backup set to a remote.",
                cli="bbackup backup --backup-set production --incremental --remote gdrive --no-interactive --output json",
            ),
            Example(
                description="JSON-driven backup of two containers.",
                cli='bbackup backup --input-json \'{\"containers\":[\"myapp\",\"mydb\"],\"incremental\":true,\"no_interactive\":true}\' --output json',
                input_json={
                    "containers": ["myapp", "mydb"],
                    "incremental": True,
                    "no_interactive": True,
                    "output": "json",
                },
            ),
            Example(
                description="Dry-run to see what would be backed up.",
                cli="bbackup backup --backup-set production --dry-run --no-interactive --output json",
                input_json={"backup_set": "production", "dry_run": True, "no_interactive": True, "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot plan",
        summary="Resolve a native snapshot profile without executing restic.",
        description=(
            "Discover Git repositories and explicit paths for a snapshot profile, "
            "then return the restic commands and safety alerts without persisting state."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(),
        examples=[
            Example(
                description="Plan a native snapshot profile with JSON output.",
                cli="bbackup snapshot plan --profile essentials-daily --output json",
                input_json={"profile": "essentials-daily", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot init",
        summary="Initialize the restic repository for a snapshot profile.",
        description=(
            "Initialize the configured restic repository after profile safety preflight. "
            "Use dry-run mode to inspect the exact restic init command first."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(dry_run=True),
        examples=[
            Example(
                description="Inspect the restic init command without executing it.",
                cli="bbackup snapshot init --profile essentials-daily --dry-run --output json",
                input_json={"profile": "essentials-daily", "dry_run": True, "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot run",
        summary="Run a native restic snapshot profile.",
        description=(
            "Discover active Git repositories and configured paths, enforce safety "
            "preflight for non-dry runs, run restic backup, and record successful "
            "repository snapshot IDs in the state ledger."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(dry_run=True),
        examples=[
            Example(
                description="Dry-run a profile before taking a snapshot.",
                cli="bbackup snapshot run --profile essentials-daily --dry-run --output json",
                input_json={"profile": "essentials-daily", "dry_run": True, "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot check",
        summary="Run a non-destructive restic repository check.",
        description=(
            "Check a native snapshot repository, optionally with restic "
            "--read-data-subset for verification schedules."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(dry_run=True, read_data_subset=True),
        examples=[
            Example(
                description="Run a lightweight repository check.",
                cli="bbackup snapshot check --profile essentials-daily --output json",
                input_json={"profile": "essentials-daily", "output": "json"},
            ),
            Example(
                description="Run a verification check over a subset of pack data.",
                cli="bbackup snapshot check --profile essentials-daily --read-data-subset 5% --output json",
                input_json={"profile": "essentials-daily", "read_data_subset": "5%", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot restore",
        summary="Restore a restic snapshot into an empty target directory.",
        description=(
            "Restore a snapshot or snapshot:path selector into an empty target. "
            "Dry-run mode returns the restic restore command without contacting Docker."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(
            dry_run=True,
            snapshot_id=True,
            target=True,
            include_path=True,
        ),
        examples=[
            Example(
                description="Plan a targeted restore from a snapshot.",
                cli="bbackup snapshot restore --profile essentials-daily --snapshot-id latest --target /tmp/restore --dry-run --output json",
                input_json={
                    "profile": "essentials-daily",
                    "snapshot_id": "latest",
                    "target": "/tmp/restore",
                    "dry_run": True,
                    "output": "json",
                },
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot retire",
        summary="Mark a tracked repository retired after it has a successful snapshot.",
        description=(
            "Retire a repository in the snapshot state ledger so future purge planning "
            "can remain explicit and dry-run-first."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(repo_id=True),
        examples=[
            Example(
                description="Retire a repository ID after verifying it has a successful snapshot.",
                cli="bbackup snapshot retire --profile essentials-daily --repo-id abc123 --output json",
                input_json={"profile": "essentials-daily", "repo_id": "abc123", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot purge-plan",
        summary="Build a dry-run-first purge plan for a retired repository.",
        description=(
            "Return restic forget arguments for a retired repo ID. The command keeps "
            "destructive cleanup manual by exposing dry-run and post-confirmation arguments separately."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(repo_id=True),
        examples=[
            Example(
                description="Prepare a manual purge plan for a retired repository.",
                cli="bbackup snapshot purge-plan --profile essentials-daily --repo-id abc123 --output json",
                input_json={"profile": "essentials-daily", "repo_id": "abc123", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="snapshot schedule",
        summary="Render user systemd units and timers for a snapshot profile.",
        description=(
            "Render matching service/timer units for daily snapshot runs, weekly "
            "non-destructive checks, and monthly verification checks."
        ),
        category="snapshot",
        parameters=_snapshot_common_parameters(),
        examples=[
            Example(
                description="Render schedule units for manual installation.",
                cli="bbackup snapshot schedule --profile essentials-daily --output json",
                input_json={"profile": "essentials-daily", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="restore",
        summary="Restore containers, volumes, networks, or filesystem paths from a backup.",
        description=(
            "Restore Docker resources and filesystem targets from a backup directory. "
            "Supports full restores, targeted restores, rename mappings, and dry-run mode."
        ),
        category="restore",
        parameters=[
            Parameter(
                name="backup_path",
                kind="flag",
                type="path",
                description="Path to the backup directory.",
                cli_flag="--backup-path",
                json_key="backup_path",
                required=True,
            ),
            Parameter(
                name="all",
                kind="flag",
                type="bool",
                description="Restore all items from the backup.",
                cli_flag="--all",
                json_key="all",
                default=False,
            ),
            Parameter(
                name="containers",
                kind="flag",
                type="string[]",
                description="Specific container names to restore (repeatable).",
                cli_flag="--containers",
                json_key="containers",
                shape="list",
            ),
            Parameter(
                name="volumes",
                kind="flag",
                type="string[]",
                description="Specific volume names to restore (repeatable).",
                cli_flag="--volumes",
                json_key="volumes",
                shape="list",
            ),
            Parameter(
                name="networks",
                kind="flag",
                type="string[]",
                description="Specific network names to restore (repeatable).",
                cli_flag="--networks",
                json_key="networks",
                shape="list",
            ),
            Parameter(
                name="filesystem",
                kind="flag",
                type="string[]",
                description="Filesystem target names to restore (repeatable).",
                cli_flag="--filesystem",
                json_key="filesystem",
                shape="list",
            ),
            Parameter(
                name="filesystem_destination",
                kind="flag",
                type="path",
                description="Destination path for filesystem restore.",
                cli_flag="--filesystem-destination",
                json_key="filesystem_destination",
            ),
            Parameter(
                name="rename",
                kind="flag",
                type="string[]",
                description="Rename mappings in old:new format (repeatable).",
                cli_flag="--rename",
                json_key="rename",
                shape="list",
            ),
            Parameter(
                name="dry_run",
                kind="flag",
                type="bool",
                description="Return a restore plan without executing.",
                cli_flag="--dry-run",
                json_key="dry_run",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Restore everything from a backup directory.",
                cli="bbackup restore --backup-path /tmp/bbackup/backup_20260227_120000 --all --output json",
            ),
            Example(
                description="Restore a single container from a backup using JSON input.",
                cli='bbackup restore --input-json \'{"backup_path":"/tmp/bbackup/backup_20260227_120000","containers":["myapp"]}\' --output json',
                input_json={
                    "backup_path": "/tmp/bbackup/backup_20260227_120000",
                    "containers": ["myapp"],
                    "output": "json",
                },
            ),
            Example(
                description="Dry-run restore to inspect what would be restored.",
                cli="bbackup restore --backup-path /tmp/bbackup/backup_20260227_120000 --all --dry-run --output json",
                input_json={
                    "backup_path": "/tmp/bbackup/backup_20260227_120000",
                    "all": True,
                    "dry_run": True,
                    "output": "json",
                },
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="list-containers",
        summary="List all Docker containers.",
        description="List Docker containers with id, name, status, and image for inspection or backup planning.",
        category="inspect",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                json_key=None,
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="List all containers with JSON details.",
                cli="bbackup list-containers --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="list-backup-sets",
        summary="List available backup sets.",
        description="List named backup sets from config with containers and scope.",
        category="inspect",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="List backup sets with JSON output.",
                cli="bbackup list-backup-sets --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="list-backups",
        summary="List available local backups.",
        description="List local backup directories in the staging directory or a specified location.",
        category="inspect",
        parameters=[
            Parameter(
                name="backup_dir",
                kind="flag",
                type="path",
                description="Backup directory to list (default: staging directory).",
                cli_flag="--backup-dir",
                json_key="backup_dir",
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="List local backups with JSON output.",
                cli="bbackup list-backups --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="list-remote-backups",
        summary="List backups stored on a configured remote.",
        description="List available backups on a configured remote storage destination.",
        category="inspect",
        parameters=[
            Parameter(
                name="remote",
                kind="flag",
                type="string",
                description="Remote storage name to list backups from.",
                cli_flag="--remote",
                json_key="remote",
                required=True,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="List remote backups on a given remote.",
                cli="bbackup list-remote-backups --remote gdrive --output json",
                input_json={"remote": "gdrive", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="list-filesystem-sets",
        summary="List configured filesystem backup sets.",
        description="List filesystem backup sets defined in config with targets and excludes.",
        category="inspect",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="List filesystem backup sets with JSON output.",
                cli="bbackup list-filesystem-sets --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="init-config",
        summary="Initialize configuration file from the bundled example template.",
        description="Create an example config.yaml in ~/.config/bbackup/ from the bundled template.",
        category="lifecycle",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Initialize a starter config file.",
                cli="bbackup init-config --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="init-encryption",
        summary="Initialize encryption keys for backup at-rest protection.",
        description="Generate symmetric and/or asymmetric keys for encrypting backups at rest and return a config snippet.",
        category="encryption",
        parameters=[
            Parameter(
                name="method",
                kind="flag",
                type="string",
                description="Encryption method to use.",
                cli_flag="--method",
                json_key="method",
                allowed_values=["symmetric", "asymmetric", "both"],
                shape="enum",
                default="symmetric",
            ),
            Parameter(
                name="key_path",
                kind="flag",
                type="path",
                description="Directory to save key(s) (default: ~/.config/bbackup/).",
                cli_flag="--key-path",
                json_key="key_path",
            ),
            Parameter(
                name="password",
                kind="flag",
                type="string",
                description="Not currently supported for generated keys; command fails if provided.",
                cli_flag="--password",
                json_key="password",
            ),
            Parameter(
                name="algorithm",
                kind="flag",
                type="string",
                description="Algorithm for asymmetric keys.",
                cli_flag="--algorithm",
                json_key="algorithm",
                allowed_values=["rsa-4096"],
                shape="enum",
                default="rsa-4096",
            ),
            Parameter(
                name="upload_github",
                kind="flag",
                type="bool",
                description="Remind about uploading public key to GitHub.",
                cli_flag="--upload-github",
                json_key="upload_github",
                default=False,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Generate asymmetric keys with JSON output.",
                cli="bbackup init-encryption --method asymmetric --algorithm rsa-4096 --output json",
                input_json={"method": "asymmetric", "algorithm": "rsa-4096", "output": "json"},
            ),
        ],
    )
)


_register_bbackup(
    CliCommand(
        cli="bbackup",
        name="skills",
        summary="List available bbackup skills for AI agent discovery.",
        description="List or inspect bbackup skills in JSON or Markdown formats.",
        category="skills",
        parameters=[
            Parameter(
                name="skill_id",
                kind="positional",
                type="string",
                description="Optional skill id for detailed view.",
            ),
            Parameter(
                name="format",
                kind="flag",
                type="string",
                description="Output as JSON or Markdown skills catalog.",
                cli_flag="--format",
                json_key="format",
                allowed_values=["json", "markdown"],
                shape="enum",
                default="json",
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format for detailed skill view (text or json).",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
        ],
        examples=[
            Example(
                description="List all bbackup skills in JSON.",
                cli="bbackup skills",
            ),
            Example(
                description="Dump the full Markdown skills catalog.",
                cli="bbackup skills --format markdown",
            ),
        ],
    )
)


# ---------------------------------------------------------------------------
# bbman command registry (normalized surface)
# ---------------------------------------------------------------------------


BBMAN_COMMANDS: Dict[str, CliCommand] = {}


def _register_bbman(cmd: CliCommand) -> None:
    BBMAN_COMMANDS[cmd.id] = cmd


_register_bbman(
    CliCommand(
        cli="bbman",
        name="setup",
        summary="Run interactive setup wizard for first-time configuration.",
        description=(
            "Run the interactive setup wizard to create an initial config.yaml. "
            "In agent mode, use --no-interactive with BBACKUP_NO_INTERACTIVE=1 "
            "to query current state instead of running the wizard."
        ),
        category="lifecycle",
        parameters=[
            Parameter(
                name="no_interactive",
                kind="flag",
                type="bool",
                description="Skip wizard; return current config state (agent mode).",
                cli_flag="--no-interactive",
                json_key="no_interactive",
                env_var="BBACKUP_NO_INTERACTIVE",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Run setup in non-interactive mode for an agent.",
                cli="bbman setup --no-interactive --output json",
                input_json={"no_interactive": True, "output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="health",
        summary="Run comprehensive health check (Docker, rsync, rclone, Python packages).",
        description=(
            "Check Docker connectivity, system tools, Python dependencies, and "
            "configuration health. Designed for both human and agent consumption."
        ),
        category="lifecycle",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Run health check with JSON result.",
                cli="bbman health --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="check-deps",
        summary="Check and optionally install missing dependencies.",
        description="Check required and optional system and Python dependencies, optionally installing missing ones.",
        category="lifecycle",
        parameters=[
            Parameter(
                name="install",
                kind="flag",
                type="bool",
                description="Install missing packages.",
                cli_flag="--install",
                json_key="install",
                default=False,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Check dependencies only.",
                cli="bbman check-deps --output json",
                input_json={"output": "json"},
            ),
            Example(
                description="Check and install missing dependencies.",
                cli="bbman check-deps --install --output json",
                input_json={"install": True, "output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="validate-config",
        summary="Validate configuration file.",
        description="Validate config.yaml and report backup sets, remotes, and encryption status.",
        category="lifecycle",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Validate configuration file with JSON output.",
                cli="bbman validate-config --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="status",
        summary="Show backup status and history.",
        description="Show backup statistics and history, suitable for both humans and agents.",
        category="lifecycle",
        parameters=[
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Show backup status with JSON output.",
                cli="bbman status --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="cleanup",
        summary="Cleanup old files and backups.",
        description="Remove old staging, log, backup, and temp files according to retention parameters.",
        category="maintenance",
        parameters=[
            Parameter(
                name="staging_days",
                kind="flag",
                type="int",
                description="Keep staging files newer than N days (default 7).",
                cli_flag="--staging-days",
                json_key="staging_days",
                default=7,
            ),
            Parameter(
                name="log_days",
                kind="flag",
                type="int",
                description="Keep log files newer than N days (default 30).",
                cli_flag="--log-days",
                json_key="log_days",
                default=30,
            ),
            Parameter(
                name="no_backups",
                kind="flag",
                type="bool",
                description="Do not cleanup old backups.",
                cli_flag="--no-backups",
                json_key="no_backups",
                default=False,
            ),
            Parameter(
                name="no_temp",
                kind="flag",
                type="bool",
                description="Do not cleanup temporary files.",
                cli_flag="--no-temp",
                json_key="no_temp",
                default=False,
            ),
            Parameter(
                name="yes",
                kind="flag",
                type="bool",
                description="Skip confirmation prompt.",
                cli_flag="--yes",
                json_key="yes",
                default=False,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Cleanup with default retention settings and JSON output.",
                cli="bbman cleanup --yes --output json",
                input_json={"yes": True, "output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="diagnostics",
        summary="Run diagnostics and optionally save report to file.",
        description="Run diagnostics and optionally save a detailed report to file for troubleshooting.",
        category="maintenance",
        parameters=[
            Parameter(
                name="report_file",
                kind="flag",
                type="path",
                description="Save diagnostics report to this file path.",
                cli_flag="--report-file",
                json_key="report_file",
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Run diagnostics and return JSON summary.",
                cli="bbman diagnostics --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="check-updates",
        summary="Check for updates (file-level comparison with checksums).",
        description="Check whether the installed version is behind the configured repository.",
        category="updates",
        parameters=[
            Parameter(
                name="branch",
                kind="flag",
                type="string",
                description="Branch to check (default: main).",
                cli_flag="--branch",
                json_key="branch",
                default="main",
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Check for updates on main branch.",
                cli="bbman check-updates --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="update",
        summary="Update application files.",
        description="Update the local installation from the configured repository using git or download methods.",
        category="updates",
        parameters=[
            Parameter(
                name="branch",
                kind="flag",
                type="string",
                description="Branch to update from (default: main).",
                cli_flag="--branch",
                json_key="branch",
                default="main",
            ),
            Parameter(
                name="method",
                kind="flag",
                type="string",
                description="Update method (git or download).",
                cli_flag="--method",
                json_key="method",
                allowed_values=["git", "download"],
                shape="enum",
                default="git",
            ),
            Parameter(
                name="yes",
                kind="flag",
                type="bool",
                description="Skip confirmation prompt.",
                cli_flag="--yes",
                json_key="yes",
                default=False,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Update non-interactively using git.",
                cli="bbman update --yes --output json",
                input_json={"yes": True, "output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="repo-url",
        summary="Show or set the repository URL override.",
        description="Show or update the repository URL used for update checks and downloads.",
        category="updates",
        parameters=[
            Parameter(
                name="url",
                kind="flag",
                type="string",
                description="Set repository URL override.",
                cli_flag="--url",
                json_key="url",
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Show current repository URL in JSON.",
                cli="bbman repo-url --output json",
                input_json={"output": "json"},
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="auth-gdrive",
        summary="Authorize Google Drive and configure an rclone Drive remote.",
        description=(
            "Run Google Desktop app loopback OAuth with the optional gdrive-auth extra, "
            "then create or update a dedicated rclone Drive remote. bbackup still uses "
            "rclone for transfers; this command only sets up OAuth and rclone config."
        ),
        category="integration",
        parameters=[
            Parameter(
                name="client_secrets",
                kind="flag",
                type="path",
                description="Path to a Google Desktop app OAuth client_secret.json file.",
                cli_flag="--client-secrets",
                json_key="client_secrets",
                required=True,
            ),
            Parameter(
                name="remote",
                kind="flag",
                type="string",
                description="rclone remote name to create or update.",
                cli_flag="--remote",
                json_key="remote",
                default="bbackup-gdrive",
            ),
            Parameter(
                name="scope",
                kind="flag",
                type="string",
                description="rclone Google Drive scope name.",
                cli_flag="--scope",
                json_key="scope",
                default="drive",
            ),
            Parameter(
                name="port",
                kind="flag",
                type="int",
                description="Loopback OAuth callback port on 127.0.0.1.",
                cli_flag="--port",
                json_key="port",
                default=53682,
            ),
            Parameter(
                name="timeout",
                kind="flag",
                type="int",
                description="OAuth local server timeout in seconds.",
                cli_flag="--timeout",
                json_key="timeout",
                default=300,
            ),
            Parameter(
                name="no_open_browser",
                kind="flag",
                type="bool",
                description="Print the auth URL instead of opening a browser.",
                cli_flag="--no-open-browser",
                json_key="no_open_browser",
                default=False,
            ),
            Parameter(
                name="dry_run",
                kind="flag",
                type="bool",
                description="Validate inputs and show the intended setup without OAuth or rclone writes.",
                cli_flag="--dry-run",
                json_key="dry_run",
                default=False,
            ),
            Parameter(
                name="force",
                kind="flag",
                type="bool",
                description="Update an existing rclone remote instead of failing.",
                cli_flag="--force",
                json_key="force",
                default=False,
            ),
            Parameter(
                name="skills",
                kind="flag",
                type="bool",
                description="Show skills documentation for this command and exit.",
                cli_flag="--skills",
                default=False,
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
            Parameter(
                name="input_json",
                kind="json_field",
                type="object",
                description="Flat JSON object providing all parameters.",
                shape="object",
            ),
        ],
        examples=[
            Example(
                description="Validate the desktop OAuth client and planned rclone remote without making changes.",
                cli="bbman auth-gdrive --client-secrets client_secret.json --dry-run --output json",
                input_json={
                    "client_secrets": "client_secret.json",
                    "dry_run": True,
                    "output": "json",
                },
            ),
            Example(
                description="Authorize Google Drive and create the default bbackup rclone remote.",
                cli="bbman auth-gdrive --client-secrets client_secret.json",
            ),
            Example(
                description="Use the printed URL through an SSH local-port tunnel on port 53682.",
                cli="bbman auth-gdrive --client-secrets client_secret.json --no-open-browser --port 53682",
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="run",
        summary="Run bbackup commands through the bbman wrapper.",
        description="Launch the main bbackup CLI through bbman, preserving JSON envelope behavior when requested.",
        category="integration",
        parameters=[
            Parameter(
                name="command",
                kind="positional",
                type="string[]",
                description="The bbackup command and arguments to run.",
                cli_flag=None,
                json_key=None,
                shape="list",
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format: text or json.",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
        ],
        examples=[
            Example(
                description="Run a backup through bbman with JSON output.",
                cli="bbman run backup --containers myapp --no-interactive --output json",
            ),
        ],
    )
)


_register_bbman(
    CliCommand(
        cli="bbman",
        name="skills",
        summary="List available bbman skills for AI agent discovery.",
        description="List or inspect bbman skills in JSON or Markdown formats.",
        category="skills",
        parameters=[
            Parameter(
                name="skill_id",
                kind="positional",
                type="string",
                description="Optional skill id for detailed view.",
            ),
            Parameter(
                name="format",
                kind="flag",
                type="string",
                description="Output as JSON or Markdown skills catalog.",
                cli_flag="--format",
                json_key="format",
                allowed_values=["json", "markdown"],
                shape="enum",
                default="json",
            ),
            Parameter(
                name="output",
                kind="flag",
                type="string",
                description="Output format for detailed skill view (text or json).",
                cli_flag="--output",
                json_key="output",
                allowed_values=["text", "json"],
                shape="enum",
            ),
        ],
        examples=[
            Example(
                description="List all bbman skills in JSON.",
                cli="bbman skills",
            ),
            Example(
                description="Dump the full Markdown skills catalog.",
                cli="bbman skills --format markdown",
            ),
        ],
    )
)


def get_command_registry(cli: CliName) -> Dict[str, CliCommand]:
    """Return the command registry for the requested CLI."""
    return BBACKUP_COMMANDS if cli == "bbackup" else BBMAN_COMMANDS


def get_command(cli: CliName, name: str) -> Optional[CliCommand]:
    """Lookup a command by cli + name. Returns None if unknown."""
    registry = get_command_registry(cli)
    return registry.get(f"{cli}:{name}")

# Version-two operation definitions. Production CLI help and JSON schemas share
# this registry; unsupported capabilities are deliberately absent.
PRODUCTION_COMMANDS = {
    "doctor": {"summary": "Inspect configured repository bindings and engine availability.", "fields": {}},
    "jobs list": {"summary": "List configured capture jobs.", "fields": {}},
    "jobs run": {"summary": "Capture locally and copy explicit successful snapshots.", "fields": {"name": "string"}},
    "repositories list": {"summary": "List declared repositories without host credentials.", "fields": {}},
    "repositories init": {"summary": "Initialize an encrypted repository.", "fields": {"name": "string", "source": "optional-string"}},
    "repositories check": {"summary": "Read and verify repository data.", "fields": {"name": "string"}},
    "snapshots list": {"summary": "List successful local snapshot receipts.", "fields": {"limit": "limit", "offset": "offset"}},
    "snapshots copy": {"summary": "Copy a successful snapshot into an independent repository.", "fields": {"source": "string", "destination": "string", "snapshot": "string"}},
    "restore": {"summary": "Restore and verify files in a new isolated directory.", "fields": {"repository": "string", "snapshot": "string", "target": "path"}},
    "runs list": {"summary": "Read sanitized operation events.", "fields": {"limit": "limit", "offset": "offset"}},
    "service render": {"summary": "Render disabled systemd service and persistent timer units.", "fields": {"name": "string", "executable": "path"}},
}


def production_schema(command: str) -> dict:
    """JSON Schema for exactly the fields accepted by a production command."""
    definition = PRODUCTION_COMMANDS[command]
    properties = {}
    required = []
    for name, kind in definition["fields"].items():
        if kind == "boolean":
            properties[name] = {"type": "boolean", "default": False}
        elif kind in ("limit", "offset"):
            properties[name] = {"type": "integer", "minimum": 1 if kind == "limit" else 0}
            if kind == "limit":
                properties[name]["maximum"] = 1000
        else:
            properties[name] = {"type": "string", "minLength": 1}
            if kind != "optional-string":
                required.append(name)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "additionalProperties": False, "properties": properties, "required": required}

PRODUCTION_COMMANDS.update({
    "recovery inspect": {"summary": "Inspect cloud versioning and lifecycle without a protection claim.", "fields": {"bucket": "string", "endpoint": "string", "prefix": "optional-string", "region": "optional-string"}},
    "recovery checkpoint": {"summary": "Check a quiescent repository and seal an independent recovery kit.", "fields": {"repository": "string", "snapshot": "string", "bucket": "string", "endpoint": "string", "prefix": "optional-string", "region": "optional-string", "target": "path", "signer": "string", "recipient": "string", "quiescent_window_confirmed": "boolean"}},
    "recovery open": {"summary": "Authenticate and decrypt a recovery kit into a new private directory.", "fields": {"kit": "path", "target": "path", "signer": "string"}},
    "recovery restore": {"summary": "Restore signed exact historical object versions into a new local repository.", "fields": {"manifest": "path", "signature": "path", "signer": "string", "password_file": "path", "repository": "string", "repository_id": "string", "snapshot": "string", "bucket": "string", "endpoint": "string", "prefix": "optional-string", "region": "optional-string", "target": "path"}},
})

PRODUCTION_COMMANDS["runs stream"] = {
    "summary": "Emit a bounded page of lifecycle events as explicit JSON lines, without a result envelope.",
    "fields": {"limit": "limit", "offset": "offset"},
}

PRODUCTION_COMMANDS["skills"] = {
    "summary": "Discover the production operation registry and generated input schemas.",
    "fields": {"name": "optional-string"},
}

PRODUCTION_COMMANDS["runs reconcile"] = {
    "summary": "Record an administrator-reviewed outcome for an uncertain operation without replaying it.",
    "fields": {"repository": "string", "operation_id": "string", "outcome": "string", "repository_reviewed": "boolean"},
}

PRODUCTION_COMMANDS["databases restore"] = {
    "summary": "Restore a trusted PostgreSQL snapshot export into a new database and validate connectivity.",
    "fields": {"repository": "string", "snapshot": "string", "source": "string", "connection": "string", "target_database": "string", "trusted_archive": "boolean"},
}
PRODUCTION_COMMANDS["databases reconcile"] = {
    "summary": "Record an administrator-reviewed database restore outcome without replaying it.",
    "fields": {"connection": "string", "target_database": "string", "operation_id": "string", "outcome": "string", "database_reviewed": "boolean"},
}
