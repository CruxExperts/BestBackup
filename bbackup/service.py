"""Shared production operations. CLI and dashboard must use this boundary."""
from __future__ import annotations

from contextlib import ExitStack
from collections.abc import Callable
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
from urllib.parse import urlsplit

from .capture import capture_sources
from .config import SnapshotProfile
from .models import Configuration, HostBindings
from .docker_recovery import DockerRecoveryError, DockerRecoveryUncertainError, DockerRuntime
from .snapshot import ResticRunner
from .resource_limits import ResourceLimitError, ResourceMonitor
from .progress import stdout_progress_handler
from .operations import (
    OperationLedger,
    OperationPlan,
    OperationRun,
    OperationRunner,
    OperationStatus,
    ReconciliationOutcome,
    repository_key,
)


class ServiceError(RuntimeError):
    """A sanitized, stable operational failure."""


class ReplicaJobError(ServiceError):
    """A local capture succeeded but one or more required copies failed."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__("One or more replica copies failed; review each destination before retrying")


class ResourceBudgetExceeded(ServiceError):
    """Sanitized capacity failure with evidence and no credential or source paths."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__("Remote-primary resource budget was exceeded or cannot be verified")


class _RemoteResticRunner(ResticRunner):
    def base_args(self):
        args = super().base_args()
        args.insert(1, "--no-cache")
        return args


def _snapshot_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ServiceError("A complete snapshot ID is required")
    return value


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


