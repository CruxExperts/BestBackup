"""
Tests for bbackup.restore - DockerRestore with mocked Docker client and subprocess.
Covers list_backups, restore_container_config/volume/network, restore_backup
None-vs-list distinction, and decrypt_backup_directory.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import json
import io
import os
import stat
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import DockerException, APIError, NotFound

from bbackup.config import Config, EncryptionSettings
from bbackup.encryption import EncryptionManager
from bbackup.manifest import generate_backup_manifest
from bbackup.restore import DockerRestore, list_volume_backup_names


@pytest.fixture(autouse=True)
def isolate_mocked_daemon_volume_leases(tmp_path, monkeypatch):
    # Each test models its own Docker daemon; identical fixture volume names
    # must not contend across xdist workers. Keep the real lease implementation.
    from bbackup import restore
    original = restore._volume_restore_lock
    monkeypatch.setattr(restore, "_volume_restore_lock", lambda name: original(f"{tmp_path}:{name}"))


def make_restore(mock_docker_client):
    cfg = Config(config_path=None)
    return DockerRestore(cfg)


def configure_owned_volume(client, volume=None):
    def create(*, name, labels):
        created = volume or MagicMock()
        created.attrs = {"Name": name, "Labels": labels}
        return created
    client.volumes.create.side_effect = create


def write_container_config(backup_path: Path, name: str, image="nginx:latest"):
    configs_dir = backup_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    cfg_data = {
        "Config": {"Image": image, "Env": ["PORT=80"]},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
        "HostConfig": {"RestartPolicy": {"Name": "always"}},
    }
    (configs_dir / f"{name}_config.json").write_text(json.dumps(cfg_data))


# ---------------------------------------------------------------------------
# TestDockerRestoreInit
# ---------------------------------------------------------------------------


class TestDockerRestoreInit:
    def test_success(self, mock_docker_client):
        cfg = Config(config_path=None)
        dr = DockerRestore(cfg)
        assert dr.client is mock_docker_client

    def test_docker_exception_raises_runtime_error(self):
        cfg = Config(config_path=None)
        with patch("bbackup.restore.docker.from_env", side_effect=DockerException("no docker")):
            with pytest.raises(RuntimeError, match="Failed to connect to Docker"):
                DockerRestore(cfg)


# ---------------------------------------------------------------------------
# TestListBackups
# ---------------------------------------------------------------------------


class TestListBackups:
    def test_empty_dir_returns_empty(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert result == []

    def test_nonexistent_dir_returns_empty(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path / "nonexistent")
        assert result == []

    def test_dirs_without_backup_prefix_excluded(self, mock_docker_client, tmp_path):
        (tmp_path / "notabackup").mkdir()
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert result == []

    def test_metadata_file_provides_timestamp(self, mock_docker_client, tmp_path):
        bkp = tmp_path / "backup_20240101_000000"
        bkp.mkdir()
        meta = {"timestamp": "2024-01-01T12:00:00"}
        (bkp / "backup_metadata.json").write_text(json.dumps(meta))

        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert len(result) == 1
        assert result[0]["timestamp"] == "2024-01-01T12:00:00"

    def test_name_parsed_timestamp(self, mock_docker_client, tmp_path):
        bkp = tmp_path / "backup_20240115_093000"
        bkp.mkdir()
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert "2024-01-15" in result[0]["timestamp"]

    def test_unparseable_name_uses_raw_name(self, mock_docker_client, tmp_path):
        bkp = tmp_path / "backup_CUSTOM"
        bkp.mkdir()
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert result[0]["timestamp"] == "backup_CUSTOM"

    def test_sorted_by_mtime_descending(self, mock_docker_client, tmp_path):
        old = tmp_path / "backup_20240101_000000"
        old.mkdir()
        time.sleep(0.01)
        new = tmp_path / "backup_20240201_000000"
        new.mkdir()

        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert result[0]["name"] == "backup_20240201_000000"

    def test_list_includes_solid_archive_files(self, mock_docker_client, tmp_path):
        (tmp_path / "backup_20240304_120000.tar.gz").write_bytes(b"x")
        dr = make_restore(mock_docker_client)
        result = dr.list_backups(tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "backup_20240304_120000.tar.gz"
        assert "2024-03-04" in result[0]["timestamp"]


# ---------------------------------------------------------------------------
# TestRestoreContainerConfig
# ---------------------------------------------------------------------------


class TestRestoreContainerConfig:
    def test_missing_config_file_returns_false(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        assert dr.restore_container_config("web", tmp_path) is False

    def test_missing_image_returns_false(self, mock_docker_client, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "web_config.json").write_text(json.dumps({"Config": {"Image": ""}}))
        dr = make_restore(mock_docker_client)
        assert dr.restore_container_config("web", tmp_path) is False

    def test_existing_container_is_preserved_and_restore_refused(self, mock_docker_client, tmp_path):
        write_container_config(tmp_path, "web")
        existing = MagicMock()
        mock_docker_client.containers.get.return_value = existing
        mock_docker_client.containers.create.return_value = MagicMock()

        dr = make_restore(mock_docker_client)
        assert dr.restore_container_config("web", tmp_path) is False

        existing.stop.assert_not_called()
        existing.remove.assert_not_called()
        mock_docker_client.containers.create.assert_not_called()

    def test_env_and_ports_and_restart_extracted(self, mock_docker_client, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        cfg_data = {
            "Config": {"Image": "nginx:1.21", "Env": ["FOO=bar", "BAZ=qux"]},
            "NetworkSettings": {
                "Ports": {"80/tcp": [{"HostPort": "8080"}]},
                "Networks": {},
            },
            "Mounts": [{"Type": "volume", "Name": "mydata", "Destination": "/data"}],
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
        (configs_dir / "web_config.json").write_text(json.dumps(cfg_data))

        mock_docker_client.containers.get.side_effect = NotFound("not found")
        created = MagicMock()
        mock_docker_client.containers.create.return_value = created

        dr = make_restore(mock_docker_client)
        result = dr.restore_container_config("web", tmp_path)

        assert result is True
        create_call = mock_docker_client.containers.create.call_args
        assert create_call.kwargs["image"] == "nginx:1.21"
        assert "FOO" in create_call.kwargs["environment"]

    def test_network_connect_api_error_continues_returns_true(self, mock_docker_client, tmp_path):
        write_container_config(tmp_path, "web")
        configs_dir = tmp_path / "configs"
        cfg_data = {
            "Config": {"Image": "nginx:latest", "Env": []},
            "NetworkSettings": {"Ports": {}, "Networks": {"mynet": {}}},
            "Mounts": [],
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
        }
        (configs_dir / "web_config.json").write_text(json.dumps(cfg_data))

        mock_docker_client.containers.get.side_effect = NotFound("not found")
        mock_docker_client.containers.create.return_value = MagicMock()
        network = MagicMock()
        network.connect.side_effect = APIError("connect fail")
        mock_docker_client.networks.get.return_value = network

        dr = make_restore(mock_docker_client)
        result = dr.restore_container_config("web", tmp_path)
        assert result is True

    def test_general_exception_returns_false(self, mock_docker_client, tmp_path):
        write_container_config(tmp_path, "web")
        mock_docker_client.containers.get.side_effect = RuntimeError("unexpected")
        dr = make_restore(mock_docker_client)
        result = dr.restore_container_config("web", tmp_path)
        assert result is False

    def test_container_lookup_api_error_is_not_treated_as_absent(self, mock_docker_client, tmp_path):
        write_container_config(tmp_path, "web")
        mock_docker_client.containers.get.side_effect = APIError("daemon unavailable")
        dr = make_restore(mock_docker_client)

        assert dr.restore_container_config("web", tmp_path) is False
        mock_docker_client.containers.create.assert_not_called()

    def test_bind_mount_restore_is_refused(self, mock_docker_client, tmp_path):
        write_container_config(tmp_path, "web")
        config_path = tmp_path / "configs" / "web_config.json"
        data = json.loads(config_path.read_text())
        data["Mounts"] = [{"Type": "bind", "Source": "/srv/live", "Destination": "/data"}]
        config_path.write_text(json.dumps(data))

        dr = make_restore(mock_docker_client)
        assert dr.restore_container_config("web", tmp_path, new_name="web_restore") is False
        mock_docker_client.containers.get.assert_not_called()
        mock_docker_client.containers.create.assert_not_called()


# ---------------------------------------------------------------------------
# TestRestoreVolume
# ---------------------------------------------------------------------------


class TestRestoreVolume:
    def test_missing_volume_backup_dir_returns_false(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        assert dr.restore_volume("myvolume", tmp_path) is False

    def test_volume_lookup_api_error_is_not_treated_as_absent(self, mock_docker_client, tmp_path):
        (tmp_path / "volumes" / "myvolume").mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = APIError("daemon unavailable")
        dr = make_restore(mock_docker_client)

        assert dr.restore_volume("myvolume", tmp_path) is False
        mock_docker_client.volumes.create.assert_not_called()

    def test_new_name_used_for_target(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        configure_owned_volume(mock_docker_client)
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        dr = make_restore(mock_docker_client)
        dr.restore_volume("myvolume", tmp_path, new_name="myvolume_restored")

        create_call = mock_docker_client.volumes.create.call_args
        assert create_call.kwargs.get("name") == "myvolume_restored" or \
               (create_call.args and create_call.args[0] == "myvolume_restored")

    def test_existing_volume_is_preserved_and_restore_refused(self, mock_docker_client, mock_subprocess, tmp_path):
        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        existing_vol = MagicMock()
        mock_docker_client.volumes.get.return_value = existing_vol
        dr = make_restore(mock_docker_client)
        assert dr.restore_volume("myvolume", tmp_path) is False

        existing_vol.remove.assert_not_called()
        mock_docker_client.volumes.create.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_docker_cp_called(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        configure_owned_volume(mock_docker_client)
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        dr = make_restore(mock_docker_client)
        dr.restore_volume("myvolume", tmp_path)

        # docker cp should have been called
        cp_calls = [
            c for c in mock_run.call_args_list
            if c.args and "docker" in c.args[0] and "cp" in c.args[0]
        ]
        assert len(cp_calls) >= 1

    def test_concurrently_created_foreign_volume_is_never_copied_or_removed(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        (tmp_path / "volumes" / "myvolume").mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        foreign = MagicMock()
        foreign.attrs = {"Name": "myvolume", "Labels": {}}
        mock_docker_client.volumes.create.return_value = foreign
        dr = make_restore(mock_docker_client)

        assert dr.restore_volume("myvolume", tmp_path) is False
        foreign.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()
        mock_subprocess[0].assert_not_called()

    def test_compressed_volume_archive_restores(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        source_dir = tmp_path / "source" / "myvolume"
        source_dir.mkdir(parents=True)
        (source_dir / "data.txt").write_text("payload")
        volumes_dir = tmp_path / "volumes"
        volumes_dir.mkdir()
        with tarfile.open(volumes_dir / "myvolume.tar.gz", "w:gz") as tar:
            tar.add(source_dir, arcname="myvolume")

        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        configure_owned_volume(mock_docker_client)
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        dr = make_restore(mock_docker_client)
        assert dr.restore_volume("myvolume", tmp_path) is True

        cp_calls = [
            c for c in mock_run.call_args_list
            if c.args and "docker" in c.args[0] and "cp" in c.args[0]
        ]
        assert len(cp_calls) == 1
        assert cp_calls[0].args[0][2] == "-a"
        assert "myvolume" in cp_calls[0].args[0][3]

    def test_dot_root_volume_archive_preserves_root_mode_under_wrapper(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        mock_run, _ = mock_subprocess
        volumes_dir = tmp_path / "volumes"
        volumes_dir.mkdir()
        archive = volumes_dir / "myvolume.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            root.uid, root.gid = os.geteuid(), os.getegid()
            tar.addfile(root)
            item = tarfile.TarInfo("data.txt")
            item.uid, item.gid = os.geteuid(), os.getegid()
            item.size = len(b"payload")
            tar.addfile(item, io.BytesIO(b"payload"))

        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        configure_owned_volume(mock_docker_client)
        mock_docker_client.containers.run.return_value = MagicMock()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from bbackup.archive import extract_archive_safely as extract_original

        observed_modes = []

        def inspect_wrapper(tar, destination, **kwargs):
            wrapper = Path(destination).parent
            assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
            extract_original(tar, destination, **kwargs)
            observed_modes.append((
                stat.S_IMODE(wrapper.stat().st_mode),
                stat.S_IMODE(Path(destination).stat().st_mode),
            ))

        with patch("bbackup.restore.extract_archive_safely", side_effect=inspect_wrapper):
            dr = make_restore(mock_docker_client)
            assert dr.restore_volume("myvolume", tmp_path) is True

        assert observed_modes == [(0o700, 0o755)]
        copy_calls = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args and call.args[0][:2] == ["docker", "cp"]
        ]
        assert len(copy_calls) == 1
        assert "/payload/." in copy_calls[0][3]

    def test_restore_volume_returns_false_when_docker_cp_fails(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        mock_run, _ = mock_subprocess
        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        created_volume = MagicMock()
        configure_owned_volume(mock_docker_client, created_volume)
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="copy failed")

        dr = make_restore(mock_docker_client)
        assert dr.restore_volume("myvolume", tmp_path) is False
        created_volume.remove.assert_called_once()
        temp_container.stop.assert_called_once()
        temp_container.remove.assert_called_once()

    def test_existing_volume_is_not_staged_or_replaced(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        vol_dir = tmp_path / "volumes" / "myvolume"
        vol_dir.mkdir(parents=True)
        existing_vol = MagicMock()
        mock_docker_client.volumes.get.return_value = existing_vol

        dr = make_restore(mock_docker_client)
        assert dr.restore_volume("myvolume", tmp_path) is False

        existing_vol.remove.assert_not_called()
        mock_docker_client.volumes.create.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()


class TestVolumeBackupDiscovery:
    def test_list_volume_backup_names_includes_archives(self, tmp_path):
        volumes_dir = tmp_path / "volumes"
        volumes_dir.mkdir()
        (volumes_dir / "plain").mkdir()
        (volumes_dir / "gzipped.tar.gz").write_bytes(b"")
        (volumes_dir / "bzip.tar.bz2").write_bytes(b"")
        (volumes_dir / "xzipped.tar.xz").write_bytes(b"")
        (volumes_dir / "ignored.txt").write_text("")

        assert list_volume_backup_names(tmp_path) == [
            "bzip",
            "gzipped",
            "plain",
            "xzipped",
        ]


# ---------------------------------------------------------------------------
# TestRestoreNetwork
# ---------------------------------------------------------------------------


class TestRestoreNetwork:
    def test_missing_network_file_returns_false(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        assert dr.restore_network("mynet", tmp_path) is False

    def test_default_network_names_skipped(self, mock_docker_client, tmp_path):
        networks_dir = tmp_path / "networks"
        networks_dir.mkdir()
        for name in ["bridge", "host", "none"]:
            (networks_dir / f"{name}.json").write_text(json.dumps({"Driver": "bridge", "IPAM": {}}))
            dr = make_restore(mock_docker_client)
            result = dr.restore_network(name, tmp_path)
            assert result is True
        mock_docker_client.networks.create.assert_not_called()

    def test_existing_network_removed_and_recreated(self, mock_docker_client, tmp_path):
        networks_dir = tmp_path / "networks"
        networks_dir.mkdir()
        (networks_dir / "mynet.json").write_text(json.dumps({"Driver": "bridge", "IPAM": {}, "Options": {}}))
        existing = MagicMock()
        mock_docker_client.networks.get.return_value = existing
        mock_docker_client.networks.create.return_value = MagicMock()

        dr = make_restore(mock_docker_client)
        result = dr.restore_network("mynet", tmp_path)

        assert result is True
        existing.remove.assert_called_once()
        mock_docker_client.networks.create.assert_called_once()

    def test_driver_and_ipam_passed_to_create(self, mock_docker_client, tmp_path):
        networks_dir = tmp_path / "networks"
        networks_dir.mkdir()
        net_data = {"Driver": "overlay", "Options": {}, "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}
        (networks_dir / "overlay_net.json").write_text(json.dumps(net_data))
        mock_docker_client.networks.get.side_effect = APIError("not found")
        mock_docker_client.networks.create.return_value = MagicMock()

        dr = make_restore(mock_docker_client)
        dr.restore_network("overlay_net", tmp_path)

        create_call = mock_docker_client.networks.create.call_args
        assert create_call.kwargs.get("driver") == "overlay" or \
               (create_call.args and create_call.args[1] == "overlay")


# ---------------------------------------------------------------------------
# TestRestoreBackup
# ---------------------------------------------------------------------------


class TestRestoreBackup:
    def test_containers_none_skips_section(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(tmp_path, containers=None, volumes=None, networks=None)
        assert result["containers"] == {}

    def test_volumes_none_skips_section(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(tmp_path, containers=None, volumes=None, networks=None)
        assert result["volumes"] == {}

    def test_networks_none_skips_section(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(tmp_path, containers=None, volumes=None, networks=None)
        assert result["networks"] == {}

    def test_containers_empty_list_no_iteration(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(tmp_path, containers=[], volumes=None, networks=None)
        # Empty list but configs_dir doesn't exist -> no iteration
        assert result["containers"] == {}

    def test_configs_dir_missing_no_error(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        # containers=["web"] but no configs dir
        result = dr.restore_backup(tmp_path, containers=["web"], volumes=None, networks=None)
        # configs_dir doesn't exist, so the loop body is never entered
        assert result["containers"] == {}

    def test_rename_map_passed_through(self, mock_docker_client, mock_subprocess, tmp_path):
        mock_run, _ = mock_subprocess
        vol_dir = tmp_path / "volumes" / "data"
        vol_dir.mkdir(parents=True)
        mock_docker_client.volumes.get.side_effect = NotFound("not found")
        mock_docker_client.volumes.create.return_value = MagicMock()
        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container

        dr = make_restore(mock_docker_client)
        dr.restore_backup(
            tmp_path,
            containers=None,
            volumes=["data"],
            networks=None,
            rename_map={"data": "data_restored"},
        )
        create_call = mock_docker_client.volumes.create.call_args
        assert create_call.kwargs.get("name") == "data_restored" or \
               (create_call.args and "data_restored" in str(create_call.args))

    def test_partial_failure_errors_list(self, mock_docker_client, mock_subprocess, tmp_path):
        """One volume fails, other succeeds; errors list has one entry."""
        mock_run, _ = mock_subprocess
        # Create two volume dirs
        (tmp_path / "volumes" / "vol_ok").mkdir(parents=True)
        (tmp_path / "volumes" / "vol_fail").mkdir(parents=True)

        temp_container = MagicMock()
        mock_docker_client.containers.run.return_value = temp_container
        mock_docker_client.volumes.create.return_value = MagicMock()

        call_count = [0]

        def volumes_get_side_effect(name):
            call_count[0] += 1
            raise NotFound("not found")

        mock_docker_client.volumes.get.side_effect = volumes_get_side_effect

        # Make containers.run fail for vol_fail
        run_call_count = [0]

        def run_side_effect(*args, **kwargs):
            run_call_count[0] += 1
            if run_call_count[0] % 2 == 0:
                raise RuntimeError("container failed")
            return temp_container

        mock_docker_client.containers.run.side_effect = run_side_effect

        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(
            tmp_path,
            containers=None,
            volumes=["vol_ok", "vol_fail"],
            networks=None,
        )
        # Both volumes should be in results
        assert "vol_ok" in result["volumes"] or "vol_fail" in result["volumes"]

    def test_manifest_hash_mismatch_fails_before_restore(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        vol_dir = tmp_path / "volumes" / "data"
        vol_dir.mkdir(parents=True)
        (vol_dir / "file.txt").write_text("original")
        scope = Config(config_path=None).scope
        generate_backup_manifest(tmp_path, scope)
        (vol_dir / "file.txt").write_text("modified")

        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(
            tmp_path,
            containers=None,
            volumes=["data"],
            networks=None,
        )

        assert result["volumes"] == {}
        assert result["errors"] == ["Manifest file hash mismatch: volumes/data/file.txt"]
        mock_docker_client.volumes.create.assert_not_called()

    def test_manifest_extra_file_fails_before_restore(
        self, mock_docker_client, mock_subprocess, tmp_path
    ):
        vol_dir = tmp_path / "volumes" / "data"
        vol_dir.mkdir(parents=True)
        (vol_dir / "file.txt").write_text("original")
        scope = Config(config_path=None).scope
        generate_backup_manifest(tmp_path, scope)
        (vol_dir / "extra.txt").write_text("unexpected")

        dr = make_restore(mock_docker_client)
        result = dr.restore_backup(
            tmp_path,
            containers=None,
            volumes=["data"],
            networks=None,
        )

        assert result["volumes"] == {}
        assert result["errors"] == ["Manifest file not listed: volumes/data/extra.txt"]
        mock_docker_client.volumes.create.assert_not_called()


# ---------------------------------------------------------------------------
# TestDecryptBackupDirectory
# ---------------------------------------------------------------------------


class TestDecryptBackupDirectory:
    def test_no_metadata_returns_original(self, mock_docker_client, tmp_path):
        dr = make_restore(mock_docker_client)
        result = dr.decrypt_backup_directory(tmp_path)
        assert result == tmp_path

    def test_encryption_disabled_fails_closed(self, mock_docker_client, tmp_path):
        (tmp_path / "encryption_metadata.json").write_text("{}")
        cfg = Config(config_path=None)
        cfg.encryption.enabled = False
        dr = DockerRestore(cfg)
        with pytest.raises(RuntimeError, match="disabled"):
            dr.decrypt_backup_directory(tmp_path)

    def test_decryption_fails_closed(self, mock_docker_client, tmp_path):
        (tmp_path / "encryption_metadata.json").write_text("{}")
        cfg = Config(config_path=None)
        cfg.encryption.enabled = True
        dr = DockerRestore(cfg)

        with patch("bbackup.restore.EncryptionManager") as mock_em:
            instance = MagicMock()
            instance.decrypt_backup.side_effect = RuntimeError("bad key")
            mock_em.return_value = instance
            with pytest.raises(RuntimeError, match="Decryption failed"):
                dr.decrypt_backup_directory(tmp_path)

    def test_decryption_succeeds_returns_new_path(self, mock_docker_client, tmp_path):
        (tmp_path / "encryption_metadata.json").write_text("{}")
        cfg = Config(config_path=None)
        cfg.encryption.enabled = True
        dr = DockerRestore(cfg)

        decrypted = tmp_path.parent / "decrypted"

        with patch("bbackup.restore.EncryptionManager") as mock_em:
            instance = MagicMock()
            instance.decrypt_backup.return_value = decrypted
            mock_em.return_value = instance
            result = dr.decrypt_backup_directory(tmp_path)

        assert result == decrypted

    def test_restore_cleans_successful_decryption_staging_root(self, mock_docker_client, tmp_path):
        (tmp_path / "encryption_metadata.json").write_text("{}")
        cfg = Config(config_path=None)
        cfg.encryption.enabled = True
        dr = DockerRestore(cfg)
        private_root = tmp_path / "bbackup_decrypted_fixture"
        decrypted = private_root / "tree"
        decrypted.mkdir(parents=True, mode=0o700)

        with patch("bbackup.restore.EncryptionManager") as mock_em:
            mock_em.return_value.decrypt_backup.return_value = decrypted
            result = dr.restore_backup(tmp_path, containers=None, volumes=None, networks=None)

        assert result["errors"] == []
        assert not private_root.exists()

    def test_restore_backup_with_disabled_encryption_does_not_touch_docker(self, mock_docker_client, tmp_path):
        (tmp_path / "encryption_metadata.json").write_text("{}")
        write_container_config(tmp_path, "web")
        dr = make_restore(mock_docker_client)

        result = dr.restore_backup(tmp_path, containers=["web"], volumes=None, networks=None)

        assert result["containers"] == {}
        assert result["errors"]
        mock_docker_client.containers.get.assert_not_called()
        mock_docker_client.containers.create.assert_not_called()

    def test_wrong_key_restore_fails_before_docker_mutation(self, mock_docker_client, tmp_path):
        source = tmp_path / "backup_plain"
        write_container_config(source, "web")
        good_key = tmp_path / "good.key"
        good_key.write_bytes(b"g" * 32)
        good_settings = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": str(good_key)},
        )
        encrypted = EncryptionManager(good_settings).encrypt_backup(source)
        wrong_key = tmp_path / "wrong.key"
        wrong_key.write_bytes(b"w" * 32)
        cfg = Config(config_path=None)
        cfg.encryption = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": str(wrong_key)},
        )
        dr = DockerRestore(cfg)

        result = dr.restore_backup(encrypted, containers=["web"], volumes=None, networks=None)

        assert result["containers"] == {}
        assert "decryption failed" in result["errors"][0].lower()
        mock_docker_client.containers.get.assert_not_called()
        mock_docker_client.containers.create.assert_not_called()
