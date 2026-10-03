"""Native restic snapshot support for bbackup."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
from fnmatch import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import Config, SnapshotProfile


STATE_FILE_NAME = "snapshot-state.json"
STATE_SCHEMA_VERSION = 1


class SnapshotError(RuntimeError):
    """Raised when a snapshot profile cannot be executed safely."""


@dataclass
class DiscoveredRepo:
    path: str
    fingerprint: str
    repo_id: str
    origin: str = ""
    head: str = ""
    legacy_fingerprint: str = ""
    legacy_repo_id: str = ""


def expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state(profile: SnapshotProfile) -> Dict[str, Any]:
    state_path = state_file(profile)
    if not state_path.exists():
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "profile": profile.name,
            "repos": {},
            "alerts": [],
        }
    with state_path.open("r", encoding="utf-8") as f:
        state = json.load(f) or {}
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("profile", profile.name)
    state.setdefault("repos", {})
    state.setdefault("alerts", [])
    return state


def save_state(profile: SnapshotProfile, state: Dict[str, Any]) -> None:
    state_path = state_file(profile)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(state_path)


def state_file(profile: SnapshotProfile) -> Path:
    state_dir = profile.state_dir or f"~/.local/state/bbackup/{profile.host_id or socket.gethostname()}/{profile.name}"
    return expand_path(state_dir) / STATE_FILE_NAME


def _git(path: Path, args: List[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _repo_from_path(path: Path) -> Optional[DiscoveredRepo]:
    toplevel = _git(path, ["rev-parse", "--show-toplevel"])
    if not toplevel:
        return None
    root = Path(toplevel).resolve()
    if root != path.resolve():
        return None
    origin = _git(root, ["config", "--get", "remote.origin.url"]) or ""
    head = _git(root, ["rev-parse", "HEAD"]) or ""
    git_dir = _git(root, ["rev-parse", "--git-dir"]) or ""
    root_commit = _git(root, ["rev-list", "--max-parents=0", "HEAD"]) or ""
    fingerprint_source = "\n".join([str(root), origin, git_dir, root_commit])
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    repo_id = hashlib.sha256(f"{origin or root}:{fingerprint}".encode("utf-8")).hexdigest()[:16]
    legacy_fingerprint_source = "\n".join([str(root), origin, head, git_dir])
    legacy_fingerprint = hashlib.sha256(legacy_fingerprint_source.encode("utf-8")).hexdigest()
    legacy_repo_id = hashlib.sha256(f"{origin or root}:{legacy_fingerprint}".encode("utf-8")).hexdigest()[:16]
    return DiscoveredRepo(
        path=str(root),
        fingerprint=fingerprint,
        repo_id=repo_id,
        origin=origin,
        head=head,
        legacy_fingerprint=legacy_fingerprint,
        legacy_repo_id=legacy_repo_id,
    )


def discover_git_repos(profile: SnapshotProfile) -> List[DiscoveredRepo]:
    """Discover immediate child Git roots under repo homes plus explicit roots."""
    repos: Dict[str, DiscoveredRepo] = {}
    for home in profile.repo_homes:
        home_path = expand_path(home)
        if not home_path.is_dir():
            continue
        for child in sorted(home_path.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if _is_excluded_path(child, profile.exclude_paths):
                continue
            repo = _repo_from_path(child)
            if repo:
                repos[repo.path] = repo
    for explicit in profile.explicit_repos:
        path = expand_path(explicit)
        if _is_excluded_path(path, profile.exclude_paths):
            continue
        repo = _repo_from_path(path)
        if repo:
            repos[repo.path] = repo
    return list(repos.values())


def _is_excluded_path(path: Path, exclude_paths: Iterable[str]) -> bool:
    resolved = str(path.resolve())
    for raw_pattern in exclude_paths:
        pattern = str(expand_path(raw_pattern)) if raw_pattern.startswith(("~", "/", "$")) else raw_pattern
        if resolved == pattern or resolved.startswith(pattern.rstrip("/") + "/"):
            return True
        if fnmatch(resolved, pattern) or fnmatch(path.name, pattern):
            return True
    return False


def _has_snapshot_history(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("successful_snapshot_ids")) or entry.get("ever_successful_snapshot") is True


def reconcile_repos(profile: SnapshotProfile, discovered: List[DiscoveredRepo], save: bool = True) -> Dict[str, Any]:
    state = load_state(profile)
    repos = state.setdefault("repos", {})
    alerts: List[Dict[str, str]] = []
    now = utc_now()
    discovered_by_path = {repo.path: repo for repo in discovered}

    for repo in discovered:
        reused = [
            data for data in repos.values()
            if data.get("path") == repo.path
            and data.get("fingerprint") != repo.fingerprint
            and data.get("status") == "retired"
        ]
        if reused:
            alerts.append({
                "severity": "critical",
                "code": "path_reuse_conflict",
                "path": repo.path,
                "message": f"Retired repo path reused with a different Git fingerprint: {repo.path}",
            })
            continue

        unexpected_active_reuse = [
            data for existing_id, data in repos.items()
            if data.get("path") == repo.path
            and data.get("status") == "active"
            and existing_id not in {repo.repo_id, repo.legacy_repo_id}
            and data.get("fingerprint") not in {repo.fingerprint, repo.legacy_fingerprint}
        ]
        if unexpected_active_reuse:
            alerts.append({
                "severity": "critical",
                "code": "active_path_identity_conflict",
                "path": repo.path,
                "message": f"Active repo path has conflicting identity history: {repo.path}",
            })
            continue

        matching_ids_for_path = [
            existing_id
            for existing_id, data in repos.items()
            if data.get("path") == repo.path
            and data.get("status") in {"active", "retired"}
            and data.get("fingerprint") in {repo.fingerprint, repo.legacy_fingerprint}
        ]
        preserve_retired = any(
            repos[existing_id].get("status") == "retired"
            for existing_id in matching_ids_for_path
        )
        if matching_ids_for_path:
            merged_entry: Dict[str, Any] = {}
            merged_success_ids: List[str] = []
            for existing_id in matching_ids_for_path:
                existing_entry = repos.pop(existing_id)
                merged_entry.update(existing_entry)
                for snapshot_id in existing_entry.get("successful_snapshot_ids", []) or []:
                    if snapshot_id not in merged_success_ids:
                        merged_success_ids.append(snapshot_id)
            if repo.repo_id in repos:
                current_entry = repos.pop(repo.repo_id)
                preserve_retired = preserve_retired or current_entry.get("status") == "retired"
                merged_entry.update(current_entry)
                for snapshot_id in current_entry.get("successful_snapshot_ids", []) or []:
                    if snapshot_id not in merged_success_ids:
                        merged_success_ids.append(snapshot_id)
            if merged_success_ids:
                merged_entry["successful_snapshot_ids"] = merged_success_ids
            repos[repo.repo_id] = merged_entry
        repo_id = repo.repo_id
        entry = repos.setdefault(repo_id, {})
        status = "retired" if preserve_retired or entry.get("status") == "retired" else "active"
        entry.update({
            "repo_id": repo_id,
            "path": repo.path,
            "fingerprint": repo.fingerprint,
            "origin": repo.origin,
            "head": repo.head,
            "status": status,
            "last_seen_at": now,
        })
        entry.setdefault("first_seen_at", now)
        entry.setdefault("successful_snapshot_ids", [])
    for repo_id, entry in repos.items():
        if entry.get("status") != "active":
            continue
        if entry.get("path") in discovered_by_path:
            continue
        if _has_snapshot_history(entry):
            entry["status"] = "retired"
            entry["retired_at"] = now
        else:
            alerts.append({
                "severity": "critical",
                "code": "missing_without_snapshot",
                "path": entry.get("path", ""),
                "repo_id": repo_id,
                "message": f"Repo disappeared before any successful snapshot: {entry.get('path', repo_id)}",
            })

    state["alerts"] = alerts
    if save:
        save_state(profile, state)
    return {
        "profile": profile.name,
        "state_file": str(state_file(profile)),
        "discovered_repos": [repo.__dict__ for repo in discovered],
        "active_repo_ids": sorted(
            repo_id for repo_id, entry in repos.items() if entry.get("status") == "active"
        ),
        "retired_repo_ids": sorted(
            repo_id for repo_id, entry in repos.items() if entry.get("status") == "retired"
        ),
        "alerts": alerts,
    }


class ResticRunner:
    def __init__(self, profile: SnapshotProfile):
        if profile.engine != "restic":
            raise SnapshotError(f"Unsupported snapshot engine: {profile.engine}")
        self.profile = profile

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.profile.cache_dir:
            env["RESTIC_CACHE_DIR"] = str(expand_path(self.profile.cache_dir))
        return env

    def base_args(self) -> List[str]:
        args = ["restic", "-r", self.profile.repository]
        if self.profile.password_file:
            args.extend(["--password-file", str(expand_path(self.profile.password_file))])
        if self.profile.cache_dir:
            args.extend(["--cache-dir", str(expand_path(self.profile.cache_dir))])
        args.extend(["--json", "--retry-lock", self.profile.retry_lock])
        return args

    def init_args(self) -> List[str]:
        return [*self.base_args(), "init"]

    def snapshots_args(self, tags: Optional[Iterable[str]] = None) -> List[str]:
        args = [*self.base_args(), "snapshots"]
        for tag in tags or []:
            args.extend(["--tag", tag])
        return args

    def check_args(self, read_data_subset: Optional[str] = None) -> List[str]:
        args = [*self.base_args(), "check"]
        if read_data_subset:
            args.extend(["--read-data-subset", read_data_subset])
        return args

    def backup_args(self, path: str, tags: Iterable[str]) -> List[str]:
        args = [*self.base_args(), "backup", str(expand_path(path))]
        if self.profile.host_id:
            args.extend(["--host", self.profile.host_id])
        for tag in tags:
            args.extend(["--tag", tag])
        for exclude in self.profile.exclude_paths:
            args.extend(["--exclude", _restic_exclude_arg(exclude)])
        return args

    def restore_args(self, snapshot_id: str, target: str, include: Optional[str] = None) -> List[str]:
        args = [*self.base_args(), "restore", snapshot_id, "--target", str(expand_path(target))]
        if include:
            args.extend(["--include", include])
        return args

    def forget_args(self, snapshot_ids: Iterable[str], dry_run: bool = True) -> List[str]:
        ids = list(snapshot_ids)
        if not ids:
            raise SnapshotError("Refusing to build a forget command without snapshot IDs")
        args = [*self.base_args(), "forget"]
        if dry_run:
            args.append("--dry-run")
        return [*args, "--", *ids]

    def retention_args(
        self,
        tags: Iterable[str],
        policy: Dict[str, int],
        host: str,
        dry_run: bool = True,
    ) -> List[str]:
        tag_values = list(tags)
        if not tag_values:
            raise SnapshotError("Refusing to build a retention command without tags")
        if not host:
            raise SnapshotError("Refusing to build a retention command without a host")
        if not policy:
            raise SnapshotError("Refusing to build a retention command without a policy")
        selected_periods = [period for period in ("daily", "weekly", "monthly") if period in policy]
        if not selected_periods:
            raise SnapshotError("Refusing to build a retention command without a supported policy")
        args = [*self.base_args(), "forget"]
        if dry_run:
            args.append("--dry-run")
        args.extend(
            [
                "--host",
                host,
                "--tag",
                ",".join(tag_values),
                "--group-by",
                "host",
            ]
        )
        for period in selected_periods:
            args.extend([f"--keep-{period}", str(policy[period])])
        return args

    def run(self, args: List[str]) -> Dict[str, Any]:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
        )
        return {
            "args": args,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "ok": result.returncode == 0,
        }


def common_tags(profile: SnapshotProfile, scope: str, extra: Iterable[str] = ()) -> List[str]:
    return [
        "bbackup",
        f"profile={profile.name}",
        f"scope={scope}",
        *profile.tags,
        *list(extra),
    ]


def _restic_exclude_arg(pattern: str) -> str:
    if pattern.startswith(("~", "/", "$")):
        return str(expand_path(pattern))
    return pattern


def _retention_policy(profile: SnapshotProfile, scope: str) -> Optional[Dict[str, int]]:
    """Resolve a scoped restic retention policy from profile configuration."""
    retention = profile.retention or {}
    if not isinstance(retention, dict):
        raise SnapshotError("Snapshot retention must be a mapping")

    nested = retention.get(scope)
    if scope == "repo" and nested is None:
        nested = retention.get("active_repo")
    if nested is not None and not isinstance(nested, dict):
        raise SnapshotError(f"Snapshot retention.{scope} must be a mapping")

    prefixes = {
        "repo": ("active_repo", "repo"),
        "path": ("path", "include_path"),
    }.get(scope)
    if prefixes is None:
        raise SnapshotError(f"Unsupported snapshot retention scope: {scope}")

    policy: Dict[str, int] = {}
    for period in ("daily", "weekly", "monthly"):
        raw_value = nested.get(period) if isinstance(nested, dict) else None
        if raw_value is None:
            for prefix in prefixes:
                raw_value = retention.get(f"{prefix}_{period}")
                if raw_value is not None:
                    break
        if raw_value is None:
            continue
        if isinstance(raw_value, bool) or (
            isinstance(raw_value, float) and not raw_value.is_integer()
        ):
            raise SnapshotError(f"Snapshot retention {scope}_{period} must be a non-negative integer")
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise SnapshotError(
                f"Snapshot retention {scope}_{period} must be a non-negative integer"
            ) from exc
        if value < 0:
            raise SnapshotError(f"Snapshot retention {scope}_{period} must be a non-negative integer")
        policy[period] = value
    return policy or None


def _retention_tags(profile: SnapshotProfile, scope: str, identifier: str) -> List[str]:
    return [
        "bbackup",
        f"profile={profile.name}",
        f"scope={scope}",
        f"{scope}_id={identifier}",
    ]


def _retention_command(
    profile: SnapshotProfile,
    runner: ResticRunner,
    scope: str,
    identifier: str,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    policy = _retention_policy(profile, scope)
    if not policy:
        return None
    tags = _retention_tags(profile, scope, identifier)
    return {
        "scope": scope,
        "identifier": identifier,
        "host": profile.host_id,
        "policy": policy,
        "tags": tags,
        "args": runner.retention_args(tags, policy, profile.host_id, dry_run=dry_run),
    }


def _snapshot_plan(profile: SnapshotProfile, save: bool, dry_run: bool) -> Dict[str, Any]:
    discovered = discover_git_repos(profile)
    reconcile = reconcile_repos(profile, discovered, save=save)
    runner = ResticRunner(profile)
    active_repo_ids = set(reconcile["active_repo_ids"])
    repo_commands: List[Dict[str, Any]] = []
    include_commands: List[Dict[str, Any]] = []
    retention_commands: List[Dict[str, Any]] = []

    for repo in discovered:
        if repo.repo_id not in active_repo_ids:
            continue
        tags = common_tags(profile, "repo", [f"repo_id={repo.repo_id}"])
        command: Dict[str, Any] = {
            "repo_id": repo.repo_id,
            "path": repo.path,
            "args": runner.backup_args(repo.path, tags),
        }
        retention = _retention_command(profile, runner, "repo", repo.repo_id, dry_run=dry_run)
        if retention:
            command["retention_args"] = retention["args"]
            retention_commands.append(retention)
        repo_commands.append(command)

    for path in profile.include_paths:
        resolved_path = str(expand_path(path))
        identifier = path_id(path)
        tags = common_tags(profile, "path", [f"path_id={identifier}"])
        command = {
            "path": resolved_path,
            "path_id": identifier,
            "args": runner.backup_args(path, tags),
        }
        retention = _retention_command(profile, runner, "path", identifier, dry_run=dry_run)
        if retention:
            command["retention_args"] = retention["args"]
            retention_commands.append(retention)
        include_commands.append(command)

    return {
        "profile": profile.name,
        "repository": profile.repository,
        "host_id": profile.host_id,
        "state_file": reconcile["state_file"],
        "repos": repo_commands,
        "paths": include_commands,
        "retention": retention_commands,
        "alerts": reconcile["alerts"],
    }


def snapshot_plan(profile: SnapshotProfile) -> Dict[str, Any]:
    return _snapshot_plan(profile, save=False, dry_run=True)


def path_id(path: str) -> str:
    return hashlib.sha256(str(expand_path(path)).encode("utf-8")).hexdigest()[:16]


def mark_snapshot_success(profile: SnapshotProfile, repo_id: str, snapshot_id: str) -> None:
    state = load_state(profile)
    entry = state.setdefault("repos", {}).setdefault(repo_id, {})
    snapshots = entry.setdefault("successful_snapshot_ids", [])
    if snapshot_id and snapshot_id not in snapshots:
        snapshots.append(snapshot_id)
    entry["ever_successful_snapshot"] = True
    entry["last_successful_snapshot_id"] = snapshot_id
    entry["last_successful_snapshot_at"] = utc_now()
    save_state(profile, state)


def snapshot_init(profile: SnapshotProfile, dry_run: bool = False) -> Dict[str, Any]:
    runner = ResticRunner(profile)
    args = runner.init_args()
    if dry_run:
        return {"dry_run": True, "args": args}
    if profile.cache_dir:
        expand_path(profile.cache_dir).mkdir(parents=True, exist_ok=True)
    expand_path(profile.state_dir).mkdir(parents=True, exist_ok=True)
    _require_snapshot_operation_preflight(profile, require_initialized=False)
    return runner.run(args)


def snapshot_run(profile: SnapshotProfile, dry_run: bool = False) -> Dict[str, Any]:
    if not dry_run:
        _require_snapshot_operation_preflight(profile)
    plan = _snapshot_plan(profile, save=not dry_run, dry_run=dry_run)
    has_targets = bool(plan["repos"] or plan["paths"])
    if dry_run:
        plan["success"] = not bool(plan["alerts"]) and has_targets
        plan["dry_run"] = True
        return plan
    if plan["alerts"]:
        raise SnapshotError("Refusing to run snapshot profile with critical alerts")
    if not has_targets:
        raise SnapshotError("Refusing to run snapshot profile with no repositories or paths")
    runner = ResticRunner(profile)
    results: List[Dict[str, Any]] = []
    retention_results: List[Dict[str, Any]] = []
    for item in [*plan["repos"], *plan["paths"]]:
        result = runner.run(item["args"])
        result["target"] = item.get("path")
        result["repo_id"] = item.get("repo_id")
        results.append(result)
        if item.get("repo_id") and result["ok"]:
            snapshot_id = _extract_snapshot_id(result["stdout"])
            if snapshot_id:
                mark_snapshot_success(profile, item["repo_id"], snapshot_id)
        if result["ok"] and item.get("retention_args"):
            retention_result = runner.run(item["retention_args"])
            retention_result["target"] = item.get("path")
            retention_result["repo_id"] = item.get("repo_id")
            retention_results.append(retention_result)
            if retention_result["ok"] and item.get("repo_id"):
                _reconcile_retention_state(profile, item["repo_id"], retention_result["stdout"])
    all_results = [*results, *retention_results]
    return {
        "profile": profile.name,
        "results": results,
        "retention_results": retention_results,
        "success": all(result["ok"] for result in all_results),
    }


def _extract_snapshot_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            return obj.get("snapshot_id", "")
    return ""


def _retention_removed_snapshot_ids(stdout: str) -> List[str]:
    """Extract snapshot IDs removed by a JSON restic forget response."""
    payloads: List[Any] = []
    try:
        payloads.append(json.loads(stdout))
    except json.JSONDecodeError:
        payloads.extend(
            json.loads(line)
            for line in stdout.splitlines()
            if line.strip()
            and _is_json_value(line)
        )
    removed: List[str] = []
    for payload in payloads:
        groups = payload if isinstance(payload, list) else [payload]
        for group in groups:
            if not isinstance(group, dict):
                continue
            for snapshot in group.get("remove", []) or []:
                snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
                if snapshot_id and snapshot_id not in removed:
                    removed.append(snapshot_id)
    return removed


def _is_json_value(value: str) -> bool:
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return False
    return True


def _reconcile_retention_state(profile: SnapshotProfile, repo_id: str, stdout: str) -> None:
    removed_ids = set(_retention_removed_snapshot_ids(stdout))
    if not removed_ids:
        return
    state = load_state(profile)
    entry = state.get("repos", {}).get(repo_id)
    if not entry:
        return
    snapshots = [
        snapshot_id
        for snapshot_id in entry.get("successful_snapshot_ids", []) or []
        if snapshot_id not in removed_ids
    ]
    entry["successful_snapshot_ids"] = snapshots
    if entry.get("last_successful_snapshot_id") in removed_ids:
        entry["last_successful_snapshot_id"] = snapshots[-1] if snapshots else ""
        entry.pop("last_successful_snapshot_at", None)
    save_state(profile, state)


def snapshot_check(profile: SnapshotProfile, read_data_subset: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    runner = ResticRunner(profile)
    args = runner.check_args(read_data_subset)
    if dry_run:
        return {"dry_run": True, "args": args}
    _require_snapshot_operation_preflight(profile)
    return runner.run(args)


def snapshot_restore(
    profile: SnapshotProfile,
    snapshot_id: str,
    target: str,
    include: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    runner = ResticRunner(profile)
    args = runner.restore_args(snapshot_id, target, include)
    if dry_run:
        return {"dry_run": True, "args": args}
    _require_snapshot_operation_preflight(profile)
    return runner.run(args)


def retire_repo(profile: SnapshotProfile, repo_id: str) -> Dict[str, Any]:
    state = load_state(profile)
    entry = state.get("repos", {}).get(repo_id)
    if not entry:
        raise SnapshotError(f"Unknown repo_id: {repo_id}")
    if not _has_snapshot_history(entry):
        raise SnapshotError(f"Cannot retire repo without a successful snapshot: {repo_id}")
    entry["status"] = "retired"
    entry["retired_at"] = utc_now()
    save_state(profile, state)
    return {"repo_id": repo_id, "status": "retired", "state_file": str(state_file(profile))}


def purge_plan(profile: SnapshotProfile, repo_id: str) -> Dict[str, Any]:
    state = load_state(profile)
    entry = state.get("repos", {}).get(repo_id)
    if not entry:
        raise SnapshotError(f"Unknown repo_id: {repo_id}")
    if entry.get("status") == "active":
        raise SnapshotError(f"Refusing to purge active repo_id: {repo_id}")
    snapshot_ids = list(dict.fromkeys(entry.get("successful_snapshot_ids", [])))
    if not snapshot_ids:
        raise SnapshotError(f"Cannot purge repo without recorded snapshot IDs: {repo_id}")
    runner = ResticRunner(profile)
    return {
        "repo_id": repo_id,
        "status": entry.get("status"),
        "path": entry.get("path"),
        "snapshot_ids": snapshot_ids,
        "selection": "exact_snapshot_ids",
        "dry_run_required": True,
        "forget_args": runner.forget_args(snapshot_ids, dry_run=True),
        "destructive_args_after_confirmation": runner.forget_args(snapshot_ids, dry_run=False),
    }


def schedule_units(profile: SnapshotProfile, config_path: Optional[str] = None) -> Dict[str, str]:
    service_name = f"bbackup-{profile.host_id.lower()}-{profile.name}"
    schedule = profile.schedule or {}
    daily_time = schedule.get("daily_time", "03:30")
    maintenance_time = schedule.get("maintenance_time", "Sun 04:30")
    verification_time = schedule.get("verification_time", "monthly")
    verification_read_data_subset = _systemd_exec_arg(str(schedule.get("verification_read_data_subset", "5%")))
    config_args = f" --config {shlex.quote(config_path)}" if config_path else ""
    lock_path = f"%t/{service_name}.lock"
    exec_prefix = f"/usr/bin/flock --exclusive {shlex.quote(lock_path)} /usr/bin/env bbackup{config_args}"
    unit = f"""[Unit]
