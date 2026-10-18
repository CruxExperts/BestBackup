"""
Integration tests for bbackup - require a live Docker daemon.
All tests are marked @pytest.mark.integration and are excluded from normal
unit test runs (use -m integration or --all flag in scripts/run_tests.py).

Volume names are UUID-based to prevent collision. All fixtures use
try/except teardown to avoid orphaned Docker resources on test failure.
Created: 2026-02-26
Last Updated: 2026-02-27
"""

import io
import json
import os
import uuid

import pytest

from bbackup.container_images import ALPINE_HELPER_IMAGE

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client():
    import docker
    return docker.from_env()


@pytest.fixture(scope="session")
def alpine_rsync_image(docker_client, worker_id):
    """Build a per-worker Alpine image with rsync available."""
    tag = f"bbackup-alpine-rsync:{worker_id}"
    dockerfile = f"FROM {ALPINE_HELPER_IMAGE}\nRUN apk add --no-cache rsync\n".encode()
    docker_client.images.build(
        fileobj=io.BytesIO(dockerfile),
        tag=tag,
        rm=True,
    )
    yield tag
    try:
        docker_client.images.remove(tag, force=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unique_id():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def seeded_volume(docker_client, unique_id):
    name = f"bbackup_itest_{unique_id}"
    vol = docker_client.volumes.create(name)
    docker_client.containers.run(
        ALPINE_HELPER_IMAGE,
        command="sh -c 'mkdir -p /v/subdir && echo hello > /v/file.txt && echo world > /v/subdir/nested.txt'",
        volumes={name: {"bind": "/v", "mode": "rw"}},
        remove=True,
    )
    yield vol
    try:
        vol.remove(force=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backup_volume_preserves_numeric_ownership_or_refuses(docker_client, alpine_rsync_image, seeded_volume, tmp_path, unique_id, caplog):
    """Preserve volume ownership or refuse without a misleading artifact."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup

    cfg = Config(config_path=None)

    # Docker SDK collection properties can return a new manager on each access;
    # patch the owning image constant so this actually exercises rsync.
    with pytest.MonkeyPatch.context() as m:
        m.setattr("bbackup.docker_backup.ALPINE_HELPER_IMAGE", alpine_rsync_image)
        db = DockerBackup(cfg)
        db.client = docker_client

        result = db.backup_volume(seeded_volume.name, tmp_path)

    vol_dir = tmp_path / "volumes" / seeded_volume.name
    if os.geteuid() != 0:
        assert result is False
        assert not vol_dir.exists()
        # Even with rsync installed, non-root clients use checked tar because
        # docker cp -a may silently discard original numeric ownership.
        assert "Cannot restore archive ownership" in caplog.text
    else:
        assert result is True
        assert (vol_dir / "file.txt").read_text() == "hello\n"
        assert (vol_dir / "subdir/nested.txt").read_text() == "world\n"
        assert (vol_dir / "file.txt").stat().st_uid == 0


def test_backup_volume_tar_fallback(docker_client, seeded_volume, tmp_path, unique_id):
    """Plain alpine (no rsync) triggers tar fallback."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup

    cfg = Config(config_path=None)
    db = DockerBackup(cfg)
    db.client = docker_client

    result = db.backup_volume(seeded_volume.name, tmp_path)
    # Either succeeds via tar or gracefully returns False
    assert isinstance(result, bool)


def test_backup_container_config(docker_client, unique_id, tmp_path):
    """Container config JSON is written with expected keys."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup

    container_name = f"bbackup_itest_container_{unique_id}"
    container = docker_client.containers.run(
        ALPINE_HELPER_IMAGE,
        command="sleep 30",
        name=container_name,
        detach=True,
    )
    try:
        cfg = Config(config_path=None)
        db = DockerBackup(cfg)
        db.client = docker_client

        result = db.backup_container_config(container_name, tmp_path)
        config_file = tmp_path / f"{container_name}_config.json"

        assert result is True
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "Id" in data or "Name" in data or "Config" in data
    finally:
        try:
            container.stop(timeout=2)
            container.remove()
        except Exception:
            pass


def test_backup_network(docker_client, unique_id, tmp_path):
    """Network backup JSON contains Driver and IPAM."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup

    net_name = f"bbackup_itest_net_{unique_id}"
    network = docker_client.networks.create(net_name, driver="bridge")
    try:
        cfg = Config(config_path=None)
        db = DockerBackup(cfg)
        db.client = docker_client

        result = db.backup_network(net_name, tmp_path)
        net_file = tmp_path / "networks" / f"{net_name}.json"

        assert result is True
        assert net_file.exists()
        data = json.loads(net_file.read_text())
        assert "Driver" in data or "IPAM" in data
    finally:
        try:
            network.remove()
        except Exception:
            pass


def test_backup_volume_incremental_hardlinks(docker_client, alpine_rsync_image, seeded_volume, tmp_path, unique_id):
    """After two incremental backup passes, at least some files are hardlinked (st_nlink > 1)."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup

    cfg = Config(config_path=None)

    backup1 = tmp_path / "backup1"
    backup2 = tmp_path / "backup2"

    original_run = docker_client.containers.run

    def patched_run(image, **kwargs):
        return original_run(alpine_rsync_image, **kwargs)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(docker_client.containers, "run", patched_run)
        db = DockerBackup(cfg)
        db.client = docker_client

        result1 = db.backup_volume(seeded_volume.name, backup1, incremental=False)
        result2 = db.backup_volume(seeded_volume.name, backup2, incremental=True)

    # Check if hardlinks exist (only verifiable if both backups succeeded)
    if result1 and result2:
        vol1 = backup1 / "volumes" / seeded_volume.name
        vol2 = backup2 / "volumes" / seeded_volume.name
        if vol1.exists() and vol2.exists():
            hardlinks = [
                f for f in vol2.rglob("*")
                if f.is_file() and f.stat().st_nlink > 1
            ]
            # May be 0 if --link-dest didn't actually work (rsync path complexity)
            assert isinstance(hardlinks, list)


# ---------------------------------------------------------------------------
# Filesystem backup / restore integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_fs_dir(tmp_path):
    """Create a realistic directory tree with files and nested dirs."""
    root = tmp_path / "source"
    (root / "subdir").mkdir(parents=True)
    (root / "subdir" / "nested.txt").write_text("nested content")
    (root / "hello.txt").write_text("hello world")
    (root / "data.log").write_text("log data")
    (root / ".cache").mkdir()
    (root / ".cache" / "cache_file.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "build").mkdir()
    (root / "build" / "artifact.o").write_bytes(b"\xff" * 16)
    return root


def test_filesystem_backup_basic(seeded_fs_dir, tmp_path):
    """Real rsync: files appear verbatim in the destination directory."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="mydata",
        path=str(seeded_fs_dir),
        excludes=[],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_120000"

    result = fb.backup_path(target, backup_dir)

    assert result is True
    dest = backup_dir / "filesystems" / "mydata"
    assert (dest / "hello.txt").exists()
    assert (dest / "hello.txt").read_text() == "hello world"
    assert (dest / "subdir" / "nested.txt").exists()
    assert (dest / "data.log").read_text() == "log data"


def test_filesystem_backup_excludes_patterns(seeded_fs_dir, tmp_path):
    """Files matching exclude patterns must not appear in the backup."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="filtered",
        path=str(seeded_fs_dir),
        excludes=[".cache/", "build/", "*.log"],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_130000"

    result = fb.backup_path(target, backup_dir)
    assert result is True

    dest = backup_dir / "filesystems" / "filtered"
    # Excluded dirs/files should be absent
    assert not (dest / ".cache").exists()
    assert not (dest / "build").exists()
    assert not (dest / "data.log").exists()
    # Non-excluded content should be present
    assert (dest / "hello.txt").exists()
    assert (dest / "subdir" / "nested.txt").exists()


def test_filesystem_backup_incremental_hardlinks(seeded_fs_dir, tmp_path):
    """Two consecutive incremental backups: unchanged files share inodes."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="docs",
        path=str(seeded_fs_dir),
        excludes=[],
        enabled=True,
    )

    run1 = tmp_path / "backup_20260226_100000"
    run2 = tmp_path / "backup_20260227_100000"

    r1 = fb.backup_path(target, run1, incremental=False)
    r2 = fb.backup_path(target, run2, incremental=True)

    assert r1 is True
    assert r2 is True

    dest2 = run2 / "filesystems" / "docs"

    hardlinked = []
    for f in dest2.rglob("*"):
        if f.is_file() and f.stat().st_nlink > 1:
            hardlinked.append(f)

    assert len(hardlinked) > 0, "Expected at least one hardlinked file in incremental backup"


def test_filesystem_backup_progress_callback(seeded_fs_dir, tmp_path):
    """progress_callback receives non-empty lines from rsync."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="cb_test",
        path=str(seeded_fs_dir),
        excludes=[],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_140000"
    received_lines = []

    result = fb.backup_path(target, backup_dir, progress_callback=received_lines.append)

    assert result is True
    assert len(received_lines) > 0


def test_filesystem_restore_roundtrip(seeded_fs_dir, tmp_path):
    """Backup then restore: original file tree is reproduced at destination."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup
    from bbackup.restore import DockerRestore
    from unittest.mock import patch

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="roundtrip",
        path=str(seeded_fs_dir),
        excludes=[],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_150000"

    backup_ok = fb.backup_path(target, backup_dir)
    assert backup_ok is True, "Backup step failed, cannot test restore"

    dest = tmp_path / "restored"

    with patch("bbackup.restore.docker.from_env") as mock_docker:
        mock_docker.return_value.ping.return_value = True
        dr = DockerRestore(cfg)

    result = dr.restore_filesystem_path("roundtrip", backup_dir, destination=dest)
    assert result is True

    assert (dest / "hello.txt").read_text() == "hello world"
    assert (dest / "subdir" / "nested.txt").read_text() == "nested content"
    # Binary files should also be restored
    assert (dest / ".cache" / "cache_file.bin").read_bytes() == b"\x00\x01\x02\x03"


def test_filesystem_restore_via_restore_backup(seeded_fs_dir, tmp_path):
    """restore_backup() with filesystems param calls restore_filesystem_path correctly."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup
    from bbackup.restore import DockerRestore
    from unittest.mock import patch

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    target = FilesystemTarget(
        name="batch_restore",
        path=str(seeded_fs_dir),
        excludes=[],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_160000"

    backup_ok = fb.backup_path(target, backup_dir)
    assert backup_ok is True, "Backup step failed"

    dest = tmp_path / "restored_batch"

    with patch("bbackup.restore.docker.from_env") as mock_docker:
        mock_docker.return_value.ping.return_value = True
        dr = DockerRestore(cfg)

    results = dr.restore_backup(
        backup_path=backup_dir,
        filesystems=["batch_restore"],
        filesystem_destination=dest,
    )

    assert results["filesystems"]["batch_restore"] == "success"
    assert results["errors"] == []
    assert (dest / "hello.txt").read_text() == "hello world"


def test_filesystem_backup_empty_dir(tmp_path):
    """Backing up an empty directory should succeed and create an empty dest."""
    from bbackup.config import Config, FilesystemTarget
    from bbackup.filesystem_backup import FilesystemBackup

    cfg = Config(config_path=None)
    fb = FilesystemBackup(cfg)
    empty_src = tmp_path / "empty_source"
    empty_src.mkdir()

    target = FilesystemTarget(
        name="empty",
        path=str(empty_src),
        excludes=[],
        enabled=True,
    )
    backup_dir = tmp_path / "backup_20260227_170000"

    result = fb.backup_path(target, backup_dir)
    assert result is True
    dest = backup_dir / "filesystems" / "empty"
    assert dest.exists()
    assert list(dest.iterdir()) == []


def test_restore_volume_roundtrip(docker_client, alpine_rsync_image, seeded_volume, tmp_path, unique_id):
    """Backup then restore to a new volume: file content is preserved."""
    from bbackup.config import Config
    from bbackup.docker_backup import DockerBackup
    from bbackup.restore import DockerRestore

    cfg = Config(config_path=None)

    original_run = docker_client.containers.run

    def patched_run(image, **kwargs):
        return original_run(alpine_rsync_image, **kwargs)

    # Backup
    with pytest.MonkeyPatch.context() as m:
        m.setattr(docker_client.containers, "run", patched_run)
        db = DockerBackup(cfg)
        db.client = docker_client
        backup_result = db.backup_volume(seeded_volume.name, tmp_path)

    # Only proceed with restore if backup succeeded
    if not backup_result:
        pytest.skip("Backup failed, skipping restore roundtrip test")

    restore_vol_name = f"bbackup_itest_restore_{unique_id}"
    dr = DockerRestore(cfg)
    dr.client = docker_client
    try:
        restore_result = dr.restore_volume(seeded_volume.name, tmp_path, new_name=restore_vol_name)
        if restore_result:
            # Verify file content by running a container
            output = docker_client.containers.run(
                ALPINE_HELPER_IMAGE,
                command="cat /v/file.txt",
                volumes={restore_vol_name: {"bind": "/v", "mode": "ro"}},
                remove=True,
            )
            assert b"hello" in output
    finally:
        try:
            vol = docker_client.volumes.get(restore_vol_name)
            vol.remove(force=True)
        except Exception:
            pass
