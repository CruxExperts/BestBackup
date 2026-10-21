import pytest

from bbackup.models import ResourceBudget
from bbackup.resource_limits import ResourceLimitError, ResourceMonitor


def test_dedicated_private_mount_and_observed_reserve(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    state = tmp_path / "state"
    staging.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    volumes = {staging: (1000, 700), state: (10000, 9000)}
    monkeypatch.setattr("bbackup.resource_limits.os.path.ismount", lambda path: path == staging)
    monkeypatch.setattr("bbackup.resource_limits._volume", lambda path: volumes[path])
    monitor = ResourceMonitor(staging, state, ResourceBudget(1000, 100))
    monitor.check(500)
    assert monitor.result()["staging_capacity_bounded"] is True
    assert monitor.result()["free_space_reserve_hard_guaranteed"] is False
    assert monitor.result()["usage_attributed_to_this_operation"] is False
    with pytest.raises(ResourceLimitError, match="reserve"):
        monitor.check(601)
    assert monitor.result()["staging_minimum_free_bytes"] == 700
    volumes[state] = (10000, 50)
    with pytest.raises(ResourceLimitError, match="reserve"):
        monitor.check()
    monitor.close()


def test_remote_staging_rejects_shared_or_oversized_mount(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    monkeypatch.setattr("bbackup.resource_limits._volume", lambda path: (2000, 1900))
    with pytest.raises(ResourceLimitError, match="dedicated"):
        ResourceMonitor(staging, tmp_path, ResourceBudget(1000, 100))
    monkeypatch.setattr("bbackup.resource_limits.os.path.ismount", lambda path: path == staging)
    with pytest.raises(ResourceLimitError, match="capacity"):
        ResourceMonitor(staging, tmp_path, ResourceBudget(1000, 100))
    (staging / "unowned").write_text("do not remove")
    with pytest.raises(ResourceLimitError, match="empty"):
        ResourceMonitor(staging, tmp_path, ResourceBudget(3000, 100))
    assert (staging / "unowned").exists()


def test_staging_lease_is_nonblocking_across_independent_state_roots(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    first_state = tmp_path / "first-state"
    second_state = tmp_path / "second-state"
    for path in (staging, first_state, second_state):
        path.mkdir(mode=0o700)
    monkeypatch.setattr("bbackup.resource_limits.os.path.ismount", lambda path: path == staging)
    monkeypatch.setattr("bbackup.resource_limits._volume", lambda _path: (1000, 900))
    with ResourceMonitor(staging, first_state, ResourceBudget(1000, 100)) as first:
        lock = staging / ".bbackup-staging.lock"
        original_inode = lock.stat().st_ino
        with pytest.raises(ResourceLimitError, match="busy"):
            ResourceMonitor(staging, second_state, ResourceBudget(1000, 100))
        first.check()
        assert lock.stat().st_ino == original_inode
    with ResourceMonitor(staging, second_state, ResourceBudget(1000, 100)) as second:
        second.check()
    assert lock.stat().st_ino == original_inode