Description=bbackup {profile.host_id} {profile.name} snapshot

[Service]
Type=oneshot
ExecStart={exec_prefix} snapshot run --profile {shlex.quote(profile.name)}
"""
    maintenance_service = f"""[Unit]
Description=bbackup {profile.host_id} {profile.name} weekly maintenance

[Service]
Type=oneshot
ExecStart={exec_prefix} snapshot check --profile {shlex.quote(profile.name)}
"""
    verification_service = f"""[Unit]
Description=bbackup {profile.host_id} {profile.name} monthly verification

[Service]
Type=oneshot
ExecStart={exec_prefix} snapshot check --profile {shlex.quote(profile.name)} --read-data-subset {shlex.quote(verification_read_data_subset)}
"""
    timer = f"""[Unit]
Description=Run bbackup {profile.host_id} {profile.name} snapshot daily

[Timer]
OnCalendar=*-*-* {daily_time}:00
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
"""
    maintenance = f"""[Unit]
Description=Run bbackup {profile.host_id} {profile.name} weekly maintenance

[Timer]
OnCalendar={maintenance_time}
Persistent=true
RandomizedDelaySec=1h
Unit={service_name}-maintenance.service

[Install]
WantedBy=timers.target
"""
    verification = f"""[Unit]
Description=Run bbackup {profile.host_id} {profile.name} monthly verification

