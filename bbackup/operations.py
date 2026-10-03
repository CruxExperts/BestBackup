"""Safe operation execution primitives for backup maintenance workflows.

The ledger stores only opaque repository and operation IDs, a caller supplied
safe label, lifecycle timestamps, status, and an exit code. Commands,
environments, working directories, and process output stay in memory.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import selectors
import signal
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import Event


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class OperationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    RECONCILED_SUCCEEDED = "reconciled_succeeded"
    RECONCILED_FAILED = "reconciled_failed"
    RECONCILED_ABANDONED = "reconciled_abandoned"


class OperationEventKind(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    RECONCILED_SUCCEEDED = "reconciled_succeeded"
    RECONCILED_FAILED = "reconciled_failed"
    RECONCILED_ABANDONED = "reconciled_abandoned"


class ReconciliationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class OperationPlan:
    """A command to run for one repository.

    ``repository_id`` must be the opaque value returned by
    :func:`repository_key`; raw paths and command values are never written to
    the ledger. ``label`` is intentionally restricted to a short safe token.
    """

    operation_id: str
    repository_id: str
    label: str
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path | None = field(default=None, repr=False)
    env: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    timeout_seconds: float | None = 3600.0
    destructive: bool = False

    def __post_init__(self) -> None:
        if not _SAFE_TOKEN.fullmatch(self.operation_id):
            raise ValueError("operation_id must be a short safe token")
        if not re.fullmatch(r"[a-f0-9]{64}", self.repository_id):
            raise ValueError("repository_id must be created with repository_key()")
        if not _SAFE_TOKEN.fullmatch(self.label):
            raise ValueError("label must be a short safe token")
        if not self.argv or any(not isinstance(arg, str) for arg in self.argv):
            raise ValueError("argv must contain at least one string argument")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")

    @classmethod
    def create(
        cls,
        repository: str | os.PathLike[str],
        label: str,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = 3600.0,
        destructive: bool = False,
    ) -> OperationPlan:
        """Build a plan with a fresh ID and a hashed repository identity."""

        return cls(
            operation_id=uuid.uuid4().hex,
            repository_id=repository_key(repository),
            label=label,
            argv=tuple(argv),
            cwd=Path(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            timeout_seconds=timeout_seconds,
            destructive=destructive,
        )


@dataclass(frozen=True, slots=True)
class OperationRun:
    """In-memory process result with bounded output tails."""

    operation_id: str
    repository_id: str
    status: OperationStatus
    exit_code: int | None
    started_at: str
    finished_at: str
    elapsed_seconds: float
    stdout_tail: bytes = field(repr=False)
    stderr_tail: bytes = field(repr=False)
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True, slots=True)
class OperationEvent:
    """Sanitized lifecycle event returned by the ledger."""

    operation_id: str
    repository_id: str
    kind: OperationEventKind
    occurred_at: str
    exit_code: int | None = None


class OperationError(RuntimeError):
    """Base class for operation coordination failures."""


class RepositoryConflictError(OperationError):
    """Another process currently owns this repository's operation lock."""


class UncertainOperationError(OperationError):
    """A previous run did not reach a recorded terminal state."""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"repository has unresolved operation {operation_id}; reconcile it explicitly"
        )