class BackupService:
    def __init__(self, config: Configuration, bindings: HostBindings, *,
                 policy_bytes: bytes | None = None, binding_bytes: bytes | None = None):
        self.config, self.bindings = config, bindings
        self.policy_bytes, self.binding_bytes = policy_bytes, binding_bytes
        if config.schema_version != bindings.schema_version:
            raise ServiceError("Policy and private binding versions must match")
        if set(bindings.repositories) != {r.name for r in config.repositories}:
            raise ServiceError("Host bindings must match declared repositories")
        self.repository_roles = {repository.name: repository.role for repository in config.repositories}
        for name, role in self.repository_roles.items():
            if role != "remote-primary":
                continue
            location = bindings.repositories[name].repository
            try:
                remote = urlsplit(location[3:]) if location.startswith("s3:") else None
            except ValueError:
                remote = None
            if (remote is None or remote.scheme != "https" or not remote.hostname
                    or not remote.path.strip("/") or remote.username or remote.password
                    or remote.query or remote.fragment or bindings.staging_dir is None):
                raise ServiceError("Remote-primary requires an HTTPS S3 binding and private staging")
        if config.schema_version == 3:
            if set(bindings.source_bindings) != {source.name for source in config.sources}:
                raise ServiceError("Private source bindings must match portable sources")
            self.sources = tuple(bindings.source_bindings[source.name].resolve(source)
                                 for source in config.sources)
        else:
            self.sources = config.sources
        for source in self.sources:
            if source.kind in ("postgresql", "mysql", "mariadb"):
                binding = bindings.database_bindings.get(source.connection)
                if binding is None or binding.kind != source.kind:
                    raise ServiceError("Database source requires a matching private connection binding")
            elif source.kind == "docker":
                binding = bindings.docker_bindings.get(source.connection)
                if binding is None or binding.kind != "docker":
                    raise ServiceError("Docker source requires a matching private local connection binding")
        self.state = bindings.state_dir.resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.stat().st_mode & 0o077:
            raise ServiceError("State directory must be private")
        self.runner = OperationRunner()
        self.docker_runtime = (
            DockerRuntime(self.state, bindings.docker_bindings)
            if bindings.docker_bindings else None
        )
        self.ledger = OperationLedger(self.state / "operations.sqlite3")
        self.receipts_path = self.state / "snapshots.sqlite3"
        with self._receipts() as db:
            # Serialize schema decisions against every SQLite writer. A second
            # read connection can back up the committed pre-migration database
            # while this reserved write lock prevents it from changing.
            db.execute("BEGIN IMMEDIATE")
            expected = {
                "snapshots": {"repository", "snapshot", "job", "operation",
                              "capture_token", "required_replicas"},
                "copies": {"source_repository", "source_snapshot", "destination_repository",
                           "destination_snapshot", "operation", "capture_token", "destination_identity"},
                "checkpoints": {"repository", "repository_identity", "snapshot", "operation",
                                "manifest_sha256", "captured_at", "expires_at"},
            }
            existing = {
                table: {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for table in expected
            }
            if any(existing.values()) and (
                any(not columns.issubset(existing[table]) for table, columns in expected.items())
                or db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='snapshots_required_replicas_immutable'"
                ).fetchone() is None
            ):
                self._backup_receipts()
            db.execute(
                """CREATE TABLE IF NOT EXISTS snapshots (
                    repository TEXT, snapshot TEXT, job TEXT, operation TEXT,
                    capture_token TEXT, required_replicas TEXT,
                    PRIMARY KEY(repository, snapshot)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS copies (
                    source_repository TEXT, source_snapshot TEXT,
                    destination_repository TEXT, destination_snapshot TEXT,
                    operation TEXT, capture_token TEXT, destination_identity TEXT,
                    PRIMARY KEY(source_repository, source_snapshot, destination_repository)
                )"""
            )
            snapshot_columns = {
                row[1] for row in db.execute("PRAGMA table_info(snapshots)")
            }
            if "capture_token" not in snapshot_columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN capture_token TEXT")
                # Older releases used a different, unrecorded tag value. Leave
                # those rows unassociated rather than guessing a token.
            if "required_replicas" not in snapshot_columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN required_replicas TEXT")
                # Legacy receipts lack immutable capture-time copy requirements.
                # Keep the evidence NULL so retention can never assume them safe.
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
            if "destination_identity" not in copy_columns:
                db.execute("ALTER TABLE copies ADD COLUMN destination_identity TEXT")
            db.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    repository TEXT NOT NULL, repository_identity TEXT NOT NULL,
                    snapshot TEXT NOT NULL, operation TEXT PRIMARY KEY,
                    manifest_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS snapshots_required_replicas_immutable
                   BEFORE UPDATE OF required_replicas ON snapshots
                   WHEN OLD.required_replicas IS NOT NEW.required_replicas
                   BEGIN SELECT RAISE(ABORT, 'required replicas are immutable'); END"""
            )
        self.receipts_path.chmod(0o600)

    def _backup_receipts(self):
        """Persist a private, compressed, consistent backup before schema changes."""
        directory = self.state / "state-backups"
        if directory.is_symlink():
            raise ServiceError("State backup directory must not be a symbolic link")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir() or directory.stat().st_mode & 0o077:
            raise ServiceError("State backup directory must be private")
        try:
            with tempfile.TemporaryDirectory(prefix=".state-upgrade-", dir=self.state) as temporary:
                raw = Path(temporary) / "receipts.sqlite3"
                descriptor = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                with sqlite3.connect(self.receipts_path) as source, sqlite3.connect(raw) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ServiceError("Pre-migration state backup failed validation")
                compressed = Path(temporary) / "receipts.sqlite3.gz"
                with raw.open("rb") as incoming, compressed.open("xb") as output:
                    compressed.chmod(0o600)
                    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as archive:
                        shutil.copyfileobj(incoming, archive)
                    output.flush()
                    os.fsync(output.fileno())
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                final = directory / f"receipts-{stamp}-{uuid.uuid4().hex}.sqlite3.gz"
                os.link(compressed, final)
                for parent in (directory, self.state):
                    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
        except (OSError, sqlite3.Error):
            raise ServiceError("Pre-migration state backup failed; existing state was not migrated") from None

    def _receipts(self):
        return sqlite3.connect(self.receipts_path)

    def _engine(self, name):
        if name not in self.bindings.repositories:
            raise ServiceError("Unknown repository")
        binding = self.bindings.repositories[name]
        if not binding.password_file.is_file() or binding.password_file.stat().st_mode & 0o077:
            raise ServiceError("Repository password file must exist and be private")
        runner_type = _RemoteResticRunner if self.repository_roles[name] == "remote-primary" else ResticRunner
        return runner_type(SnapshotProfile(name=name, repository=binding.repository,
                                           password_file=str(binding.password_file), retry_lock="0s"))

    def _resource_monitor(self, name):
        if self.repository_roles[name] != "remote-primary":
            return None
        budgets = [job.resource_budget for job in self.config.jobs if job.repository == name]
        if not budgets or self.bindings.staging_dir is None:
            raise ServiceError("Remote-primary requires a configured resource budget and private staging")
        # One repository has one physical staging volume; every job must agree on its ceiling.
        if any(budget != budgets[0] for budget in budgets):
            raise ServiceError("Remote-primary jobs sharing a repository require one resource budget")
        try:
            return ResourceMonitor(self.bindings.staging_dir, self.state, budgets[0])
        except ResourceLimitError as exc:
            raise ResourceBudgetExceeded({"resource_budget_exceeded": True, "reason": str(exc),
                                          "repository": name, "local_complete": False,
                                          "cloud_complete": False, "replayed": False}) from None

    def _operation_environment(self, name):
        if name not in self.repository_roles:
            raise ServiceError("Unknown repository")
        if self.repository_roles[name] != "remote-primary":
            return {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            raise ServiceError("Remote-primary requires explicit provider environment credentials")
        staging = self.bindings.staging_dir
        assert staging is not None
        environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(staging),
                       "TMPDIR": str(staging)}
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                    "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_CA_BUNDLE", "SSL_CERT_FILE"):
            if key in os.environ:
                environment[key] = os.environ[key]
        return environment

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
        on_stdout=None,
        resource_monitor=None,
        after_run: Callable[[], None] | None = None,
        timeout_seconds: int = 3600,
    ):
        if cancel is not None and cancel.is_set():
            raise ServiceError("Operation cancelled before dispatch")
        # Restic configuration cannot be redirected by inherited RESTIC_* options.
        repositories = sorted(set((name, *other)), key=self._identity)
        remote_participants = [repository for repository in repositories
                               if self.repository_roles[repository] == "remote-primary"]
        env = self._operation_environment(remote_participants[0] if remote_participants else name)
        if remote_participants and argv and argv[0] == "restic" and "--no-cache" not in argv:
            argv = (argv[0], "--no-cache", *argv[1:])
        plans = {}
        for repository in repositories:
            identity = self._identity(repository)
            plans[repository] = OperationPlan(
                operation_id=(operation_id if repository == name and operation_id else uuid.uuid4().hex),
                repository_id=repository_key(identity),
                label=label,
                argv=tuple(argv),
                env=env,
                timeout_seconds=timeout_seconds,
            )
        with ExitStack() as stack:
            if remote_participants and resource_monitor is None:
                resource_monitor = self._resource_monitor(remote_participants[0])
                stack.callback(resource_monitor.close)
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
            run_options = {"cancel_event": cancel}
            on_stdout = stdout_progress_handler(plan.operation_id, label, on_stdout)
            if on_stdout is not None:
                run_options["on_stdout"] = on_stdout
            if resource_monitor is not None:
                run_options["on_tick"] = resource_monitor.check
            try:
                try:
                    result = self.runner.run(plan, **run_options)
                finally:
                    if after_run is not None:
                        after_run()
                if resource_monitor is not None:
                    resource_monitor.check()
                if finalize is not None and result.status is OperationStatus.SUCCEEDED:
                    finalize(plan, result)
            except BaseException as exc:
                # Source is a read-only copy participant. Only the destination
                # can be uncertain after a failed or interrupted dispatch.
                for repository, lease in zip(plans, leases):
                    if repository != name:
                        lease.finish(OperationStatus.FAILED, None)
                if isinstance(exc, ResourceLimitError):
                    raise ResourceBudgetExceeded({
                        "resource_budget_exceeded": True, "reason": str(exc),
                        "repository": name, "operation_id": plan.operation_id,
                        "local_complete": False, "cloud_complete": False,
                        "replayed": False, "resource_observation": resource_monitor.result(),
                    }) from None
                raise
            if mutating and result.status is not OperationStatus.SUCCEEDED:
                # These outcomes can leave repository state partially changed.
                # Exiting with unfinished leases records UNCERTAIN for explicit
                # reconciliation and prevents an automatic replay.
                for repository, lease in zip(plans, leases):
                    if repository != name:
                        lease.finish(OperationStatus.FAILED, None)
                raise ServiceError("Repository operation was interrupted; reconcile run status before retrying")
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
        remote_primary = self.repository_roles[job.repository] == "remote-primary"
        if remote_primary:
            self._operation_environment(job.repository)
        sources = tuple(source for source in self.sources if source.name in job.sources)
        covered = job.coverage_policy is not None
        coverage_exclusions = ()
        if covered:
            from .linux_coverage import check_coverage
            if self.policy_bytes is None or self.binding_bytes is None:
                raise ServiceError("Covered capture requires exact enrolled policy and binding snapshots")
            declaration = self.bindings.coverage_declarations.get(job.name)
            if declaration is None:
                raise ServiceError("Covered capture declaration is missing")
            coverage = check_coverage(declaration, job.coverage_policy, state=self.state,
                                      policy_bytes=self.policy_bytes, binding_bytes=self.binding_bytes)
            if not coverage.capture_ready:
                raise ServiceError("Covered capture is not ready; inspect and review current coverage")
            expected = {(source.name, Path(path), source.kind)
                        for source in sources for path in source.paths}
            approved = {(source_id, path, adapter)
                        for _, path, source_id, adapter in coverage.approved_roots}
            if (any(source.kind != "files" for source in sources)
                    or expected != approved):
                raise ServiceError("Covered capture sources differ from reviewed root mappings")
            coverage_exclusions = coverage.approved_exclusions
        if remote_primary and any(source.kind == "docker" for source in sources):
            raise ServiceError("Remote-primary Docker capture has no bounded export support")
        monitor = None
        protected = [self.state]
        if remote_primary:
            protected.append(self.bindings.staging_dir.resolve())
        for binding in self.bindings.repositories.values():
            if binding.repository.startswith("/"):
                protected.append(Path(binding.repository).resolve())
        if not covered:
            protected.extend(Path(path).resolve() for path in self.bindings.sensitive_paths)
        for source in sources:
            for raw in source.paths:
                if any(_overlap(Path(raw).resolve(), path) for path in protected):
                    raise ServiceError("Capture source overlaps repository, runtime state, or credentials")
        if not remote_primary:
            local = self.bindings.repositories[job.repository].repository
            if not local.startswith("/"):
                raise ServiceError("Local-first jobs require an absolute local repository path")
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
                       (repository, snapshot, job, operation, capture_token, required_replicas)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job.repository, snapshot, job.name, plan.operation_id, capture_id,
                     json.dumps({"schema_version": 1, "bindings": {
                         name: repository_key(self._identity(name)) for name in job.replicas
                     }}, sort_keys=True, separators=(",", ":"))),
                )
            receipt["snapshot"] = snapshot

        with ExitStack() as resource_stack, ExitStack() as capture_stack:
            if remote_primary:
                monitor = self._resource_monitor(job.repository)
                resource_stack.callback(monitor.close)
            try:
                paths = capture_stack.enter_context(capture_sources(
                    sources,
                    self.bindings.staging_dir if remote_primary else self.state,
                    timeout=job.timeout_seconds,
                    cancel=cancel,
                    database_bindings=self.bindings.database_bindings,
                    docker_runtime=self.docker_runtime,
                    protected_paths=tuple(protected),
                    operation_id=capture_id,
                    resource_monitor=monitor,
                ))
            except ResourceLimitError as exc:
                raise ResourceBudgetExceeded({
                    "resource_budget_exceeded": True, "reason": str(exc),
                    "repository": job.repository, "operation_id": capture_id,
                    "local_complete": False, "cloud_complete": False,
                    "replayed": False, "resource_observation": monitor.result(),
                }) from None
            except DockerRecoveryError as exc:
                raise ServiceError(str(exc)) from None
            lifecycle = capture_stack.pop_all()

            def finish_lifecycle():
                try:
                    lifecycle.close()
                except DockerRecoveryError as exc:
                    raise ServiceError(str(exc)) from None

            try:
                args = [*engine.base_args(), "backup", "--tag", f"bbackup-job={job.name}",
                        "--tag", f"bbackup-capture={capture_id}"]
                if coverage_exclusions:
                    from .linux_coverage import restic_literal_exclusion
                    for excluded in coverage_exclusions:
                        args.extend(("--exclude", restic_literal_exclusion(excluded)))
                args.extend(("--", *paths))
                plan, _ = self._execute(
                    job.repository,
                    args,
                    label="capture",
                    cancel=cancel,
                    operation_id=capture_id,
                    mutating=True,
                    resource_monitor=monitor,
                    finalize=finalize_capture,
                    after_run=finish_lifecycle,
                    timeout_seconds=job.timeout_seconds,
                )
            finally:
                finish_lifecycle()
        snapshot = receipt["snapshot"]
        response = {"operation_id": plan.operation_id, "repository": job.repository, "snapshot_id": snapshot,
                    "primary_destination": {"repository": job.repository,
                                            "kind": "remote" if remote_primary else "local"},
                    "local_complete": not remote_primary, "required_replicas": list(job.replicas),
                    "cloud_complete": remote_primary, "required_replicas_complete": not job.replicas}
        if monitor is not None:
            response["resource_observation"] = monitor.result()
        return response

    def copy(self, source, destination, snapshot, *, cancel=None, timeout_seconds=3600):
        if any(self.repository_roles.get(name) == "remote-primary" for name in (source, destination)):
            raise ServiceError("Remote-primary copies do not yet have bounded resource support")
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
                        destination_snapshot, operation, capture_token, destination_identity)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_repository, source_snapshot, destination_repository)
                       DO UPDATE SET destination_snapshot=excluded.destination_snapshot,
                         operation=excluded.operation, capture_token=excluded.capture_token,
                         destination_identity=excluded.destination_identity""",
                    (source, snapshot, destination, copied, plan.operation_id, capture_token,
                     repository_key(self._identity(destination))),
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
            timeout_seconds=timeout_seconds,
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
        protected += [Path(p).resolve() for src in self.sources for p in src.paths]
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

    @staticmethod
    def _docker_scope(connection: str, daemon_id: str, container: str) -> str:
        return f"docker:{connection}:{daemon_id}:{container}"

    def docker_restore(
        self,
        repository: str,
        snapshot: str,
        source: str,
        connection: str,
        target: Path,
        container: str,
        trusted_archive: bool,
        *,
        cancel=None,
    ) -> dict[str, object]:
        snapshot = _snapshot_id(snapshot)
        if trusted_archive is not True:
            raise ServiceError("Docker restore requires explicit trusted_archive acknowledgement")
        if (not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", source)
                or not isinstance(container, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container)):
            raise ServiceError("Docker source and target container names are invalid")
        if self.docker_runtime is None or connection not in self.bindings.docker_bindings:
            raise ServiceError("Docker restore requires a configured local Unix-socket binding")
        if not target.is_absolute() or ".." in target.parts:
            raise ServiceError("Docker restore target must be an absolute new directory")
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise ServiceError("Docker restore target cannot traverse symlinks")
        if target.exists():
            raise ServiceError("Docker restore target directory must be new")
        if not target.parent.is_dir():
            raise ServiceError("Docker restore target parent must already exist")
        protected = [self.state]
        protected.extend(Path(path).resolve() for path in self.bindings.sensitive_paths)
        for source_config in self.sources:
            protected.extend(Path(path).resolve() for path in source_config.paths)
        for binding in self.bindings.repositories.values():
            if binding.repository.startswith("/"):
                protected.append(Path(binding.repository).resolve())
            protected.append(binding.password_file.resolve())
        try:
            protected.extend(self.docker_runtime.protected_mount_paths())
        except DockerRecoveryError as exc:
            raise ServiceError(str(exc)) from None
        resolved = target.resolve()
        if any(_overlap(resolved, path) for path in protected):
            raise ServiceError("Docker restore target overlaps protected data or an existing Docker resource")
        engine = self._engine(repository)
        try:
            target.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError:
            raise ServiceError("Docker restore target must be new and have an existing parent") from None
        restore_plan, _ = self._execute(
            repository,
            [*engine.restore_args(snapshot, str(target)), "--verify"],
            label="docker-restore-files",
            cancel=cancel,
            mutating=True,
        )
        try:
            daemon_id = self.docker_runtime.daemon_identity(connection)
        except DockerRecoveryError as exc:
            raise ServiceError(str(exc)) from None
        plan = OperationPlan.create(
            self._docker_scope(connection, daemon_id, container),
            "docker-restore",
            ["docker", "create", container],
            env={},
            destructive=True,
            timeout_seconds=self.docker_runtime.startup_timeout + self.docker_runtime.request_timeout,
        )
        try:
            with self.ledger.begin(plan) as lease:
                try:
                    result = self.docker_runtime.restore_new(
                        connection,
                        target,
                        source_name=source,
                        container_name=container,
                        trusted_archive=True,
                        expected_daemon_id=daemon_id,
                        cancel=cancel,
                    )
                except DockerRecoveryUncertainError as exc:
                    raise ServiceError(f"{exc} (operation_id {plan.operation_id})") from None
                except DockerRecoveryError as exc:
                    status = OperationStatus.CANCELLED if cancel is not None and cancel.is_set() else OperationStatus.FAILED
                    lease.finish(status, None)
                    raise ServiceError(str(exc)) from None
                lease.finish(OperationStatus.SUCCEEDED, 0)
        except ServiceError:
            raise
        except Exception:
            raise
        return {
            "operation_id": plan.operation_id,
            "restore_operation_id": restore_plan.operation_id,
            "repository": repository,
            "snapshot_id": snapshot,
            "target": str(target),
            **result,
        }

    def docker_recover(self, connection: str, reviewed: bool, *, cancel=None):
        if self.docker_runtime is None:
            raise ServiceError("Docker recovery requires a configured local Unix-socket binding")
        try:
            return self.docker_runtime.recover(
                connection, reviewed=reviewed, ledger=self.ledger, cancel=cancel
            )
        except DockerRecoveryError as exc:
            raise ServiceError(str(exc)) from None

    def docker_reconcile(
        self,
        connection: str,
        container: str,
        operation_id: str,
        outcome: ReconciliationOutcome,
        docker_reviewed: bool,
    ) -> dict[str, object]:
        if docker_reviewed is not True:
            raise ServiceError("Docker operation reconciliation requires explicit review")
        if (self.docker_runtime is None
                or not isinstance(container, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container)
                or not isinstance(operation_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", operation_id)):
            raise ServiceError("Docker reconciliation identity is invalid")
        try:
            daemon_id = self.docker_runtime.daemon_identity(connection)
        except DockerRecoveryError as exc:
            raise ServiceError(str(exc)) from None
        self.ledger.reconcile(
            repository_key(self._docker_scope(connection, daemon_id, container)),
            operation_id,
            outcome,
        )
        return {
            "operation_id": operation_id,
            "reviewed_outcome": outcome.value,
            "replayed": False,
        }

    def _retention_snapshot_plan(self, job, operation_plan, cancel):
        """Inspect snapshots and restic's daily forget selection under one lease."""
        engine = self._engine(job.repository)
        tag = f"bbackup-job={job.name}"
        forget_args = [
            *engine.base_args(), "forget", "--dry-run",
            "--keep-daily", str(job.retention.local_daily),
            "--group-by", "host", "--tag", tag,
        ]

        def run_read(argv, message):
            plan = OperationPlan(
                operation_id=operation_plan.operation_id,
                repository_id=operation_plan.repository_id,
                label=operation_plan.label,
                argv=tuple(argv),
                env=operation_plan.env,
                timeout_seconds=operation_plan.timeout_seconds,
            )
            result = self.runner.run(plan, cancel_event=cancel)
            if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
                raise ServiceError(message)
            if result.stdout_bytes != len(result.stdout_tail):
                raise ServiceError("Repository inventory exceeds the supported bound")
            return result.stdout_tail

        snapshots_json = run_read(
            [*engine.base_args(), "snapshots"],
            "Repository snapshot inventory could not be verified",
        )
        forget_json = run_read(forget_args, "Restic retention selection could not be verified")
        return self._make_retention_plan(job, snapshots_json, forget_json, operation_plan, cancel)

    def _required_copy_present(self, destination, snapshot, source_snapshot, capture_token,
                               operation_plan, cancel):
        if cancel is not None and cancel.is_set():
            raise ServiceError("Retention planning cancelled before dispatch")
        try:
            engine = self._engine(destination)
        except ServiceError:
            return False
        tag = f"bbackup-capture={capture_token}"
        plan = OperationPlan(
            operation_id=operation_plan.operation_id,
            repository_id=repository_key(self._identity(destination)),
            label=operation_plan.label,
            argv=tuple([*engine.base_args(), "snapshots", "--tag", tag, "--", snapshot]),
            env=operation_plan.env,
            timeout_seconds=operation_plan.timeout_seconds,
        )
        result = self.runner.run(plan, cancel_event=cancel)
        if cancel is not None and cancel.is_set():
            raise ServiceError("Retention planning cancelled")
        if (result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0
                or result.stdout_bytes != len(result.stdout_tail)):
            return False
        try:
            inventory = json.loads(result.stdout_tail.decode("utf-8"))
            return (isinstance(inventory, list) and len(inventory) == 1
                    and isinstance(inventory[0], dict)
                    and inventory[0].get("id") == snapshot
                    and inventory[0].get("original", snapshot) == source_snapshot
                    and isinstance(inventory[0].get("tags"), list)
                    and tag in inventory[0]["tags"])
        except (ValueError, UnicodeDecodeError, TypeError):
            return False

    @staticmethod
    def _retention_inventory(raw):
        try:
            inventory = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ServiceError("Repository snapshot inventory is invalid") from None
        if not isinstance(inventory, list):
            raise ServiceError("Repository snapshot inventory is invalid")
        snapshots = {}
        for item in inventory:
            if not isinstance(item, dict):
                raise ServiceError("Repository snapshot inventory is invalid")
            snapshot_id = item.get("id")
            tags = item.get("tags") or []
            if (not isinstance(snapshot_id, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
                    or not isinstance(tags, list)
                    or any(not isinstance(tag, str) for tag in tags)
                    or snapshot_id in snapshots):
                raise ServiceError("Repository snapshot inventory is ambiguous")
            snapshots[snapshot_id] = item
        canonical = json.dumps(
            [snapshots[snapshot_id] for snapshot_id in sorted(snapshots)],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return snapshots, hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _forget_snapshot_ids(raw):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ServiceError("Restic retention selection is invalid") from None
        payloads = []
        try:
            payloads.append(json.loads(text))
        except ValueError:
            try:
                payloads.extend(json.loads(line) for line in text.splitlines() if line.strip())
            except ValueError:
                raise ServiceError("Restic retention selection is invalid") from None
        if not payloads:
            raise ServiceError("Restic retention selection is invalid")
        removed = set()
        for payload in payloads:
            groups = payload if isinstance(payload, list) else [payload]
            if not isinstance(groups, list):
                raise ServiceError("Restic retention selection is invalid")
            for group in groups:
                if not isinstance(group, dict):
                    raise ServiceError("Restic retention selection is invalid")
                if "remove" not in group:
                    if group.get("message_type") == "summary":
                        continue
                    raise ServiceError("Restic retention selection is incomplete")
                snapshots = group["remove"]
                if not isinstance(snapshots, list):
                    raise ServiceError("Restic retention selection is invalid")
                for item in snapshots:
                    if not isinstance(item, dict):
                        raise ServiceError("Restic retention selection is invalid")
                    removed.add(_snapshot_id(item.get("id")))
        return removed

    def _make_retention_plan(self, job, snapshots_json, forget_json, operation_plan, cancel):
        snapshots, inventory_digest = self._retention_inventory(snapshots_json)
        restic_selected = self._forget_snapshot_ids(forget_json)
        if not restic_selected <= snapshots.keys():
            raise ServiceError("Repository changed while retention was being planned")
        job_tag = f"bbackup-job={job.name}"
        if any(job_tag not in (snapshots[snapshot_id].get("tags") or [])
               for snapshot_id in restic_selected):
            raise ServiceError("Restic selected a snapshot outside the configured job")

        with self._receipts() as db:
            receipts = db.execute(
                """SELECT snapshot, job, capture_token, required_replicas
                   FROM snapshots WHERE repository=? ORDER BY snapshot""",
                (job.repository,),
            ).fetchall()
            copies = db.execute(
                """SELECT source_snapshot, destination_repository, destination_snapshot,
                          capture_token, destination_identity
                   FROM copies WHERE source_repository=? ORDER BY source_snapshot, destination_repository""",
                (job.repository,),
            ).fetchall()

        try:
            receipt_digest = hashlib.sha256(json.dumps(
                {"snapshots": receipts, "copies": copies},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            raise ServiceError("Local retention receipt evidence is invalid") from None
        receipt_by_snapshot = {row[0]: row[1:] for row in receipts}
        copy_evidence = {
            (source_snapshot, destination, capture_token, destination_identity): destination_snapshot
            for source_snapshot, destination, destination_snapshot, capture_token, destination_identity in copies
            if (isinstance(destination_snapshot, str)
                and re.fullmatch(r"[0-9a-f]{64}", destination_snapshot)
                and destination in self.bindings.repositories
                and isinstance(capture_token, str) and capture_token
                and isinstance(destination_identity, str)
                and re.fullmatch(r"[0-9a-f]{64}", destination_identity))
        }

        def required_replicas(value):
            if not isinstance(value, str):
                return None
            try:
                required = json.loads(value)
            except ValueError:
                return None
            if (not isinstance(required, dict)
                    or set(required) != {"schema_version", "bindings"}
                    or type(required["schema_version"]) is not int
                    or required["schema_version"] != 1
                    or not isinstance(required["bindings"], dict)):
                return None
            expected = required["bindings"]
            if any(not isinstance(name, str) or name not in self.bindings.repositories
                   or not isinstance(identity, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", identity)
                   or identity != repository_key(self._identity(name))
                   for name, identity in expected.items()):
                return None
            return expected

        targets = []
        protected = []
        for snapshot_id in sorted(restic_selected):
            receipt = receipt_by_snapshot.get(snapshot_id)
            if receipt is None or receipt[0] != job.name:
                protected.append({"snapshot_id": snapshot_id, "reason": "unrecorded_or_foreign"})
                continue
            capture_token, raw_required = receipt[1], receipt[2]
            required = required_replicas(raw_required)
            if not capture_token or required is None:
                protected.append({
                    "snapshot_id": snapshot_id,
                    "reason": "missing_required_replica_evidence",
                })
                continue
            if any((snapshot_id, destination, capture_token, identity) not in copy_evidence
                   for destination, identity in required.items()):
                protected.append({"snapshot_id": snapshot_id, "reason": "required_copy_pending"})
                continue
            if any(not self._required_copy_present(
                    destination, copy_evidence[(snapshot_id, destination, capture_token, identity)],
                    snapshot_id, capture_token, operation_plan, cancel)
                   for destination, identity in required.items()):
                protected.append({"snapshot_id": snapshot_id, "reason": "required_copy_not_verified"})
                continue
            targets.append(snapshot_id)

        policy = {
            "local_daily": job.retention.local_daily,
            "group_by": "host",
            "job_tag": job_tag,
        }
        approval = {
            "schema_version": 1,
            "repository_id": repository_key(self._identity(job.repository)),
            "job": job.name,
            "policy": policy,
            "inventory_sha256": inventory_digest,
            "receipt_evidence_sha256": receipt_digest,
            "restic_selected_snapshot_ids": sorted(restic_selected),
            "target_snapshot_ids": targets,
        }
        token = hashlib.sha256(json.dumps(
            approval, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return {
            "plan_token": token,
            "job": job.name,
            "repository": job.repository,
            "policy": policy,
            "target_snapshot_ids": targets,
            "target_count": len(targets),
            "protected_snapshots": protected,
            "retained_by_policy_count": len(snapshots) - len(restic_selected),
            "total_snapshots": len(snapshots),
            "read_only": True,
        }

    def retention_plan(self, job_name, *, cancel=None):
        job = self._job(job_name)
        if not self.bindings.repositories[job.repository].repository.startswith("/"):
            raise ServiceError("Local retention requires an absolute local repository path")
        engine = self._engine(job.repository)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RESTIC_")}
        plan = OperationPlan.create(
            self._identity(job.repository), "retention-plan",
            [*engine.base_args(), "snapshots"], env=env,
        )
        with self.ledger.begin(plan) as lease:
            finished = False
            try:
                result = self._retention_snapshot_plan(job, plan, cancel)
                lease.finish(OperationStatus.SUCCEEDED, 0)
                finished = True
                return result
            except BaseException:
                if not finished:
                    lease.finish(OperationStatus.FAILED, None)
                raise

    def retention_apply(self, job_name, plan_token, approved, *, cancel=None):
        if approved is not True:
            raise ServiceError("Retention deletion requires explicit approval")
        if not isinstance(plan_token, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_token):
            raise ServiceError("A complete retention plan token is required")
        job = self._job(job_name)
        preview = self.retention_plan(job_name, cancel=cancel)
        if preview["plan_token"] != plan_token:
            raise ServiceError("Retention approval is stale; create and review a new plan")
        target_ids = preview["target_snapshot_ids"]
        if not target_ids:
            return {
                "operation_id": None,
                "plan_token": plan_token,
                "deleted_snapshot_ids": [],
                "deleted_count": 0,
            }

        engine = self._engine(job.repository)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RESTIC_")}
        forget_args = [*engine.base_args(), "forget", "--", *target_ids]
        plan = OperationPlan.create(
            self._identity(job.repository), "retention-apply", forget_args,
            env=env, destructive=True,
        )
        with self.ledger.begin(plan) as lease:
            mutation_started = False
            finished = False
            try:
                current = self._retention_snapshot_plan(job, plan, cancel)
                if current["plan_token"] != plan_token:
                    lease.finish(OperationStatus.FAILED, None)
                    finished = True
                    raise ServiceError("Retention approval became stale before deletion")
                if current["target_snapshot_ids"] != target_ids:
                    lease.finish(OperationStatus.FAILED, None)
                    finished = True
                    raise ServiceError("Retention targets changed before deletion")

                mutation_started = True
                result = self.runner.run(plan, cancel_event=cancel)
                if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
                    raise ServiceError(
                        "Retention mutation is uncertain; inspect run status before retrying"
                    )
                remaining = self._retention_inventory(
                    self._run_retention_query(job, plan, cancel)
                )[0]
                if any(snapshot_id in remaining for snapshot_id in target_ids):
                    raise ServiceError(
                        "Retention mutation could not be confirmed; inspect run status before retrying"
                    )
                with self._receipts() as db:
                    db.executemany(
                        "DELETE FROM snapshots WHERE repository=? AND snapshot=?",
                        [(job.repository, snapshot_id) for snapshot_id in target_ids],
                    )
                lease.finish(OperationStatus.SUCCEEDED, 0)
                finished = True
            except BaseException:
                if not mutation_started and not finished:
                    lease.finish(OperationStatus.FAILED, None)
                raise
        return {
            "operation_id": plan.operation_id,
            "plan_token": plan_token,
            "deleted_snapshot_ids": target_ids,
            "deleted_count": len(target_ids),
        }

    def _run_retention_query(self, job, operation_plan, cancel):
        engine = self._engine(job.repository)
        query_plan = OperationPlan(
            operation_id=operation_plan.operation_id,
            repository_id=operation_plan.repository_id,
            label=operation_plan.label,
            argv=tuple([*engine.base_args(), "snapshots"]),
            env=operation_plan.env,
            timeout_seconds=operation_plan.timeout_seconds,
        )
        result = self.runner.run(query_plan, cancel_event=cancel)
        if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
            raise ServiceError("Repository state after retention could not be verified")
        if result.stdout_bytes != len(result.stdout_tail):
            raise ServiceError("Repository inventory exceeds the supported bound")
        return result.stdout_tail

    def snapshots(self, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ServiceError("Invalid pagination")
        with self._receipts() as db:
            rows = db.execute("SELECT repository,snapshot,job,operation FROM snapshots ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(zip(("repository", "snapshot_id", "job", "operation_id"), row)) for row in rows]

    def database_identity(self, connection, target_database):
        binding = self.bindings.database_bindings.get(connection)
        if binding is None:
            raise ServiceError("A supported destination database binding is required")
        if binding.kind == "postgresql" and binding.service_file is not None and binding.service_name:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", target_database):
                raise ServiceError("Invalid destination database name")
            return f"database:{binding.service_file.resolve()}:{binding.service_name}:{target_database}"
        if binding.kind in ("mysql", "mariadb") and binding.options_file is not None:
            from .database_restore import DatabaseRestoreError, validate_mysql_database_name
            try:
                validate_mysql_database_name(target_database)
            except DatabaseRestoreError:
                raise ServiceError("Invalid destination database name") from None
            # Dedicated MySQL recovery needs exclusive use of an empty server.
            # Aliases are not server identity: serialize all MySQL-family restores
            # and uncertain outcomes across this shared state directory.
            return "database:mysql-family:isolated-recovery"
        raise ServiceError("A supported destination database binding is required")

    def restore_database(self, repository, snapshot, source, connection, target_database, *,
                         trusted_archive=False, isolated_server_confirmed=False, cancel=None):
        """Restore trusted native exports into a new database or empty recovery server."""
        from .database_restore import DatabaseRestoreError, DatabaseRestoreUncertainError, restore_mysql, restore_postgresql
        from .models import strict_json

        if trusted_archive is not True:
            raise ServiceError("Database archives must be explicitly trusted before execution")
        snapshot = _snapshot_id(snapshot)
        identity = self.database_identity(connection, target_database)
        binding = self.bindings.database_bindings[connection]
        if binding.kind in ("mysql", "mariadb") and isolated_server_confirmed is not True:
            raise ServiceError("MySQL/MariaDB restore requires explicit confirmation of an empty isolated server")
        engine = self._engine(repository)
        _, inspected = self._execute(repository, [*engine.base_args(), "cat", "snapshot", snapshot], label="inspect", cancel=cancel)
        if inspected.stdout_bytes != len(inspected.stdout_tail):
            raise ServiceError("Snapshot metadata exceeds the supported bound")
        try:
            metadata = strict_json(inspected.stdout_tail.decode())
            manifests = [p for p in metadata["paths"] if Path(p).name == "bbackup-capture.json"]
            if len(manifests) != 1:
                raise ValueError()
            manifest_path = Path(manifests[0])
        except (ValueError, TypeError, KeyError):
            raise ServiceError("Snapshot has no unambiguous database capture manifest") from None
        _, exported = self._execute(repository, [*engine.base_args(), "dump", snapshot, str(manifest_path)], label="inspect", cancel=cancel)
        if exported.stdout_bytes != len(exported.stdout_tail):
            raise ServiceError("Capture manifest exceeds the supported bound")
        try:
            manifest = strict_json(exported.stdout_tail.decode())
            if manifest["schema_version"] != 2:
                raise ValueError()
            entries = [item for item in manifest["exports"] if item["source"] == source]
            if len(entries) != 1:
                raise ValueError()
            entry = entries[0]
            archive_path = Path(entry["path"])
            expected_format = ("postgresql-custom" if binding.kind == "postgresql"
                               else f"{binding.kind}-single-database-sql-v1")
            if (entry["kind"] != binding.kind or entry["format"] != expected_format
                    or not archive_path.is_absolute() or ".." in archive_path.parts
                    or archive_path.parent != manifest_path.parent
                    or str(archive_path) not in metadata["paths"]):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ServiceError("Snapshot does not contain a valid selected native database export") from None
        # The bounded runner streams the archive directly to private temporary
        # storage. No original-host source configuration or ledger is required.
        with tempfile.TemporaryDirectory(prefix="database-restore-", dir=self.state) as staging:
            archive = Path(staging) / "archive.dump"
            with archive.open("xb") as output:
                archive.chmod(0o600)
                self._execute(repository, [*engine.base_args(), "dump", snapshot, str(archive_path)],
                              label="database-export", cancel=cancel, on_stdout=output.write)
            client = "pg_restore" if binding.kind == "postgresql" else binding.kind
            plan = OperationPlan.create(identity, "database-restore", [client])
            with self.ledger.begin(plan) as lease:
                try:
                    if binding.kind == "postgresql":
                        result = restore_postgresql(archive, binding, target_database,
                                                    database_options=entry.get("database_options"),
                                                    runner=self.runner, cancel=cancel)
                    else:
                        result = restore_mysql(archive, binding, target_database,
                                               source_database=entry.get("source_database"),
                                               export_format=entry["format"],
                                               character_set=entry.get("character_set"),
                                               collation=entry.get("collation"),
                                               isolated_server_confirmed=isolated_server_confirmed,
                                               runner=self.runner, cancel=cancel)
                except DatabaseRestoreUncertainError:
                    raise ServiceError("Database restore is uncertain; review the target before reconciliation") from None
                except DatabaseRestoreError:
                    lease.finish(OperationStatus.FAILED, None)
                    raise ServiceError("Database restore refused before mutation") from None
                lease.finish(OperationStatus.SUCCEEDED, 0)
        return {"operation_id": plan.operation_id, "snapshot_id": snapshot,
                "database_validated": True, "application_validated": False, "target_database": result}

    def run_job(self, job_name, *, cancel=None):
        result = self.capture(job_name, cancel=cancel)
        result["replicas"] = []
        result["replica_failures"] = []
        for destination in self._job(job_name).replicas:
            if cancel is not None and cancel.is_set():
                result["replica_failures"].append({"destination_repository": destination,
                                                   "reason": "cancelled_before_dispatch"})
                break
            try:
                result["replicas"].append(self.copy(
                    result["repository"], destination, result["snapshot_id"], cancel=cancel,
                    timeout_seconds=self._job(job_name).timeout_seconds,
                ))
                if self.bindings.repositories.get(destination) is not None:
                    if self.bindings.repositories[destination].repository.startswith("/"):
                        result["local_complete"] = True
                    else:
                        result["cloud_complete"] = True
            except (ServiceError, RuntimeError, OSError, sqlite3.Error, ValueError):
                result["replica_failures"].append({"destination_repository": destination,
                                                   "reason": "copy_failed_or_uncertain"})
                if cancel is not None and cancel.is_set():
                    break
        result["required_replicas_complete"] = not result["replica_failures"]
        if result["replica_failures"]:
            raise ReplicaJobError(result)
        return result
