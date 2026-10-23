"""Focused Restic repository lock inspection and stale-unlock regressions."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import time

import pytest

from bbackup.operations import OperationStatus, UncertainOperationError
from bbackup.service import BackupService, ServiceError
from tests import test_service as service_fixtures

service = service_fixtures.service


HOST = "fixture-host"
HOST_DIGEST = "b" * 64
LOCK_ID = "a" * 64


def _lock_payload(*, hostname=HOST, uid=None, pid=2**31 - 1):
    return json.dumps({
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "exclusive": False,
        "hostname": hostname,
        "username": "fixture-user",
        "pid": pid,
        "uid": os.geteuid() if uid is None else uid,
        "gid": os.getegid(),
    }, separators=(",", ":")).encode()


def _fake_restic(service, monkeypatch, *, ids=None, payloads=None, list_responses=None,
                 unlock_status=None, postflight_response=None,
                 retain_locks_after_unlock=False):
    state = {
        "ids": [LOCK_ID] if ids is None else list(ids),
        "payloads": {LOCK_ID: _lock_payload()} if payloads is None else dict(payloads),
        "list_responses": list(list_responses or []),
        "unlock_args": [],
        "plans": [],
        "unlock_status": unlock_status,
        "postflight_response": postflight_response,
        "unlocked": False,
        "retain_locks_after_unlock": retain_locks_after_unlock,
    }

    def fake_run(plan, **_kwargs):
        state["plans"].append(plan)
        argv = list(plan.argv)
        if "list" in argv and "locks" in argv:
            if state["list_responses"]:
                output = state["list_responses"].pop(0)
            elif state["unlocked"] and state["postflight_response"] is not None:
                output = state["postflight_response"]
            else:
                output = "\n".join(state["ids"]).encode()
                if state["ids"]:
                    output += b"\n"
            return service_fixtures._run(plan, stdout=output)
        if "cat" in argv and "lock" in argv:
            lock_id = argv[-1]
            return service_fixtures._run(plan, stdout=state["payloads"].get(lock_id, b""))
        if argv[-1:] == ["unlock"]:
            state["unlock_args"].append(argv)
            if state["unlock_status"] is not None:
                return service_fixtures._run(plan, status=state["unlock_status"], exit_code=-9)
            state["unlocked"] = True
            if not state["retain_locks_after_unlock"]:
                state["ids"] = []
                state["payloads"] = {}
            return service_fixtures._run(plan)
        pytest.fail(f"unexpected Restic command: {argv!r}")

    monkeypatch.setattr(service.runner, "run", fake_run)
    monkeypatch.setattr("bbackup.service.socket.gethostname", lambda: HOST)
    monkeypatch.setattr("bbackup.service.machine_identity", lambda: (HOST_DIGEST, os.geteuid()))
    monkeypatch.setattr(service, "_restic_pid_state", lambda _pid: "absent")
    return state


def test_reviewed_unlock_reinspects_under_lease_and_uses_normal_restic_unlock(
        service, monkeypatch):
    state = _fake_restic(service, monkeypatch)
    review = service.repository_locks("local")
    assert review["unlock_eligible"] is True
    assert review["locks"][0]["id"] == LOCK_ID
    assert review["locks"][0]["pid_state"] == "absent"
    assert state["unlock_args"] == []

    result = service.repository_unlock_stale(
        "local", review["plan_token"], True, quiescent_window_confirmed=True,
    )
    assert result["unlocked_lock_ids"] == [LOCK_ID]
    assert result["replayed"] is False
    assert state["unlock_args"]
    assert "--remove-all" not in state["unlock_args"][0]
    assert service.ledger.events(result["operation_id"])[-1].kind.value == "succeeded"


@pytest.mark.parametrize("reason", ["foreign_host", "foreign_uid", "live_pid"])
def test_foreign_or_live_lock_never_reaches_restic_unlock(service, monkeypatch, reason):
    payload = _lock_payload(
        hostname="another-host" if reason == "foreign_host" else HOST,
        uid=os.geteuid() + 1 if reason == "foreign_uid" else os.geteuid(),
    )
    state = _fake_restic(service, monkeypatch, payloads={LOCK_ID: payload})
    if reason == "live_pid":
        monkeypatch.setattr(service, "_restic_pid_state", lambda _pid: "present")
    review = service.repository_locks("local")
    assert review["unlock_eligible"] is False
    with pytest.raises(ServiceError):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"] == []


def test_mixed_local_and_foreign_inventory_never_unlocks_any_lock(service, monkeypatch):
    second_id = "c" * 64
    state = _fake_restic(
        service, monkeypatch,
        ids=[LOCK_ID, second_id],
        payloads={LOCK_ID: _lock_payload(), second_id: _lock_payload(hostname="another-host")},
    )
    review = service.repository_locks("local")
    assert review["unlock_eligible"] is False
    with pytest.raises(ServiceError):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"] == []


def test_changed_payload_invalidates_lock_review(service, monkeypatch):
    state = _fake_restic(service, monkeypatch)
    review = service.repository_locks("local")
    state["payloads"][LOCK_ID] = _lock_payload(pid=2**31 - 2)
    with pytest.raises(ServiceError, match="review is stale"):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"] == []


def test_changed_pid_liveness_invalidates_lock_review(service, monkeypatch):
    state = _fake_restic(service, monkeypatch)
    review = service.repository_locks("local")
    monkeypatch.setattr(service, "_restic_pid_state", lambda _pid: "present")
    with pytest.raises(ServiceError, match="review is stale"):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"] == []


@pytest.mark.parametrize("bad_inventory", [
    [b"not-a-lock-id\n", b"not-a-lock-id\n"],
    [b"a" * (64 * 1024 + 1), b"a" * (64 * 1024 + 1)],
    [b"a" * 64 + b"\n", b""],
])
def test_malformed_truncated_or_changed_lock_inventory_never_unlocks(
        service, monkeypatch, bad_inventory):
    state = _fake_restic(service, monkeypatch, list_responses=bad_inventory)
    with pytest.raises(ServiceError):
        service.repository_locks("local")
    assert state["unlock_args"] == []


def test_excessive_lock_inventory_fails_closed(service, monkeypatch):
    ids = [f"{index:064x}" for index in range(129)]
    state = _fake_restic(service, monkeypatch, ids=ids, payloads={})
    with pytest.raises(ServiceError, match="excessive"):
        service.repository_locks("local")
    assert state["unlock_args"] == []


def test_malformed_lock_payload_fails_closed(service, monkeypatch):
    state = _fake_restic(service, monkeypatch, payloads={LOCK_ID: b'{"pid":1}'})
    with pytest.raises(ServiceError, match="payload is malformed"):
        service.repository_locks("local")
    assert state["unlock_args"] == []


def test_restic_lock_json_omitted_zero_uid_and_gid_are_normalized():
    payload = json.loads(_lock_payload())
    payload.pop("uid")
    payload.pop("gid")
    normalized = BackupService._parse_restic_lock_payload(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    assert normalized["uid"] == 0
    assert normalized["gid"] == 0
    payload["pid"] = 2**31
    with pytest.raises(ServiceError, match="payload is malformed"):
        BackupService._parse_restic_lock_payload(
            json.dumps(payload, separators=(",", ":")).encode()
        )


def test_restic_lock_id_inventory_accepts_empty_json_and_plain_id_lines():
    assert BackupService._restic_lock_ids(b"[]") == []
    assert BackupService._restic_lock_ids(f"{LOCK_ID}\n".encode()) == [LOCK_ID]
    assert BackupService._restic_lock_ids(json.dumps([LOCK_ID]).encode()) == [LOCK_ID]


def test_unlock_requires_both_explicit_approvals(service, monkeypatch):
    state = _fake_restic(service, monkeypatch)
    review = service.repository_locks("local")
    for approved, quiet in ((False, True), (True, False)):
        with pytest.raises(ServiceError, match="explicit approval"):
            service.repository_unlock_stale(
                "local", review["plan_token"], approved,
                quiescent_window_confirmed=quiet,
            )
    assert state["unlock_args"] == []


def test_interrupted_unlock_keeps_repository_behind_uncertain_barrier(service, monkeypatch):
    state = _fake_restic(service, monkeypatch, unlock_status=OperationStatus.TIMED_OUT)
    review = service.repository_locks("local")
    with pytest.raises(ServiceError, match="outcome is uncertain"):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"]
    with pytest.raises(UncertainOperationError):
        service.repository_locks("local")


@pytest.mark.parametrize("postflight", ["locks_remain", "malformed", "truncated"])
def test_postflight_unlock_failure_keeps_uncertain_barrier(service, monkeypatch, postflight):
    state = _fake_restic(
        service, monkeypatch,
        postflight_response=(b"not-a-lock-id\n" if postflight == "malformed"
                             else b"x" * (64 * 1024 + 1) if postflight == "truncated"
                             else None),
        retain_locks_after_unlock=postflight == "locks_remain",
    )
    review = service.repository_locks("local")
    with pytest.raises(ServiceError):
        service.repository_unlock_stale(
            "local", review["plan_token"], True, quiescent_window_confirmed=True,
        )
    assert state["unlock_args"]
    with pytest.raises(UncertainOperationError):
        service.repository_locks("local")


def test_lock_queries_disable_cache_when_home_and_xdg_are_missing(
        service, monkeypatch):
    state = _fake_restic(service, monkeypatch, ids=[], payloads={})
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("RESTIC_CACHE_DIR", raising=False)
    service.repository_locks("local")
    assert state["plans"]
    assert all("--no-cache" in plan.argv for plan in state["plans"])


def test_b2_lock_queries_disable_cache_even_with_home(service, monkeypatch):
    state = _fake_restic(service, monkeypatch, ids=[], payloads={})
    repositories = dict(service.bindings.repositories)
    repositories["local"] = replace(
        repositories["local"],
        repository="s3:https://s3.us-west-004.backblazeb2.com/bucket/repository",
    )
    service.bindings = replace(service.bindings, repositories=repositories)
    service.repository_locks("local")
    assert state["plans"]
    assert all("--no-cache" in plan.argv for plan in state["plans"])


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_disposable_local_restic_unlocks_only_a_reviewed_dead_lock(service):
    service.initialize("local")
    job = service.config.jobs[0]
    service.config = replace(
        service.config,
        jobs=(replace(job, retention=replace(
            job.retention, local_last=12, local_daily=14, local_weekly=8,
        )),),
    )
    service.capture("daily")
    retention_review = service.retention_plan("daily")
    assert retention_review["total_snapshots"] == 1
    assert retention_review["target_snapshot_ids"] == []
    assert retention_review["policy"] == {
        "local_last": 12, "local_daily": 14, "local_weekly": 8,
        "group_by": "host", "job_tag": "bbackup-job=daily",
    }
    engine = service._engine("local")
    environment = service._operation_environment("local")

    def snapshot_ids():
        result = subprocess.run(
            [*engine.base_args(), "snapshots"], check=True, capture_output=True,
            env=environment, timeout=15,
        )
        return sorted(item["id"] for item in json.loads(result.stdout))

    snapshots_before = snapshot_ids()
    process = subprocess.Popen(
        [*engine.base_args(), "backup", "--stdin", "--stdin-filename", "held-open.bin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 15
        live_review = None
        while time.monotonic() < deadline and process.poll() is None:
            live_review = service.repository_locks("local")
            if live_review["lock_count"]:
                break
            time.sleep(0.05)
        assert process.poll() is None
        assert live_review is not None and live_review["lock_count"] > 0
        assert live_review["unlock_eligible"] is False
        with pytest.raises(ServiceError):
            service.repository_unlock_stale(
                "local", live_review["plan_token"], True,
                quiescent_window_confirmed=True,
            )

        process.kill()
        process.communicate(timeout=10)
        stale_review = service.repository_locks("local")
        assert stale_review["lock_count"] > 0
        assert stale_review["unlock_eligible"] is True
        result = service.repository_unlock_stale(
            "local", stale_review["plan_token"], True,
            quiescent_window_confirmed=True,
        )
        assert result["unlocked_count"] == stale_review["lock_count"]
        assert service.repository_locks("local")["lock_count"] == 0
        assert snapshot_ids() == snapshots_before
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
