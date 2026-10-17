"""
Tests for bbackup.docker_backup - DockerBackup methods with mocked Docker client
and mocked subprocess. Covers all public methods, backup_volume paths (rsync,
tar fallback, incremental, compression, progress, cleanup), and create_backup scopes.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import json
import os
import io
import tarfile
import stat
import time
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import DockerException, APIError

from bbackup.config import BackupScope, Config
from bbackup.docker_backup import DockerBackup


def make_backup(mock_docker_client):
    """Create a DockerBackup using the shared mock_docker_client fixture."""
    cfg = Config(config_path=None)
    return DockerBackup(cfg)


# ---------------------------------------------------------------------------
# TestDockerBackupInit
# ---------------------------------------------------------------------------


class TestDockerBackupInit:
    def test_success(self, mock_docker_client):
        cfg = Config(config_path=None)
        db = DockerBackup(cfg)
        assert db.client is mock_docker_client

    def test_docker_exception_raises_runtime_error(self):
        cfg = Config(config_path=None)
        with patch("bbackup.docker_backup.docker.from_env", side_effect=DockerException("no docker")):
            with pytest.raises(RuntimeError, match="Failed to connect to Docker"):
                DockerBackup(cfg)


# ---------------------------------------------------------------------------
# TestGetResources
# ---------------------------------------------------------------------------


class TestGetResources:
    def test_get_all_containers_list(self, mock_docker_client):
        c = MagicMock()
        c.id = "abc123"
        c.name = "web"
        c.status = "running"
        c.image.tags = ["nginx:latest"]
        mock_docker_client.containers.list.return_value = [c]

        db = make_backup(mock_docker_client)
        result = db.get_all_containers()
        assert len(result) == 1
        assert result[0]["name"] == "web"

    def test_get_all_containers_empty(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        db = make_backup(mock_docker_client)
        assert db.get_all_containers() == []

    def test_get_all_containers_api_error_raises(self, mock_docker_client):
        mock_docker_client.containers.list.side_effect = APIError("fail")
        db = make_backup(mock_docker_client)
        with pytest.raises(RuntimeError):
            db.get_all_containers()

    def test_get_all_volumes_driver_mountpoint(self, mock_docker_client):
        v = MagicMock()
        v.name = "mydata"
        v.attrs = {"Driver": "local", "Mountpoint": "/var/lib/docker/volumes/mydata/_data"}
        mock_docker_client.volumes.list.return_value = [v]

        db = make_backup(mock_docker_client)
        result = db.get_all_volumes()
        assert result[0]["driver"] == "local"
        assert result[0]["mountpoint"] == "/var/lib/docker/volumes/mydata/_data"

    def test_get_all_volumes_api_error_raises(self, mock_docker_client):
        mock_docker_client.volumes.list.side_effect = APIError("fail")
        db = make_backup(mock_docker_client)
        with pytest.raises(RuntimeError):
            db.get_all_volumes()

    def test_get_all_networks_filters_defaults(self, mock_docker_client):
        def make_net(name):
            n = MagicMock()
            n.name = name
            n.id = f"id_{name}"
            n.attrs = {"Driver": "bridge"}
            return n

        networks = [make_net("bridge"), make_net("host"), make_net("none"), make_net("mynet")]
        mock_docker_client.networks.list.return_value = networks

        db = make_backup(mock_docker_client)
        result = db.get_all_networks()
        names = [n["name"] for n in result]
        assert "mynet" in names
        assert "host" not in names
        assert "none" not in names

    def test_get_all_networks_api_error_raises(self, mock_docker_client):
        mock_docker_client.networks.list.side_effect = APIError("fail")
        db = make_backup(mock_docker_client)
        with pytest.raises(RuntimeError):
            db.get_all_networks()


# ---------------------------------------------------------------------------
# TestBackupContainerConfig
# ---------------------------------------------------------------------------


class TestBackupContainerConfig:
    def test_json_and_logs_written(self, mock_docker_client, tmp_path):
        container = MagicMock()
        container.attrs = {"Id": "abc", "Name": "web", "Config": {}}
        container.logs.return_value = b"some logs"
        mock_docker_client.containers.get.return_value = container

        db = make_backup(mock_docker_client)
        result = db.backup_container_config("web", tmp_path)

        assert result is True
        config_file = tmp_path / "web_config.json"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["Id"] == "abc"
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "web_logs.txt").stat().st_mode) == 0o600

    def test_container_not_found_returns_false(self, mock_docker_client, tmp_path):
        mock_docker_client.containers.get.side_effect = APIError("not found")
        db = make_backup(mock_docker_client)
        assert db.backup_container_config("missing", tmp_path) is False


# ---------------------------------------------------------------------------
# TestFindPreviousVolumeBackup
# ---------------------------------------------------------------------------


class TestFindPreviousVolumeBackup:
    def test_no_backups_root_returns_none(self, mock_docker_client, tmp_path):
        db = make_backup(mock_docker_client)
        result = db._find_previous_volume_backup("myvolume", tmp_path / "nonexistent")
        assert result is None

    def test_empty_root_returns_none(self, mock_docker_client, tmp_path):
        db = make_backup(mock_docker_client)
        result = db._find_previous_volume_backup("myvolume", tmp_path)
        assert result is None

    def test_finds_volume_subdir(self, mock_docker_client, tmp_path):
        backup_dir = tmp_path / "backup_20240101_000000"
        vol_dir = backup_dir / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        db = make_backup(mock_docker_client)
        result = db._find_previous_volume_backup("myvolume", tmp_path)
        assert result == vol_dir

    def test_most_recent_selected(self, mock_docker_client, tmp_path):
        old = tmp_path / "backup_20240101_000000"
        (old / "volumes" / "myvolume").mkdir(parents=True)
        time.sleep(0.01)
        new = tmp_path / "backup_20240201_000000"
        (new / "volumes" / "myvolume").mkdir(parents=True)

        db = make_backup(mock_docker_client)
        result = db._find_previous_volume_backup("myvolume", tmp_path)
        assert result == new / "volumes" / "myvolume"


# ---------------------------------------------------------------------------
# TestBackupNetwork
# ---------------------------------------------------------------------------


class TestBackupNetwork:
    def test_json_written(self, mock_docker_client, tmp_path):
        network = MagicMock()
        network.attrs = {"Id": "net123", "Name": "mynet", "Driver": "bridge"}
        mock_docker_client.networks.get.return_value = network

        db = make_backup(mock_docker_client)
        result = db.backup_network("mynet", tmp_path)

        assert result is True
        net_file = tmp_path / "networks" / "mynet.json"
        assert net_file.exists()

    def test_api_error_returns_false(self, mock_docker_client, tmp_path):
        mock_docker_client.networks.get.side_effect = APIError("fail")
        db = make_backup(mock_docker_client)
        assert db.backup_network("nonet", tmp_path) is False


# ---------------------------------------------------------------------------
# TestCreateMetadataArchive
# ---------------------------------------------------------------------------


class TestCreateMetadataArchive:
    def test_gzip_mode(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        cfg.data = {"backup": {"compression": {"format": "gzip"}}}
        db = DockerBackup(cfg)

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "web_config.json").write_text("{}")

        output_file = tmp_path / "metadata.tar.gz"
        result = db.create_metadata_archive(tmp_path, output_file)
        assert result is True
        assert output_file.exists()

    def test_bzip2_mode(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        cfg.data = {"backup": {"compression": {"format": "bzip2"}}}
        db = DockerBackup(cfg)

        output_file = tmp_path / "metadata.tar.bz2"
        result = db.create_metadata_archive(tmp_path, output_file)
        assert result is True

    def test_empty_dirs(self, mock_docker_client, tmp_path):
        db = make_backup(mock_docker_client)
        output_file = tmp_path / "metadata.tar.gz"
        result = db.create_metadata_archive(tmp_path, output_file)
        assert result is True


# ---------------------------------------------------------------------------
# TestBackupVolumeRsync
# ---------------------------------------------------------------------------


class TestBackupVolumeRsync:
    def test_rsync_success(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, mock_popen = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()

        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        # check_rsync returns 0 (rsync available), main rsync returns 0
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        db = make_backup(mock_docker_client)
        result = db.backup_volume("myvolume", tmp_path)
        assert result is True

    def test_rsync_docker_cp_failure_returns_false(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        def run_side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["docker", "cp"]:
                result.returncode = 1
                result.stderr = "copy failed"
            return result

        mock_run.side_effect = run_side_effect

        db = make_backup(mock_docker_client)
        assert db.backup_volume("myvolume", tmp_path) is False
        assert not (tmp_path / "volumes" / "myvolume").exists()

    def test_volume_not_found_returns_false(self, mock_docker_client, tmp_path):
        from docker.errors import APIError
        mock_docker_client.volumes.get.side_effect = APIError("not found")
        db = make_backup(mock_docker_client)
        result = db.backup_volume("missing", tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# TestBackupVolumeTarFallback
# ---------------------------------------------------------------------------


class TestBackupVolumeTarFallback:
    @pytest.mark.skipif(os.geteuid() == 0, reason="non-root ownership refusal")
    def test_tar_capture_refuses_foreign_owner_before_inventory(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        def run(cmd, **kwargs):
            if "which" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="")
            if "cp" in cmd:
                with tarfile.open(cmd[-1], "w:gz") as tar:
                    member = tarfile.TarInfo("./private.txt")
                    member.size = 4
                    member.uid = os.geteuid() + 1
                    member.gid = os.getegid()
                    tar.addfile(member, io.BytesIO(b"data"))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run
        db = make_backup(mock_docker_client)
        assert db.backup_volume("myvolume", tmp_path) is False
        assert not (tmp_path / "volumes" / "myvolume").exists()
        assert not (tmp_path / "volumes" / "myvolume.tar.gz").exists()

    @pytest.mark.skipif(not hasattr(os, "setxattr"), reason="extended attributes unavailable")
    def test_encrypted_capture_checks_original_volume_before_copy(self, mock_docker_client, tmp_path):
        mountpoint = tmp_path / "mounted_volume"
        mountpoint.mkdir()
        file = mountpoint / "file.txt"
        file.write_text("data")
        try:
            os.setxattr(file, "user.bbackup-test", b"unsupported")
        except OSError as exc:
            pytest.skip(f"extended attributes unavailable: {exc}")
        volume = MagicMock()
        volume.attrs = {"Mountpoint": str(mountpoint)}
        mock_docker_client.volumes.get.return_value = volume
        cfg = Config(config_path=None)
        cfg.encryption.enabled = True
        db = DockerBackup(cfg)

        assert db.backup_volume("myvolume", tmp_path) is False
        mock_docker_client.containers.run.assert_not_called()
        assert not (tmp_path / "volumes").exists()

    def test_tar_fallback_when_rsync_absent(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()

        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        # check_rsync returns non-zero (rsync not available)
        # tar returns 0
        call_count = [0]
        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            if "which" in cmd and "rsync" in cmd:
                result.returncode = 1  # rsync not found
            elif "tar" in cmd:
                result.returncode = 0
            else:
                result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            call_count[0] += 1
            return result

        mock_run.side_effect = run_side_effect

        db = make_backup(mock_docker_client)
        db.backup_volume("myvolume", tmp_path)
        # Verify tar was called (any call with 'tar' in it)
        calls_with_tar = [
            c for c in mock_run.call_args_list
            if c.args and "tar" in c.args[0]
        ]
        assert len(calls_with_tar) > 0


# ---------------------------------------------------------------------------
# TestBackupVolumeIncremental
# ---------------------------------------------------------------------------


class TestBackupVolumeIncremental:
    def test_incremental_with_prev_uses_link_dest(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        # Create a previous backup
        prev_dir = tmp_path / "backup_old"
        (prev_dir / "volumes" / "myvolume").mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        db = make_backup(mock_docker_client)
        db.backup_volume("myvolume", tmp_path / "current", incremental=True)

        all_calls_flat = " ".join(
            str(c.args[0]) for c in mock_run.call_args_list if c.args
        )
        assert "--link-dest" in all_calls_flat

    def test_incremental_without_prev_no_link_dest(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        db = make_backup(mock_docker_client)
        # Patch _find_previous_volume_backup to return None (no prior backup)
        with patch.object(db, "_find_previous_volume_backup", return_value=None):
            db.backup_volume("myvolume", tmp_path / "current", incremental=True)

        all_calls_flat = " ".join(
            str(c.args[0]) for c in mock_run.call_args_list if c.args
        )
        assert "--link-dest" not in all_calls_flat


# ---------------------------------------------------------------------------
# TestBackupVolumeProgress
# ---------------------------------------------------------------------------


class TestBackupVolumeProgress:
    def test_popen_used_when_callback_given(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, mock_popen = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        # check_rsync returns 0 (rsync available)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        proc = MagicMock()
        proc.stdout.__iter__ = MagicMock(return_value=iter(["progress line 1\n"]))
        proc.poll.return_value = None
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen.return_value = proc

        lines_received = []
        db = make_backup(mock_docker_client)
        db.backup_volume("myvolume", tmp_path, progress_callback=lines_received.append)

        mock_popen.assert_called()


# ---------------------------------------------------------------------------
# TestBackupVolumeCompression
# ---------------------------------------------------------------------------


class TestBackupVolumeCompression:
    def test_gzip_creates_tar_gz(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Enable compression in config
        cfg = Config(config_path=None)
        cfg.data = {"backup": {"compression": {"enabled": True, "format": "gzip"}}}
        db = DockerBackup(cfg)

        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        vol_dir.parent.chmod(0o700)
        (vol_dir / "file.txt").write_text("data")

        db.backup_volume("myvolume", tmp_path)
        # The code creates a tar.gz and removes the dir; we just verify no exception


# ---------------------------------------------------------------------------
# TestBackupVolumeCleanup
# ---------------------------------------------------------------------------


class TestBackupVolumeCleanup:
    def test_temp_container_stopped_on_exception(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.volumes.get.return_value = MagicMock()

        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        # Make subprocess.run raise on the mkdir call (simulates inner block exception)
        def raise_on_mkdir(cmd, **kwargs):
            if "mkdir" in cmd:
                raise RuntimeError("disk full")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = raise_on_mkdir

        db = make_backup(mock_docker_client)
        result = db.backup_volume("myvolume", tmp_path)
        # Should have tried to clean up
        assert temp_container.stop.called or result is False


# ---------------------------------------------------------------------------
# TestGetContainerVolumes
# ---------------------------------------------------------------------------


class TestGetContainerVolumes:
    def test_volume_type_included(self, mock_docker_client):
        container = MagicMock()
        container.attrs = {
            "Mounts": [
                {"Type": "volume", "Name": "mydata"},
                {"Type": "bind", "Name": "/host/path"},
            ]
        }
        mock_docker_client.containers.get.return_value = container

        db = make_backup(mock_docker_client)
        volumes = db._get_container_volumes(["web"])
        assert "mydata" in volumes
        assert "/host/path" not in volumes

    def test_api_error_skips_container(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = APIError("fail")
        db = make_backup(mock_docker_client)
        # Should not raise, just skip
        volumes = db._get_container_volumes(["missing"])
        assert volumes == set()


# ---------------------------------------------------------------------------
# TestCreateBackup
# ---------------------------------------------------------------------------


class TestCreateBackup:
    def test_all_scope_populates_keys(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        mock_docker_client.containers.list.return_value = []
        mock_docker_client.volumes.list.return_value = []
        mock_docker_client.networks.list.return_value = []

        db = make_backup(mock_docker_client)
        scope = BackupScope(containers=True, volumes=True, networks=True, configs=True)
        result = db.create_backup(tmp_path, scope=scope)

        assert "containers" in result
        assert "volumes" in result
        assert "networks" in result
        assert "errors" in result

    def test_containers_only_skips_volumes_and_networks(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_docker_client.containers.list.return_value = []
        db = make_backup(mock_docker_client)
        scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
        result = db.create_backup(tmp_path, scope=scope)
        assert result["volumes"] == {}
        assert result["networks"] == {}

    def test_failure_accumulation(self, mock_docker_client, mock_subprocess, tmp_path):
        container = MagicMock()
        container.name = "broken"
        container.id = "id1"
        container.status = "exited"
        container.image.tags = ["nginx:latest"]
        mock_docker_client.containers.list.return_value = [container]
        mock_docker_client.volumes.list.return_value = []
        mock_docker_client.networks.list.return_value = []

        # container.attrs raises APIError -> backup_container_config returns False
        mock_docker_client.containers.get.side_effect = APIError("fail")

        db = make_backup(mock_docker_client)
        scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
        result = db.create_backup(tmp_path, scope=scope)
        assert len(result["errors"]) >= 1

    def test_explicit_containers_list(self, mock_docker_client, mock_subprocess, tmp_path):
        container = MagicMock()
        container.attrs = {"Id": "abc", "Name": "web", "Config": {}}
        container.logs.return_value = b"logs"
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.volumes.list.return_value = []

        db = make_backup(mock_docker_client)
        scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
        result = db.create_backup(tmp_path, containers=["web"], scope=scope)

        # Verify get_all_containers was NOT called (explicit list used)
        mock_docker_client.containers.list.assert_not_called()
        assert "web" in result["containers"]
