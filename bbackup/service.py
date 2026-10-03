"""Shared production operations. CLI and dashboard must use this boundary."""
from __future__ import annotations

from contextlib import ExitStack
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from .capture import capture_sources
from .config import SnapshotProfile
from .models import Configuration, HostBindings
from .snapshot import ResticRunner
from .operations import (
    OperationLedger,
    OperationPlan,
    OperationRun,
    OperationRunner,
    OperationStatus,
    repository_key,
)


class ServiceError(RuntimeError):
    """A sanitized, stable operational failure."""


def _snapshot_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ServiceError("A complete snapshot ID is required")
    return value


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


class BackupService:
    def __init__(self, config: Configuration, bindings: HostBindings):
        self.config, self.bindings = config, bindings
        if set(bindings.repositories) != {r.name for r in config.repositories}:
            raise ServiceError("Host bindings must match declared repositories")
        for source in config.sources:
            if source.kind in ("postgresql", "mysql", "mariadb"):
                binding = bindings.database_bindings.get(source.connection)
                if binding is None or binding.kind != source.kind:
                    raise ServiceError("Database source requires a matching private connection binding")
        self.state = bindings.state_dir.resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.stat().st_mode & 0o077:
            raise ServiceError("State directory must be private")
        self.ledger = OperationLedger(self.state / "operations.sqlite3")
        self.runner = OperationRunner()
        self.receipts_path = self.state / "snapshots.sqlite3"
        with self._receipts() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS snapshots (
                    repository TEXT, snapshot TEXT, job TEXT, operation TEXT,
                    capture_token TEXT, PRIMARY KEY(repository, snapshot)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS copies (
                    source_repository TEXT, source_snapshot TEXT,
                    destination_repository TEXT, destination_snapshot TEXT,
                    operation TEXT, capture_token TEXT,
                    PRIMARY KEY(source_repository, source_snapshot, destination_repository)
                )"""
            )
            snapshot_columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
            if "capture_token" not in snapshot_columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN capture_token TEXT")
                # Older releases used a different, unrecorded tag value. Leave
                # those rows unassociated rather than guessing a token.
            copy_columns = {row[1] for row in db.execute("PRAGMA table_info(copies)")}
            if "capture_token" not in copy_columns:
                db.execute("ALTER TABLE copies ADD COLUMN capture_token TEXT")
                db.execute(
                    """UPDATE copies SET capture_token = (
                           SELECT snapshots.capture_token FROM snapshots
                           WHERE snapshots.repository = copies.source_repository
                             AND snapshots.snapshot = copies.source_snapshot
                       ) WHERE capture_token IS NULL"""
                )
        self.receipts_path.chmod(0o600)

    def _receipts(self):
        return sqlite3.connect(self.receipts_path)

    def _engine(self, name):
        if name not in self.bindings.repositories:
            raise ServiceError("Unknown repository")
        binding = self.bindings.repositories[name]
        if not binding.password_file.is_file() or binding.password_file.stat().st_mode & 0o077:
            raise ServiceError("Repository password file must exist and be private")
        return ResticRunner(SnapshotProfile(name=name, repository=binding.repository,
                                           password_file=str(binding.password_file), retry_lock="0s"))

    def _identity(self, name):
        if name not in self.bindings.repositories:
            raise ServiceError("Unknown repository")
        value = self.bindings.repositories[name].repository
        if value.startswith("/"):
            value = str(Path(value).resolve())
        return value

    def _execute(
        self,
        name,
        argv,
        *,
        label,
        other=(),
        cancel=None,
        operation_id=None,
        mutating=False,
        finalize: Callable[[OperationPlan, OperationRun], object] | None = None,
    ):
        if cancel is not None and cancel.is_set():
            raise ServiceError("Operation cancelled before dispatch")
        # Restic configuration cannot be redirected by inherited RESTIC_* options.
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        repositories = sorted(set((name, *other)), key=self._identity)
        plans = {}
        for repository in repositories:
            identity = self._identity(repository)
            plans[repository] = OperationPlan(
                operation_id=(operation_id if repository == name and operation_id else uuid.uuid4().hex),
                repository_id=repository_key(identity),
                label=label,
                argv=tuple(argv),
                env=env,
            )
        with ExitStack() as stack:
            leases = []
            try:
                for plan in plans.values():
                    leases.append(stack.enter_context(self.ledger.begin(plan)))
            except BaseException:
                # No subprocess has been dispatched yet; release earlier locks
                # as a definite failure instead of misclassifying them as crashes.
                for lease in leases:
                    lease.finish(OperationStatus.FAILED, None)
                raise
            plan = plans[name]
            result = self.runner.run(plan, cancel_event=cancel)
            if mutating and result.status is not OperationStatus.SUCCEEDED:
                # These outcomes can leave repository state partially changed.
                # Exiting with unfinished leases records UNCERTAIN for explicit
                # reconciliation and prevents an automatic replay.
                raise ServiceError("Repository operation was interrupted; reconcile run status before retrying")
            if finalize is not None and result.status is OperationStatus.SUCCEEDED:
                finalize(plan, result)
            for lease in leases:
                lease.finish(result.status, result.exit_code)
        if result.exit_code != 0 or str(getattr(result.status, "value", result.status)) != "succeeded":
            raise ServiceError("Repository operation failed; inspect run status before retrying")
        return plan, result

    def initialize(self, repository, *, source=None, cancel=None):
        engine = self._engine(repository)
        args = engine.init_args()
        if source is not None:
            origin = self._engine(source)
            args += ["--from-repo", origin.profile.repository, "--from-password-file", origin.profile.password_file,
                     "--copy-chunker-params"]
        plan, _ = self._execute(
            repository,
            args,
            label="initialize",
            other=(source,) if source else (),
            cancel=cancel,
            mutating=True,
        )
        return {"operation_id": plan.operation_id, "repository": repository}

    def _job(self, name):
        try:
            return next(job for job in self.config.jobs if job.name == name)
        except StopIteration:
            raise ServiceError("Unknown job") from None

    def capture(self, job_name, *, cancel=None):
        job = self._job(job_name)
        sources = tuple(source for source in self.config.sources if source.name in job.sources)
        protected = [self.state]
        for binding in self.bindings.repositories.values():
            if binding.repository.startswith("/"):
                protected.append(Path(binding.repository).resolve())
        for source in sources:
            for raw in source.paths:
                if any(_overlap(Path(raw).resolve(), path) for path in protected):
                    raise ServiceError("Capture source overlaps repository or runtime state")
        local = self.bindings.repositories[job.repository].repository
        if not local.startswith("/"):
            raise ServiceError("Local-first jobs require an absolute local repository path")
        # Preserve pending recovery points: capacity failure never triggers deletion.
        ancestor = Path(local)
        while not ancestor.exists():
            ancestor = ancestor.parent
        if shutil.disk_usage(ancestor).free < 64 * 1024 * 1024:
            raise ServiceError("Insufficient local space; existing recovery points retained")
        engine = self._engine(job.repository)
        capture_id = uuid.uuid4().hex
        receipt = {}

        def finalize_capture(plan, result):
            tail = result.stdout_tail
            if isinstance(tail, bytes):
                tail = tail.decode("utf-8", errors="replace")
            snapshot = None
            for line in tail.splitlines():
                try:
                    message = json.loads(line)
                    if message.get("message_type") == "summary":
                        snapshot = message.get("snapshot_id")
                except (ValueError, AttributeError):
                    continue
            snapshot = _snapshot_id(snapshot)
            with self._receipts() as db:
                db.execute(
                    """INSERT INTO snapshots
                       (repository, snapshot, job, operation, capture_token)
                       VALUES (?, ?, ?, ?, ?)""",
                    (job.repository, snapshot, job.name, plan.operation_id, capture_id),
                )
            receipt["snapshot"] = snapshot

        with capture_sources(sources, self.state, cancel=cancel, database_bindings=self.bindings.database_bindings) as paths:
            args = [*engine.base_args(), "backup", "--tag", f"bbackup-job={job.name}",
                    "--tag", f"bbackup-capture={capture_id}", "--", *paths]
            plan, _ = self._execute(
                job.repository,
                args,
                label="capture",
                cancel=cancel,
                operation_id=capture_id,
                mutating=True,
                finalize=finalize_capture,
            )
        snapshot = receipt["snapshot"]
        return {"operation_id": plan.operation_id, "repository": job.repository, "snapshot_id": snapshot,
                "local_complete": True, "required_replicas": list(job.replicas), "cloud_complete": not job.replicas}

    def copy(self, source, destination, snapshot, *, cancel=None):
        snapshot = _snapshot_id(snapshot)
        if source == destination or self._identity(source) == self._identity(destination):
            raise ServiceError("Copy requires independent repositories")
        with self._receipts() as db:
            source_receipt = db.execute(
                "SELECT capture_token FROM snapshots WHERE repository=? AND snapshot=?",
                (source, snapshot),
            ).fetchone()
            if source_receipt is None:
                raise ServiceError("Copy requires a recorded successful local snapshot")
            capture_token = source_receipt[0]
            if not capture_token:
                raise ServiceError("Copy requires a recoverable capture token")
        capture_tag = f"bbackup-capture={capture_token}"
        src, dst = self._engine(source), self._engine(destination)
        args = [*dst.base_args(), "copy", "--from-repo", src.profile.repository,
                "--from-password-file", src.profile.password_file, "--tag", capture_tag, "--", snapshot]
        receipt = {}

        def finalize_copy(plan, _copy_result):
            # The source tag is unique to this capture, so --latest bounds the
            # listing even when unrelated repository history is large.
            inspect_args = [*dst.base_args(), "snapshots", "--tag", capture_tag, "--latest", "1"]
            inspect_plan = OperationPlan.create(
                self._identity(destination), "inspect", inspect_args, env=plan.env
            )
            result = self.runner.run(inspect_plan, cancel_event=cancel)
            if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
                raise ServiceError("Destination identity could not be verified")
            tail = result.stdout_tail
            try:
                candidates = json.loads(tail)
                matches = [
                    item["id"]
                    for item in candidates
                    if capture_tag in item.get("tags", [])
                    and item.get("original", item.get("id")) == snapshot
                ]
            except (ValueError, TypeError, KeyError):
                raise ServiceError("Destination identity could not be verified") from None
            if len(matches) != 1:
                raise ServiceError("Destination identity is ambiguous or missing")
            copied = _snapshot_id(matches[0])
            with self._receipts() as db:
                db.execute(
                    """INSERT INTO copies
                       (source_repository, source_snapshot, destination_repository,
                        destination_snapshot, operation, capture_token)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_repository, source_snapshot, destination_repository)
                       DO UPDATE SET destination_snapshot=excluded.destination_snapshot,
                         operation=excluded.operation, capture_token=excluded.capture_token""",
                    (source, snapshot, destination, copied, plan.operation_id, capture_token),
                )
            receipt["snapshot"] = copied

        plan, _ = self._execute(
            destination,
            args,
            label="copy",
            other=(source,),
            cancel=cancel,
            mutating=True,
            finalize=finalize_copy,
        )
        copied = receipt["snapshot"]
        return {"operation_id": plan.operation_id, "source_snapshot_id": snapshot,
                "destination_snapshot_id": copied, "destination_repository": destination}

    def check(self, repository, *, cancel=None):
        engine = self._engine(repository)
        plan, _ = self._execute(repository, [*engine.check_args(), "--read-data"], label="check", cancel=cancel)
        return {"operation_id": plan.operation_id, "repository": repository, "repository_verified": True,
                "recovery_demonstrated": False}

    def restore(self, repository, snapshot, target: Path, *, cancel=None):
        snapshot = _snapshot_id(snapshot)
        if not target.is_absolute() or ".." in target.parts:
            raise ServiceError("Restore target must be an absolute new directory")
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise ServiceError("Restore target cannot traverse symlinks")
        resolved = target.resolve()
        protected = [self.state]
        protected += [Path(p).resolve() for src in self.config.sources for p in src.paths]
        for binding in self.bindings.repositories.values():
            if binding.repository.startswith("/"):
                protected.append(Path(binding.repository).resolve())
            protected.append(binding.password_file.resolve())
        if any(_overlap(resolved, path) for path in protected):
            raise ServiceError("Restore target overlaps protected data")
        engine = self._engine(repository)
        try:
            target.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError:
            raise ServiceError("Restore target must be new and have an existing parent") from None
        try:
            plan, _ = self._execute(
                repository,
                [*engine.restore_args(snapshot, str(target)), "--verify"],
                label="restore",
                cancel=cancel,
                mutating=True,
            )
        except Exception:
            try:
                target.rmdir()
            except OSError:
                pass
            raise
        return {"operation_id": plan.operation_id, "snapshot_id": snapshot,
                "files_verified": True, "application_validated": False}

    def snapshots(self, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ServiceError("Invalid pagination")
        with self._receipts() as db:
            rows = db.execute("SELECT repository,snapshot,job,operation FROM snapshots ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(zip(("repository", "snapshot_id", "job", "operation_id"), row)) for row in rows]

    def run_job(self, job_name, *, cancel=None):
        result = self.capture(job_name, cancel=cancel)
        result["replicas"] = [self.copy(result["repository"], destination, result["snapshot_id"], cancel=cancel)
                              for destination in self._job(job_name).replicas]
        result["cloud_complete"] = True
        return result