def repository_key(repository: str | os.PathLike[str]) -> str:
    """Return an opaque key without storing the repository location.

    Path-like values are local filesystem paths and are made absolute. Strings
    are stable caller-defined identities; pass canonical absolute paths for
    local repositories and exact backend identifiers for remote repositories.
    """

    identity = os.fspath(repository)
    if isinstance(repository, os.PathLike):
        identity = os.path.abspath(identity)
    return hashlib.sha256(os.fsencode(identity)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class OperationLedger:
    """SQLite lifecycle ledger with nonblocking, per-repository ``flock``."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        lock_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        self.database_path = Path(database_path).absolute()
        self.lock_directory = (
            Path(lock_directory).absolute()
            if lock_directory is not None
            else self.database_path.parent / f"{self.database_path.name}.locks"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    destructive INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER
                );
                CREATE INDEX IF NOT EXISTS operations_by_repository
                    ON operations(repository_id, status);
                CREATE TABLE IF NOT EXISTS operation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    exit_code INTEGER,
                    FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
                );
                """
            )

    def _lock(self, repository_id: str) -> int:
        if not re.fullmatch(r"[a-f0-9]{64}", repository_id):
            raise ValueError("repository_id must be created with repository_key()")
        lock_path = self.lock_directory / f"{repository_id}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RepositoryConflictError(
                    f"repository {repository_id} already has an active operation"
                ) from exc
            raise
        return fd

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        operation_id: str,
        repository_id: str,
        kind: OperationEventKind,
        occurred_at: str,
        exit_code: int | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO operation_events
               (operation_id, repository_id, kind, occurred_at, exit_code)
               VALUES (?, ?, ?, ?, ?)""",
            (operation_id, repository_id, kind.value, occurred_at, exit_code),
        )

    def begin(self, plan: OperationPlan) -> OperationLease:
        """Record a running operation and hold its repository lock.

        An orphaned ``running`` row is changed to ``uncertain`` and rejected.
        No operation is replayed until a caller invokes :meth:`reconcile`.
        """

        lock_fd = self._lock(plan.repository_id)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                pending = connection.execute(
                    """SELECT operation_id, status FROM operations
                       WHERE repository_id = ? AND status IN (?, ?)
                       ORDER BY created_at LIMIT 1""",
                    (
                        plan.repository_id,
                        OperationStatus.RUNNING.value,
                        OperationStatus.UNCERTAIN.value,
                    ),
                ).fetchone()
                if pending is not None:
                    if pending["status"] == OperationStatus.RUNNING.value:
                        occurred_at = _now()
                        connection.execute(
                            """UPDATE operations SET status = ?, finished_at = ?
                               WHERE operation_id = ?""",
                            (
                                OperationStatus.UNCERTAIN.value,
                                occurred_at,
                                pending["operation_id"],
                            ),
                        )
                        self._insert_event(
                            connection,
                            pending["operation_id"],
                            plan.repository_id,
                            OperationEventKind.UNCERTAIN,
                            occurred_at,
                        )
                    connection.commit()
                    raise UncertainOperationError(pending["operation_id"])
                if connection.execute(
                    "SELECT 1 FROM operations WHERE operation_id = ?",
                    (plan.operation_id,),
                ).fetchone():
                    connection.rollback()
                    raise ValueError(f"operation_id already exists: {plan.operation_id}")
                occurred_at = _now()
                connection.execute(
                    """INSERT INTO operations
                       (operation_id, repository_id, label, status, destructive,
                        created_at, started_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        plan.operation_id,
                        plan.repository_id,
                        plan.label,
                        OperationStatus.RUNNING.value,
                        int(plan.destructive),
                        occurred_at,
                        occurred_at,
                    ),
                )
                self._insert_event(
                    connection,
                    plan.operation_id,
                    plan.repository_id,
                    OperationEventKind.STARTED,
                    occurred_at,
                )
                connection.commit()
            return OperationLease(self, plan, lock_fd)
        except BaseException:
            self._unlock(lock_fd)
            raise

    def reconcile(
        self,
        repository_id: str,
        operation_id: str,
        outcome: ReconciliationOutcome,
    ) -> OperationEvent:
        """Explicitly resolve an uncertain or orphaned operation."""

        lock_fd = self._lock(repository_id)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT status FROM operations
                       WHERE operation_id = ? AND repository_id = ?""",
                    (operation_id, repository_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise KeyError(operation_id)
                if row["status"] not in (
                    OperationStatus.RUNNING.value,
                    OperationStatus.UNCERTAIN.value,
                ):
                    connection.rollback()
                    raise ValueError("only a running or uncertain operation can be reconciled")
                status = {
                    ReconciliationOutcome.SUCCEEDED: OperationStatus.RECONCILED_SUCCEEDED,
                    ReconciliationOutcome.FAILED: OperationStatus.RECONCILED_FAILED,
                    ReconciliationOutcome.ABANDONED: OperationStatus.RECONCILED_ABANDONED,
                }[outcome]
                kind = OperationEventKind(status.value)
                occurred_at = _now()
                connection.execute(
                    "UPDATE operations SET status = ?, finished_at = ? WHERE operation_id = ?",
                    (status.value, occurred_at, operation_id),
                )
                self._insert_event(
                    connection, operation_id, repository_id, kind, occurred_at
                )
                connection.commit()
            return OperationEvent(operation_id, repository_id, kind, occurred_at)
        finally:
            self._unlock(lock_fd)

    def events(
        self,
        operation_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = False,
    ) -> tuple[OperationEvent, ...]:
        """Read a bounded page of sanitized events in insertion order."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")

        order = "DESC" if newest_first else "ASC"
        with self._connect() as connection:
            if operation_id is None:
                rows = connection.execute(
                    f"""SELECT operation_id, repository_id, kind, occurred_at, exit_code
                       FROM operation_events ORDER BY event_id {order} LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""SELECT operation_id, repository_id, kind, occurred_at, exit_code
                       FROM operation_events WHERE operation_id = ? ORDER BY event_id {order}
                       LIMIT ? OFFSET ?""",
                    (operation_id, limit, offset),
                ).fetchall()
        return tuple(
            OperationEvent(
                row["operation_id"],
                row["repository_id"],
                OperationEventKind(row["kind"]),
                row["occurred_at"],
                row["exit_code"],
            )
            for row in rows
        )


