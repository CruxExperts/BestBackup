from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bbackup.operations import (
    OperationLedger,
    OperationPlan,
    OperationRunner,
    OperationStatus,
    ReconciliationOutcome,
    RepositoryConflictError,
    UncertainOperationError,
    repository_key,
)


def _plan(repository: Path, *, operation_id: str | None = None, **kwargs) -> OperationPlan:
    plan = OperationPlan.create(repository, "test-operation", (sys.executable, "-c", "pass"), **kwargs)
    if operation_id is None:
        return plan
    return OperationPlan(
        operation_id=operation_id,
        repository_id=plan.repository_id,
        label=plan.label,
        argv=plan.argv,
        cwd=plan.cwd,
        env=plan.env,
        timeout_seconds=plan.timeout_seconds,
        destructive=plan.destructive,
    )


def test_repository_key_keeps_remote_identity_independent_of_working_directory() -> None:
    first = repository_key("s3:s3.amazonaws.com/example-bucket")
    second = repository_key("s3:s3.amazonaws.com/example-bucket")

    assert first == second


def test_runner_times_out_child_and_returns_its_exit_code(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "repo", timeout_seconds=0.15)

    result = OperationRunner().run(
        OperationPlan.create(
            tmp_path / "repo",
            "slow-command",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout_seconds=0.15,
        )
    )

    assert result.status is OperationStatus.TIMED_OUT
    assert result.exit_code is not None
    assert result.elapsed_seconds < 5
    assert plan.repository_id == result.repository_id


