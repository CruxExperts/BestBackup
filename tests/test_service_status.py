from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

from bbackup.models import Job
from bbackup.operations import repository_key
from bbackup.service_status import _schedules, operational_status


class _Ledger:
    def __init__(self, summaries: tuple[dict[str, str | int | None], ...]):
        self._summaries = summaries

    def summary(self) -> tuple[dict[str, str | int | None], ...]:
        return self._summaries


class _StatusService:
    def __init__(self, state, local_repository, cloud_repository, summaries):
        local_repository.mkdir()
        state.mkdir(parents=True)
        self.state = state
        self.receipts_path = state / "snapshots.sqlite3"
        self.config = SimpleNamespace(
            repositories=(
                SimpleNamespace(name="local", role="local"),
                SimpleNamespace(name="cloud", role="replica"),
            ),
            jobs=(),
        )
        self.bindings = SimpleNamespace(
            repositories={
                "local": SimpleNamespace(repository=str(local_repository)),
                "cloud": SimpleNamespace(repository=cloud_repository),
            }
        )
        self.ledger = _Ledger(summaries)
        with sqlite3.connect(self.receipts_path) as db:
            db.execute(
                """CREATE TABLE checkpoints (
                    repository TEXT,
                    repository_identity TEXT,
                    snapshot TEXT,
                    operation TEXT PRIMARY KEY,
                    manifest_sha256 TEXT,
                    captured_at TEXT,
                    expires_at TEXT
                )"""
            )

    def _identity(self, name: str) -> str:
        repository = self.bindings.repositories[name].repository
        return str(repository)

    def _receipts(self) -> sqlite3.Connection:
        return sqlite3.connect(self.receipts_path)


def _summary(repository_id: str, label: str, status: str, finished: str, count: int = 1):
    return {
        "repository_id": repository_id,
        "label": label,
        "status": status,
        "count": count,
        "last_started_at": finished,
        "last_finished_at": finished,
    }


def test_operational_status_filters_current_repositories_and_reports_real_capacity(
    tmp_path,
):
    local_path = tmp_path / "local-repository"
    cloud_path = "s3:https://storage.example/bucket/bbackup"
    local_id = repository_key(str(local_path))
    cloud_id = repository_key(cloud_path)
    summaries = (
        _summary(local_id, "capture", "succeeded", "2026-10-01T00:00:00+00:00"),
        _summary(local_id, "capture", "failed", "2026-10-03T00:00:00+00:00"),
        _summary("stale-repository", "capture", "succeeded", "2026-10-05T00:00:00+00:00"),
        _summary(local_id, "copy", "succeeded", "2026-10-02T00:00:00+00:00"),
        _summary(cloud_id, "copy", "succeeded", "2026-10-04T00:00:00+00:00"),
        _summary(local_id, "check", "succeeded", "2026-10-03T00:00:00+00:00"),
        _summary(local_id, "restore", "succeeded", "2026-10-03T01:00:00+00:00"),
        _summary(local_id, "database-restore", "succeeded", "2026-10-03T02:00:00+00:00"),
        _summary(
            "docker:connection:daemon:container",
            "docker-restore",
            "succeeded",
            "2026-10-03T03:00:00+00:00",
        ),
    )
    service = _StatusService(
        tmp_path / "state", local_path, cloud_path, summaries
    )

    now = datetime.now(timezone.utc)
    current_checkpoint = (
        "cloud",
        cloud_id,
        "a" * 64,
        "current-checkpoint",
        "b" * 64,
        (now - timedelta(days=2)).isoformat(),
        (now - timedelta(days=1)).isoformat(),
    )
    rebound_checkpoint = (
        "cloud",
        repository_key("s3:https://old.example/bucket/bbackup"),
        "c" * 64,
        "old-endpoint-checkpoint",
        "d" * 64,
        now.isoformat(),
        (now + timedelta(days=30)).isoformat(),
    )
    with service._receipts() as db:
        db.executemany(
            """INSERT INTO checkpoints
               (repository, repository_identity, snapshot, operation,
                manifest_sha256, captured_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (current_checkpoint, rebound_checkpoint),
        )

    facts = operational_status(service)

    assert facts["last_local_capture"] == "2026-10-01T00:00:00+00:00"
    assert facts["last_independent_copy"] == "2026-10-04T00:00:00+00:00"
    assert facts["last_cloud_copy"] == "2026-10-04T00:00:00+00:00"
    assert facts["last_repository_check"] == "2026-10-03T00:00:00+00:00"
    assert facts["last_verified_file_restore"] == "2026-10-03T01:00:00+00:00"
    assert facts["last_native_database_restore"] == "2026-10-03T02:00:00+00:00"
    assert facts["last_container_restore"] == "2026-10-03T03:00:00+00:00"
    assert facts["operation_counts"] == {"succeeded": 8, "failed": 1}
    local, cloud = facts["repositories"]
    assert local["free_bytes"] is not None and local["total_bytes"] is not None
    assert local["capacity_source"] == "repository filesystem"
    assert cloud["free_bytes"] is None and cloud["total_bytes"] is None
    assert cloud["capacity_source"] == "remote capacity unavailable"
    checkpoint = facts["last_checkpoint"]
    assert checkpoint is not None
    assert checkpoint["snapshot_id"] == "a" * 64
    assert checkpoint["expiry_state"] == "expired"
    assert facts["protection_status"].startswith("not qualified;")


def test_rebound_checkpoint_is_not_reported_for_current_repository(tmp_path):
    local_path = tmp_path / "local-repository"
    cloud_path = "s3:https://new.example/bucket/bbackup"
    service = _StatusService(tmp_path / "state", local_path, cloud_path, ())

    with service._receipts() as db:
        db.execute(
            """INSERT INTO checkpoints
               (repository, repository_identity, snapshot, operation,
                manifest_sha256, captured_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "cloud",
                repository_key("s3:https://old.example/bucket/bbackup"),
                "a" * 64,
                "old-endpoint-checkpoint",
                "b" * 64,
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            ),
        )

    assert operational_status(service)["last_checkpoint"] is None


