"""Read durable recovery facts without treating receipts as protection proof."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING, Literal, TypedDict

from .models import Job
from .operations import repository_key

if TYPE_CHECKING:
    from .service import BackupService


class ScheduleStatus(TypedDict):
    name: str
    configured: str
    next_run: str | None
    active_timer: str | None


class RepositoryStatus(TypedDict):
    name: str
    role: str
    free_bytes: int | None
    total_bytes: int | None
    capacity_source: Literal[
        "repository filesystem",
        "nearest existing parent filesystem",
        "remote capacity unavailable",
        "local capacity unavailable",
    ]


class CheckpointStatus(TypedDict):
    repository: str
    snapshot_id: str
    captured_at: str
    expires_at: str | None
    expiry_state: Literal["expired", "unexpired", "unknown"]
    manifest_sha256: str


class OperationalStatus(TypedDict):
    last_local_capture: str | None
    last_independent_copy: str | None
    last_cloud_copy: str | None
    last_repository_check: str | None
    last_verified_file_restore: str | None
    last_native_database_restore: str | None
    last_container_restore: str | None
    application_recovery: str
    operation_counts: dict[str, int]
    repositories: list[RepositoryStatus]
    schedules: list[ScheduleStatus]
    last_checkpoint: CheckpointStatus | None
    protection_status: str


def _schedules(jobs: tuple[Job, ...]) -> list[ScheduleStatus]:
    schedules: list[ScheduleStatus] = [
        {
            "name": job.name,
            "configured": job.schedule,
            "next_run": None,
            "active_timer": None,
        }
        for job in jobs
    ]
    units = {
        unit: row
        for row in schedules
        for unit in (f"bbackup-{row['name']}.timer", f"bbackup@{row['name']}.timer")
    }
    if not units:
        return schedules
    for manager in ("user", "system"):
        command = ["systemctl", *(["--user"] if manager == "user" else []),
                   "show", "--property=Id,LoadState,ActiveState,NextElapseUSecRealtime",
                   "--", *units]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=2,
                                    env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            continue
        # Missing units may make systemctl return nonzero alongside valid rows.
        for block in result.stdout.split("\n\n"):
            properties = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            unit = properties.get("Id")
            next_run = properties.get("NextElapseUSecRealtime", "")
            if (unit in units and properties.get("LoadState") == "loaded"
                    and properties.get("ActiveState") == "active"
                    and next_run not in ("", "0", "n/a")):
                row = units[unit]
                if row["active_timer"] is None:
                    row.update(active_timer=f"{manager}: {unit}", next_run=next_run)
                else:
                    row.update(active_timer="conflicting timers", next_run=None)
    return schedules


def _checkpoint_expiry_state(
    expires_at: str | None, now: datetime
) -> Literal["expired", "unexpired", "unknown"]:
    if expires_at is None:
        return "unknown"
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return "unknown"
    return "expired" if expiry <= now else "unexpired"


def _last_current_checkpoint(
    service: BackupService, identities: set[str], now: datetime
) -> CheckpointStatus | None:
    if not identities:
        return None
    placeholders = ",".join("?" for _ in identities)
    with service._receipts() as db:
        row = db.execute(
            f"""SELECT repository, snapshot, captured_at, expires_at, manifest_sha256
                FROM checkpoints WHERE repository_identity IN ({placeholders})
                ORDER BY captured_at DESC, rowid DESC LIMIT 1""",
            tuple(identities),
        ).fetchone()
    if row is None:
        return None
    repository, snapshot, captured_at, expires_at, manifest_sha256 = row
    if not all(
        isinstance(value, str)
        for value in (repository, snapshot, captured_at, manifest_sha256)
    ) or (expires_at is not None and not isinstance(expires_at, str)):
        return None
    return {
        "repository": repository,
        "snapshot_id": snapshot,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "expiry_state": _checkpoint_expiry_state(expires_at, now),
        "manifest_sha256": manifest_sha256,
    }


def _repository_capacity(
    repository: str,
) -> tuple[
    int | None,
    int | None,
    Literal[
        "repository filesystem",
        "nearest existing parent filesystem",
        "remote capacity unavailable",
        "local capacity unavailable",
    ],
]:
    if not repository.startswith("/"):
        return None, None, "remote capacity unavailable"
    ancestor = Path(repository)
    missing = False
    while True:
        try:
            ancestor.stat()
            break
        except FileNotFoundError:
            parent = ancestor.parent
            if parent == ancestor:
                return None, None, "local capacity unavailable"
            ancestor = parent
            missing = True
        except OSError:
            return None, None, "local capacity unavailable"
    try:
        usage = shutil.disk_usage(ancestor)
    except OSError:
        return None, None, "local capacity unavailable"
    source: Literal["repository filesystem", "nearest existing parent filesystem"]
    source = "nearest existing parent filesystem" if missing else "repository filesystem"
    return usage.free, usage.total, source


def operational_status(service: BackupService) -> OperationalStatus:
    """Return typed, path-free observations shared by doctor and Overview."""
    summaries = service.ledger.summary()
    identities: set[str] = {
        repository_key(str(service._identity(name)))
        for name in service.bindings.repositories
    }
    cloud_identities: set[str] = {
        repository_key(str(service._identity(name)))
        for name, binding in service.bindings.repositories.items()
        if not binding.repository.startswith("/")
    }

    def latest(label: str, *, allowed: set[str] | None = None) -> str | None:
        finished_at: list[str] = []
        for row in summaries:
            finished = row["last_finished_at"]
            repository_id = row["repository_id"]
            if (
                row["label"] == label
                and row["status"] == "succeeded"
                and isinstance(finished, str)
                and isinstance(repository_id, str)
                and (allowed is None or repository_id in allowed)
            ):
                finished_at.append(finished)
        return max(finished_at, default=None)

    counts: dict[str, int] = {}
    for row in summaries:
        status = row["status"]
        count = row["count"]
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + (
                count if type(count) is int else 0
            )

    repositories: list[RepositoryStatus] = []
    for repo in service.config.repositories:
        binding = service.bindings.repositories[repo.name]
        free_bytes, total_bytes, capacity_source = _repository_capacity(
            binding.repository
        )
        repositories.append(
            {
                "name": repo.name,
                "role": repo.role,
                "free_bytes": free_bytes,
                "total_bytes": total_bytes,
                "capacity_source": capacity_source,
            }
        )

    now = datetime.now(timezone.utc)
    return {
        "last_local_capture": latest("capture", allowed=identities),
        "last_independent_copy": latest("copy", allowed=identities),
        "last_cloud_copy": latest("copy", allowed=cloud_identities),
        "last_repository_check": latest("check", allowed=identities),
        "last_verified_file_restore": latest("restore", allowed=identities),
        "last_native_database_restore": latest(
            "database-restore", allowed=identities
        ),
        "last_container_restore": latest("docker-restore"),
        "application_recovery": (
            "not recorded; native and file checks are not application validation"
        ),
        "operation_counts": counts,
        "repositories": repositories,
        "schedules": _schedules(service.config.jobs),
        "last_checkpoint": _last_current_checkpoint(service, identities, now),
        "protection_status": (
            "not qualified; checkpoint expiry alone does not prove off-host custody "
            "or current provider protection"
        ),
    }