def test_runner_cancels_child_when_event_is_set(tmp_path: Path) -> None:
    cancel_event = threading.Event()
    timer = threading.Timer(0.15, cancel_event.set)
    timer.start()
    try:
        result = OperationRunner().run(
            OperationPlan.create(
                tmp_path / "repo",
                "cancellable",
                (sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_seconds=10,
            ),
            cancel_event=cancel_event,
        )
    finally:
        timer.cancel()

    assert result.status is OperationStatus.CANCELLED
    assert result.exit_code is not None
    assert result.elapsed_seconds < 5


def test_runner_streams_giant_line_in_bounded_chunks_and_caps_tails(tmp_path: Path) -> None:
    output = b"z" * 300_000 + b"\n"
    received: list[bytes] = []
    code = "import os; os.write(1, b'z' * 300000 + b'\\n'); os.write(2, b'finished\\n')"
    result = OperationRunner(
        max_buffer_bytes=256,
        max_line_bytes=127,
        read_chunk_bytes=8192,
    ).run(
        OperationPlan.create(tmp_path / "repo", "large-output", (sys.executable, "-c", code)),
        on_stdout=received.append,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.stdout_bytes == len(output)
    assert b"".join(received) == output
    assert max(map(len, received)) <= 127
    assert len(result.stdout_tail) == 256
    assert result.stdout_tail == output[-256:]
    assert result.stderr_bytes == len(b"finished\n")


def test_runner_preserves_nonzero_exit_code_and_streams_stderr(tmp_path: Path) -> None:
    received: list[bytes] = []
    result = OperationRunner().run(
        OperationPlan.create(
            tmp_path / "repo",
            "failing-command",
            (sys.executable, "-c", "import sys; print('problem', file=sys.stderr); sys.exit(7)"),
        ),
        on_stderr=received.append,
    )

    assert result.status is OperationStatus.FAILED
    assert result.exit_code == 7
    assert b"".join(received) == b"problem\n"
    assert result.stderr_tail == b"problem\n"


def test_explicit_environment_does_not_inherit_parent_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BBACKUP_TEST_PARENT_SECRET", "must-not-be-inherited")
    result = OperationRunner().run(
        OperationPlan.create(
            tmp_path / "repo",
            "clean-environment",
            (
                sys.executable,
                "-c",
                "import os; print(os.getenv('BBACKUP_TEST_PARENT_SECRET', 'absent') + ':' + os.environ['BBACKUP_TEST_EXPLICIT'])",
            ),
            env={"BBACKUP_TEST_EXPLICIT": "present"},
        )
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.stdout_tail == b"absent:present\n"


def test_runner_bounds_cleanup_when_escaped_descendant_holds_pipe(tmp_path: Path) -> None:
    code = """
import os
pid = os.fork()
if pid == 0:
    os.setsid()
    while True:
        try:
            os.write(1, b'x' * 4096)
        except BrokenPipeError:
            os._exit(0)
else:
    os._exit(0)
"""
    result = OperationRunner(
        cleanup_drain_seconds=0.05,
        cleanup_drain_bytes=4096,
    ).run(
        OperationPlan.create(
            tmp_path / "repo",
            "escaped-writer",
            (sys.executable, "-c", code),
            timeout_seconds=0.15,
        )
    )

    assert result.status is OperationStatus.TIMED_OUT
    assert result.elapsed_seconds < 3


def test_ledger_serializes_processes_by_repository_with_flock(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    marker = tmp_path / "lease-ready"
    plan = _plan(tmp_path / "repo")
    child_script = """
import pathlib, sys, time
from bbackup.operations import OperationLedger, OperationPlan, OperationStatus
ledger = OperationLedger(sys.argv[1])
plan = OperationPlan('child-op', sys.argv[2], 'child', ('unused',))
with ledger.begin(plan) as lease:
    pathlib.Path(sys.argv[3]).write_text('ready')
    time.sleep(0.5)
    lease.finish(OperationStatus.SUCCEEDED, 0)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(database), plan.repository_id, str(marker)],
        cwd=Path(__file__).parents[1],
    )
    try:
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert marker.exists(), "child did not acquire its repository lease"
        with pytest.raises(RepositoryConflictError):
            OperationLedger(database).begin(_plan(tmp_path / "repo"))
    finally:
        child_code = child.wait(timeout=5)

    assert child_code == 0


def test_ledger_event_pages_are_bounded_and_validate_pagination(tmp_path: Path) -> None:
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    plan = _plan(tmp_path / "repo")
    with ledger.begin(plan) as lease:
        lease.finish(OperationStatus.SUCCEEDED, 0)

    all_events = ledger.events()
    assert len(all_events) == 2
    assert ledger.events(limit=1, offset=1) == (all_events[1],)
    assert ledger.events(plan.operation_id, limit=1)[0].kind.value == "started"
    with pytest.raises(ValueError):
        ledger.events(limit=1001)
    with pytest.raises(ValueError):
        ledger.events(offset=-1)


def test_interrupted_operation_requires_explicit_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    repository_id = repository_key(tmp_path / "repo")
    child_script = """
import os, sys
from bbackup.operations import OperationLedger, OperationPlan
ledger = OperationLedger(sys.argv[1])
plan = OperationPlan('crashed-op', sys.argv[2], 'unsafe-run', ('secret-command', 'secret-value'))
ledger.begin(plan)
os._exit(0)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_script, str(database), repository_id],
        cwd=Path(__file__).parents[1],
        check=False,
        timeout=5,
    )
    assert child.returncode == 0

    ledger = OperationLedger(database)
    with pytest.raises(UncertainOperationError) as error:
        ledger.begin(_plan(tmp_path / "repo"))
    assert error.value.operation_id == "crashed-op"
    assert [event.kind.value for event in ledger.events("crashed-op")] == ["started", "uncertain"]

    reconciled = ledger.reconcile(
        repository_id,
        "crashed-op",
        ReconciliationOutcome.FAILED,
    )
    assert reconciled.kind.value == "reconciled_failed"
    with ledger.begin(_plan(tmp_path / "repo")) as lease:
        lease.finish(OperationStatus.SUCCEEDED, 0)

    raw_records = sqlite3.connect(database).execute(
        "SELECT * FROM operations UNION ALL SELECT * FROM operations WHERE 0"
    ).fetchall()
    # The operation table schema has no command, output, environment, or path fields.
    assert len(raw_records[0]) == 9
    database_text = database.read_bytes()
    assert b"secret-command" not in database_text
    assert b"secret-value" not in database_text
    assert os.fsencode(str(tmp_path / "repo")) not in database_text


def test_pre_cancelled_command_never_spawns(tmp_path, monkeypatch):
    from threading import Event
    import subprocess
    cancellation = Event()
    cancellation.set()
    def forbidden(*args, **kwargs):
        raise AssertionError("cancelled command dispatched")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    plan = OperationPlan.create(tmp_path / "repo", "capture", ["never-run"])
    result = OperationRunner().run(plan, cancel_event=cancellation)
    assert result.status == OperationStatus.CANCELLED
    assert result.exit_code is None