def test_schedules_require_loaded_active_systemd_timer_with_next_run(monkeypatch):
    job = Job(name="daily", repository="local", sources=("files",), schedule="daily")
    output = "\n\n".join(
        (
            "Id=bbackup-daily.timer\nLoadState=loaded\nActiveState=active\n"
            "NextElapseUSecRealtime=2026-10-16 00:00:00 UTC",
            "Id=bbackup@daily.timer\nLoadState=loaded\nActiveState=inactive\n"
            "NextElapseUSecRealtime=2026-10-17 00:00:00 UTC",
        )
    )
    monkeypatch.setattr(
        "bbackup.service_status.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(stdout=output if "--user" in command else ""),
    )

    schedule, = _schedules((job,))

    assert schedule["active_timer"] == "user: bbackup-daily.timer"
    assert schedule["next_run"] == "2026-10-16 00:00:00 UTC"


def test_configured_schedule_without_active_timer_has_no_next_run(monkeypatch):
    job = Job(name="daily", repository="local", sources=("files",), schedule="daily")
    output = "Id=bbackup-daily.timer\nLoadState=not-found\nActiveState=inactive\nNextElapseUSecRealtime=0"
    monkeypatch.setattr(
        "bbackup.service_status.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(stdout=output if "--user" in command else ""),
    )

    schedule, = _schedules((job,))

    assert schedule["active_timer"] is None
    assert schedule["next_run"] is None


def test_schedules_detect_duplicate_user_and_system_timers(monkeypatch):
    job = Job(name="daily", repository="local", sources=("files",), schedule="daily")
    output = ("Id=bbackup-daily.timer\nLoadState=loaded\nActiveState=active\n"
              "NextElapseUSecRealtime=2026-10-16 00:00:00 UTC")
    monkeypatch.setattr("bbackup.service_status.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=output))
    schedule, = _schedules((job,))
    assert schedule["active_timer"] == "conflicting timers"
    assert schedule["next_run"] is None


def test_remote_primary_capture_does_not_report_local_capture(tmp_path):
    cloud = "s3:https://storage.example/bucket/machine"
    service = _StatusService(tmp_path / "state", tmp_path / "local", cloud, (
        _summary(repository_key(cloud), "capture", "succeeded", "2026-10-20T12:00:00+00:00"),
    ))
    facts = operational_status(service)
    assert facts["last_local_capture"] is None
    assert facts["last_remote_primary_capture"] == "2026-10-20T12:00:00+00:00"