[Timer]
OnCalendar={verification_time}
Persistent=true
RandomizedDelaySec=2h
Unit={service_name}-verification.service

[Install]
WantedBy=timers.target
"""
    return {
        f"{service_name}.service": unit,
        f"{service_name}.timer": timer,
        f"{service_name}-maintenance.service": maintenance_service,
        f"{service_name}-maintenance.timer": maintenance,
        f"{service_name}-verification.service": verification_service,
        f"{service_name}-verification.timer": verification,
    }


def _systemd_exec_arg(value: str) -> str:
    return value.replace("%", "%%")


def check_snapshot_profile(profile: SnapshotProfile) -> Dict[str, Any]:
    checks: Dict[str, Dict[str, Any]] = {}
    checks["engine"] = {"ok": profile.engine == "restic", "message": profile.engine}
    checks["restic"] = _tool_check("restic")
    checks["rclone"] = _tool_check("rclone") if profile.repository.startswith("rclone:") else {
        "ok": True,
        "message": "not required for non-rclone repository",
    }
    checks["rclone_drive_client_id"] = _rclone_drive_client_id_check(profile)
    checks["repository"] = {"ok": bool(profile.repository), "message": profile.repository or "missing"}
    checks["hostname"] = _hostname_check(profile)
    checks["password_file"] = _password_file_check(profile)
    checks["cache_dir"] = _dir_check(profile.cache_dir, create=False)
    checks["state_dir"] = _dir_check(profile.state_dir, create=False)
    if checks["engine"]["ok"]:
        try:
            checks["repository_initialized"] = _repository_initialized_check(profile)
        except SnapshotError as exc:
            checks["repository_initialized"] = {"ok": False, "message": str(exc)}
    else:
        checks["repository_initialized"] = {
            "ok": False,
            "message": "unsupported snapshot engine",
        }
    ok = all(check["ok"] for check in checks.values())
    return {"ok": ok, "profile": profile.name, "checks": checks}


def _require_rclone_drive_client(profile: SnapshotProfile) -> None:
    check = _rclone_drive_client_id_check(profile)
    if not check["ok"]:
        raise SnapshotError(check["message"])


def _require_snapshot_operation_preflight(profile: SnapshotProfile, require_initialized: bool = True) -> None:
    checks: Dict[str, Dict[str, Any]] = {
        "engine": {"ok": profile.engine == "restic", "message": profile.engine},
        "restic": _tool_check("restic"),
        "rclone": _tool_check("rclone") if profile.repository.startswith("rclone:") else {
            "ok": True,
            "message": "not required for non-rclone repository",
        },
        "rclone_drive_client_id": _rclone_drive_client_id_check(profile),
        "repository": {"ok": bool(profile.repository), "message": profile.repository or "missing"},
        "hostname": _hostname_check(profile),
        "password_file": _password_file_check(profile),
        "cache_dir": _dir_check(profile.cache_dir, create=False),
        "state_dir": _dir_check(profile.state_dir, create=False),
    }
    if require_initialized:
        checks["repository_initialized"] = _repository_initialized_check(profile)
    failures = [
        f"{name}: {check['message']}"
        for name, check in checks.items()
        if not check["ok"]
    ]
    if failures:
        raise SnapshotError("Snapshot profile preflight failed: " + "; ".join(failures))


def check_all_snapshot_profiles(config: Config) -> Dict[str, Any]:
    profiles = {
        name: check_snapshot_profile(profile)
        for name, profile in config.snapshot_profiles.items()
    }
    return {
        "ok": all(item["ok"] for item in profiles.values()) if profiles else True,
        "profiles": profiles,
    }


def _tool_check(name: str) -> Dict[str, Any]:
    found = shutil.which(name)
    if not found:
        return {"ok": False, "message": f"{name} not found in PATH"}
    try:
        result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=5)
        version = result.stdout.splitlines()[0] if result.stdout else found
    except (OSError, subprocess.TimeoutExpired):
        version = found
    return {"ok": True, "message": version}


def _rclone_remote_name(repository: str) -> Optional[str]:
    if not repository.startswith("rclone:"):
        return None
    parts = repository.split(":", 2)
    if len(parts) < 3 or not parts[1]:
        return None
    return parts[1]


def _rclone_drive_client_id_check(profile: SnapshotProfile) -> Dict[str, Any]:
    remote_name = _rclone_remote_name(profile.repository)
    if not remote_name:
        return {"ok": True, "message": "not required for non-rclone repository"}
    if not shutil.which("rclone"):
        return {"ok": False, "message": "rclone not found in PATH"}
    try:
        result = subprocess.run(
            ["rclone", "config", "show", remote_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"could not inspect rclone remote {remote_name}: {exc}"}
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return {"ok": False, "message": f"could not inspect rclone remote {remote_name}: {detail}"}

    fields: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    if fields.get("type") != "drive":
        return {"ok": True, "message": f"rclone remote {remote_name} type {fields.get('type', 'unknown')}"}
    if fields.get("client_id"):
        return {"ok": True, "message": f"Google Drive remote {remote_name} has client_id configured"}
    if profile.allow_default_rclone_drive_client:
        return {
            "ok": True,
            "message": (
                f"Google Drive remote {remote_name} uses the default rclone Drive client by "
                "explicit profile configuration"
            ),
        }
    return {
        "ok": False,
        "message": (
            f"Google Drive remote {remote_name} has no client_id; configure a dedicated "
            "OAuth client before treating this snapshot profile as reliable"
        ),
    }


def _hostname_check(profile: SnapshotProfile) -> Dict[str, Any]:
    if not profile.host_id:
        return {"ok": False, "message": "host_id missing"}
    actual = socket.gethostname()
    if profile.host_id.lower() == actual.lower():
        return {"ok": True, "message": actual}
    return {"ok": False, "message": f"profile host_id {profile.host_id} does not match hostname {actual}"}


def _password_file_check(profile: SnapshotProfile) -> Dict[str, Any]:
    if not profile.password_file:
        return {"ok": False, "message": "password_file missing"}
    path = expand_path(profile.password_file)
    if not path.exists():
        return {"ok": False, "message": f"password file not found: {path}"}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return {"ok": False, "message": f"password file must not be group/world accessible: {oct(mode)}"}
    return {"ok": True, "message": str(path)}


def _dir_check(path: str, create: bool = False) -> Dict[str, Any]:
    if not path:
        return {"ok": False, "message": "missing"}
    resolved = expand_path(path)
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.exists():
        return {"ok": False, "message": f"not found: {resolved}"}
    if not os.access(resolved, os.W_OK):
        return {"ok": False, "message": f"not writable: {resolved}"}
    return {"ok": True, "message": str(resolved)}


def _repository_initialized_check(profile: SnapshotProfile) -> Dict[str, Any]:
    if not shutil.which("restic") or not profile.repository:
        return {"ok": False, "message": "restic or repository missing"}
    runner = ResticRunner(profile)
    result = runner.run(runner.snapshots_args(["bbackup"]))
    if result["ok"]:
        return {"ok": True, "message": "repository accessible"}
    return {"ok": False, "message": result["stderr"] or result["stdout"] or "repository inaccessible"}