class OperationLease:
    """Active ledger record and repository lock for one operation."""

    def __init__(self, ledger: OperationLedger, plan: OperationPlan, lock_fd: int) -> None:
        self._ledger = ledger
        self.plan = plan
        self._lock_fd = lock_fd
        self._finished = False

    def finish(self, status: OperationStatus, exit_code: int | None) -> None:
        if status not in (
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.TIMED_OUT,
            OperationStatus.CANCELLED,
        ):
            raise ValueError("finish requires a terminal process status")
        if self._finished:
            raise RuntimeError("operation lease is already finished")
        occurred_at = _now()
        kind = OperationEventKind(status.value)
        with self._ledger._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE operations SET status = ?, finished_at = ?, exit_code = ?
                   WHERE operation_id = ? AND status = ?""",
                (
                    status.value,
                    occurred_at,
                    exit_code,
                    self.plan.operation_id,
                    OperationStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("operation ledger row is no longer running")
            self._ledger._insert_event(
                connection,
                self.plan.operation_id,
                self.plan.repository_id,
                kind,
                occurred_at,
                exit_code,
            )
            connection.commit()
        self._finished = True

    def _mark_uncertain(self) -> None:
        occurred_at = _now()
        with self._ledger._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE operations SET status = ?, finished_at = ?
                   WHERE operation_id = ? AND status = ?""",
                (
                    OperationStatus.UNCERTAIN.value,
                    occurred_at,
                    self.plan.operation_id,
                    OperationStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount:
                self._ledger._insert_event(
                    connection,
                    self.plan.operation_id,
                    self.plan.repository_id,
                    OperationEventKind.UNCERTAIN,
                    occurred_at,
                )
            connection.commit()

    def close(self) -> None:
        if self._lock_fd < 0:
            return
        try:
            if not self._finished:
                self._mark_uncertain()
                self._finished = True
        finally:
            OperationLedger._unlock(self._lock_fd)
            self._lock_fd = -1

    def __enter__(self) -> OperationLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del exc_type, exc, traceback
        self.close()
        return False


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = bytearray()

    def append(self, data: bytes) -> None:
        if not self.limit:
            return
        self.value.extend(data)
        if len(self.value) > self.limit:
            del self.value[:-self.limit]


class _LineEmitter:
    """Split arbitrarily long lines into callback chunks of a fixed maximum."""

    def __init__(self, callback: Callable[[bytes], None] | None, max_line_bytes: int) -> None:
        self.callback = callback
        self.max_line_bytes = max_line_bytes
        self.pending = bytearray()

    def feed(self, data: bytes, *, final: bool = False) -> None:
        if self.callback is None:
            return
        self.pending.extend(data)
        while self.pending:
            newline = self.pending.find(b"\n")
            line_size = newline + 1 if newline >= 0 else None
            if line_size is not None and line_size <= self.max_line_bytes:
                chunk = bytes(self.pending[:line_size])
                del self.pending[:line_size]
                self.callback(chunk)
            elif len(self.pending) >= self.max_line_bytes:
                chunk = bytes(self.pending[: self.max_line_bytes])
                del self.pending[: self.max_line_bytes]
                self.callback(chunk)
            elif final:
                chunk = bytes(self.pending)
                self.pending.clear()
                self.callback(chunk)
            else:
                break


class OperationRunner:
    """Run a child process with bounded capture and streaming callbacks."""

    def __init__(
        self,
        *,
        max_buffer_bytes: int = 64 * 1024,
        max_line_bytes: int = 16 * 1024,
        read_chunk_bytes: int = 16 * 1024,
        terminate_grace_seconds: float = 0.5,
        cleanup_drain_seconds: float = 0.25,
        cleanup_drain_bytes: int = 256 * 1024,
    ) -> None:
        if min(max_buffer_bytes, max_line_bytes, read_chunk_bytes, cleanup_drain_bytes) < 0:
            raise ValueError("buffer, line, and read chunk sizes cannot be negative")
        if max_line_bytes == 0 or read_chunk_bytes == 0:
            raise ValueError("line and read chunk sizes must be positive")
        if terminate_grace_seconds < 0 or cleanup_drain_seconds < 0:
            raise ValueError("cleanup time limits cannot be negative")
        self.max_buffer_bytes = max_buffer_bytes
        self.max_line_bytes = max_line_bytes
        self.read_chunk_bytes = read_chunk_bytes
        self.terminate_grace_seconds = terminate_grace_seconds
        self.cleanup_drain_seconds = cleanup_drain_seconds
        self.cleanup_drain_bytes = cleanup_drain_bytes

    def run(
        self,
        plan: OperationPlan,
        *,
        on_stdout: Callable[[bytes], None] | None = None,
        on_stderr: Callable[[bytes], None] | None = None,
        cancel_event: Event | None = None,
    ) -> OperationRun:
        """Execute a plan and return bounded output tails and an explicit code."""

        started_at = _now()
        started = time.monotonic()
        stdout_tail = _TailBuffer(self.max_buffer_bytes)
        stderr_tail = _TailBuffer(self.max_buffer_bytes)
        if cancel_event is not None and cancel_event.is_set():
            return OperationRun(plan.operation_id, plan.repository_id, OperationStatus.CANCELLED,
                                None, started_at, _now(), time.monotonic() - started, b"", b"", 0, 0)
        popen_env = dict(plan.env) if plan.env is not None else None
        process = subprocess.Popen(
            plan.argv,
            cwd=os.fspath(plan.cwd) if plan.cwd is not None else None,
            env=popen_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        emitters: dict[int, _LineEmitter] = {}
        tails: dict[int, _TailBuffer] = {}
        counters = {"stdout": 0, "stderr": 0}
        stream_names: dict[int, str] = {}
        for name, stream, callback, tail in (
            ("stdout", process.stdout, on_stdout, stdout_tail),
            ("stderr", process.stderr, on_stderr, stderr_tail),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
            fd = stream.fileno()
            emitters[fd] = _LineEmitter(callback, self.max_line_bytes)
            tails[fd] = tail
            stream_names[fd] = name

        deadline = started + plan.timeout_seconds if plan.timeout_seconds is not None else None
        status: OperationStatus | None = None
        callback_error: BaseException | None = None
        try:
            while selector.get_map() or process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = OperationStatus.CANCELLED
                    self._terminate_group(process)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    status = OperationStatus.TIMED_OUT
                    self._terminate_group(process)
                    break
                wait_for = 0.05
                if deadline is not None:
                    wait_for = max(0.0, min(wait_for, deadline - time.monotonic()))
                for key, _ in selector.select(wait_for):
                    stream = key.fileobj
                    fd = stream.fileno()
                    data = os.read(fd, self.read_chunk_bytes)
                    if not data:
                        selector.unregister(stream)
                        emitters[fd].feed(b"", final=True)
                        stream.close()
                        continue
                    tails[fd].append(data)
                    counters[stream_names[fd]] += len(data)
                    emitters[fd].feed(data)
        except BaseException as exc:
            callback_error = exc
            self._terminate_group(process)
        finally:
            if process.poll() is None:
                self._terminate_group(process)
            try:
                exit_code = process.wait()
            finally:
                drain_deadline = time.monotonic() + self.cleanup_drain_seconds
                drain_budget = self.cleanup_drain_bytes
                for key in list(selector.get_map().values()):
                    stream = key.fileobj
                    fd = stream.fileno()
                    try:
                        if callback_error is None:
                            while (
                                drain_budget > 0
                                and time.monotonic() < drain_deadline
                            ):
                                data = os.read(fd, min(self.read_chunk_bytes, drain_budget))
                                if not data:
                                    break
                                drain_budget -= len(data)
                                tails[fd].append(data)
                                counters[stream_names[fd]] += len(data)
                                emitters[fd].feed(data)
                            emitters[fd].feed(b"", final=True)
                    except BlockingIOError:
                        pass
                    except BaseException as exc:
                        callback_error = callback_error or exc
                    finally:
                        try:
                            selector.unregister(stream)
                        except (KeyError, ValueError):
                            pass
                        stream.close()
                selector.close()
                process.stdout.close()
                process.stderr.close()
        if callback_error is not None:
            raise callback_error
        if status is None:
            status = OperationStatus.SUCCEEDED if exit_code == 0 else OperationStatus.FAILED
        finished = time.monotonic()
        return OperationRun(
            operation_id=plan.operation_id,
            repository_id=plan.repository_id,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=_now(),
            elapsed_seconds=finished - started,
            stdout_tail=bytes(stdout_tail.value),
            stderr_tail=bytes(stderr_tail.value),
            stdout_bytes=counters["stdout"],
            stderr_bytes=counters["stderr"],
        )

    def _terminate_group(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.poll() is None and self.terminate_grace_seconds:
            try:
                process.wait(timeout=self.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()


__all__ = [
    "OperationEvent",
    "OperationEventKind",
    "OperationLedger",
    "OperationLease",
    "OperationPlan",
    "OperationRun",
    "OperationRunner",
    "OperationStatus",
    "ReconciliationOutcome",
    "RepositoryConflictError",
    "UncertainOperationError",
    "repository_key",
]
