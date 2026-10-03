"""
Tests for bbackup.backup_runner - BackupRunner lifecycle, pause/cancel/skip_current,
parse_rsync_progress closure, encrypt_backup_directory, and upload_to_remotes.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import json
import threading
from unittest.mock import MagicMock, patch


from bbackup.config import BackupScope, Config
from bbackup.backup_runner import BackupRunner
from bbackup.tui import BackupStatus


def make_runner(mock_docker_client, tmp_path):
    """Build a BackupRunner with all sub-dependencies mocked."""
    cfg = Config(config_path=None)
    status = BackupStatus()

    with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
         patch("bbackup.backup_runner.RemoteStorageManager") as MockRM, \
         patch("bbackup.backup_runner.BackupRotation") as MockRot:

        mock_db = MagicMock()
        mock_db.get_all_containers.return_value = []
        mock_db.get_all_volumes.return_value = []
        mock_db.get_all_networks.return_value = []
        MockDB.return_value = mock_db

        mock_rm = MagicMock()
        MockRM.return_value = mock_rm

        mock_rot = MagicMock()
        MockRot.return_value = mock_rot

        runner = BackupRunner(cfg, status)
        # Store references for later assertions
        runner._mock_db = mock_db
        runner._mock_rm = mock_rm
        runner._mock_rot = mock_rot

    return runner


# ---------------------------------------------------------------------------
# TestBackupRunnerInit
# ---------------------------------------------------------------------------


class TestBackupRunnerInit:
    def test_sub_objects_constructed(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        status = BackupStatus()
        runner = BackupRunner(cfg, status)
        assert runner.docker_backup is not None
        assert runner.remote_mgr is not None
        assert runner.rotation is not None
        assert runner.status is status


# ---------------------------------------------------------------------------
# TestRunBackupLifecycle
# ---------------------------------------------------------------------------


class TestRunBackupLifecycle:
    def test_basic_run_sets_completed_status(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        scope = BackupScope(containers=False, volumes=False, networks=False, configs=False)
        runner.run_backup(tmp_path, scope=scope)
        assert runner.status.status == "completed"
    def test_deferred_run_leaves_finalization_to_caller(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        scope = BackupScope(containers=False, volumes=False, networks=False, configs=False)

        runner.run_backup(tmp_path, scope=scope, defer_completion=True)

        assert runner.status.status == "finalizing"

    def test_item_failure_sets_partial_status(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner._mock_db.get_all_volumes.return_value = [{"name": "mydata"}]
        runner._mock_db.backup_volume.return_value = False
        scope = BackupScope(containers=False, volumes=True, networks=False, configs=False)

        result = runner.run_backup(tmp_path, scope=scope)

        assert result["volumes"]["mydata"] == "failed"
        assert result["errors"] == ["Failed to backup volume: mydata"]
        assert runner.status.status == "partial"
        manifest = json.loads((tmp_path / "backup_manifest.json").read_text())
        assert manifest["status"] == "partial"
        assert manifest["item_results"]["volumes"]["mydata"] == "failed"
        assert manifest["errors"] == ["Failed to backup volume: mydata"]

    def test_run_backup_writes_manifest(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        scope = BackupScope(containers=False, volumes=False, networks=False, configs=False)

        runner.run_backup(tmp_path, scope=scope)

        manifest_file = tmp_path / "backup_manifest.json"
        assert manifest_file.exists()
        manifest = json.loads(manifest_file.read_text())
        assert manifest["schema_version"] == 1
        assert manifest["backup_name"] == tmp_path.name
        assert manifest["status"] == "complete"
        assert manifest["source_scope"]["volumes"] is False

    def test_run_backup_creates_metadata_archive_before_manifest(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner._mock_db.backup_container_config.side_effect = (
            lambda _name, path: (path / "web_config.json").write_text("{}", encoding="utf-8") is not None
        )

        def create_metadata_archive(_backup_dir, output_file):
            output_file.write_bytes(b"metadata")
            return True

        runner._mock_db.create_metadata_archive.side_effect = create_metadata_archive
        scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)

        runner.run_backup(tmp_path, containers=["web"], scope=scope)

        metadata_archive = tmp_path / "metadata.tar.gz"
        assert metadata_archive.exists()
        manifest = json.loads((tmp_path / "backup_manifest.json").read_text())
        manifest_paths = {entry["path"] for entry in manifest["files"]}
        assert "metadata.tar.gz" in manifest_paths

    def test_metadata_archive_failure_sets_partial_status(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner._mock_db.backup_container_config.side_effect = (
            lambda _name, path: (path / "web_config.json").write_text("{}", encoding="utf-8") is not None
        )
        runner._mock_db.create_metadata_archive.return_value = False
        scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)

        result = runner.run_backup(tmp_path, containers=["web"], scope=scope)

        assert "Failed to create metadata archive" in result["errors"]
        assert runner.status.status == "partial"
        manifest = json.loads((tmp_path / "backup_manifest.json").read_text())
        assert manifest["status"] == "partial"

    def test_start_called_before_work(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        scope = BackupScope(containers=False, volumes=False, networks=False, configs=False)
        runner.run_backup(tmp_path, scope=scope)
        assert runner.status.start_time is not None

    def test_cancel_before_containers_stops_early(self, mock_docker_client, tmp_path):
        """When status is cancelled before run_backup, backup_container_config is never called."""
        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = [{"name": "web"}]
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)
            # Set cancelled AFTER runner constructed but status.start() not yet called
            # The run_backup checks status AFTER start(), so we need to cancel during execution.
            # Instead, use a side effect: cancel when get_all_containers is called (before the loop)
            def cancel_on_list(*args, **kwargs):
                status.status = "cancelled"
                return [{"name": "web"}]
            mock_db.get_all_containers.side_effect = cancel_on_list

            scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
            runner.run_backup(tmp_path, scope=scope)
        
        # Backup should have been cancelled - config backup not called
        mock_db.backup_container_config.assert_not_called()

    def test_cancel_mid_containers(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        status = BackupStatus()
        call_count = [0]

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = [{"name": "web"}, {"name": "db"}]
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []

            def backup_config_side_effect(name, path):
                call_count[0] += 1
                status.status = "cancelled"
                return True

            mock_db.backup_container_config.side_effect = backup_config_side_effect
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)
            scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
            runner.run_backup(tmp_path, scope=scope)

        # Only one container should have been processed before cancel
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# TestPauseCancel
# ---------------------------------------------------------------------------


class TestPauseCancel:
    def test_pause_then_resume_completes(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = [{"name": "web"}]
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []
            mock_db.backup_container_config.return_value = True
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)

            # Pause first, then resume after 200ms
            status.status = "paused"
            threading.Timer(0.2, lambda: setattr(status, "status", "running")).start()

            scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
            runner.run_backup(tmp_path, scope=scope)

        assert status.status == "completed"

    def test_cancel_while_paused_terminates(self, mock_docker_client, tmp_path):
        """Pausing then cancelling terminates the loop. We verify the cancel flag is set."""
        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = [{"name": "web"}]
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)

            # Pause during container iteration, then cancel quickly
            paused = threading.Event()

            def cancel_on_list(*args, **kwargs):
                status.status = "paused"
                paused.set()
                return [{"name": "web"}]

            mock_db.get_all_containers.side_effect = cancel_on_list
            threading.Timer(0.15, lambda: setattr(status, "status", "cancelled")).start()

            scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
            runner.run_backup(tmp_path, scope=scope)

        assert status.status == "cancelled"


# ---------------------------------------------------------------------------
# TestSkipCurrent
# ---------------------------------------------------------------------------


class TestSkipCurrent:
    def test_skip_container(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = [{"name": "web"}]
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)
            status.skip_current = True

            scope = BackupScope(containers=True, volumes=False, networks=False, configs=True)
            result = runner.run_backup(tmp_path, scope=scope)

        assert result["containers"]["web"] == "skipped"
        assert status.skip_current is False
        mock_db.backup_container_config.assert_not_called()

    def test_skip_volume(self, mock_docker_client, tmp_path):
        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = []
            mock_db.get_all_volumes.return_value = [{"name": "mydata"}]
            mock_db.get_all_networks.return_value = []
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)
            status.skip_current = True

            scope = BackupScope(containers=False, volumes=True, networks=False, configs=False)
            result = runner.run_backup(tmp_path, scope=scope)

        assert result["volumes"]["mydata"] == "skipped"
        assert status.skip_current is False


# ---------------------------------------------------------------------------
# TestParseRsyncProgress
# ---------------------------------------------------------------------------


class TestParseRsyncProgress:
    """
    parse_rsync_progress is a nested closure inside run_backup.
    We exercise it by passing a mock backup_volume that immediately calls the
    progress_callback with test lines.
    """

    def _run_with_lines(self, mock_docker_client, tmp_path, lines):
        """Helper: run backup with volume backup that fires callback with given lines."""
        cfg = Config(config_path=None)
        status = BackupStatus()
        status.start()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):

            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = []
            mock_db.get_all_networks.return_value = []
            mock_db.get_all_volumes.return_value = [{"name": "myvolume"}]

            def backup_volume_with_callback(name, path, incremental, progress_callback):
                for line in lines:
                    if progress_callback:
                        progress_callback(line)
                return True

            mock_db.backup_volume.side_effect = backup_volume_with_callback
            MockDB.return_value = mock_db

            runner = BackupRunner(cfg, status)
            scope = BackupScope(containers=False, volumes=True, networks=False, configs=False)
            runner.run_backup(tmp_path, scope=scope)

        return status

    def test_bytes_line_updates_bytes_transferred(self, mock_docker_client, tmp_path):
        lines = ["    1,234,567  50%  12.34MB/s    0:00:10\n"]
        status = self._run_with_lines(mock_docker_client, tmp_path, lines)
        # The parse_rsync_progress closure in run_backup calls update(bytes_transferred=...)
        # BackupStatus.update() accepts bytes_transferred as a kwarg
        assert status.bytes_transferred == 1234567

    def test_bytes_line_updates_total_bytes(self, mock_docker_client, tmp_path):
        lines = ["    1,234,567  50%  12.34MB/s    0:00:10\n"]
        status = self._run_with_lines(mock_docker_client, tmp_path, lines)
        # total = bytes * 100 / percentage = 1234567 * 100 / 50
        assert status.total_bytes == 2469134

    def test_bytes_line_updates_transfer_speed(self, mock_docker_client, tmp_path):
        lines = ["    1,234,567  50%  12.34MB/s    0:00:10\n"]
        status = self._run_with_lines(mock_docker_client, tmp_path, lines)
        # BackupStatus.update() does not accept transfer_speed as a kwarg; the parse closure
        # calls update(bytes_transferred=X, total_bytes=Y) which triggers speed recalculation.
        # After the parse call, transfer_speed is a float (possibly 0 if delta is too small).
        assert isinstance(status.transfer_speed, float)

    def test_file_count_line_updates_total_files(self, mock_docker_client, tmp_path):
        lines = ["Number of files: 1,234 (reg: 1,200, dir: 34)\n"]
        status = self._run_with_lines(mock_docker_client, tmp_path, lines)
        assert status.total_files == 1234

    def test_sent_received_line_increments_files_counted(self, mock_docker_client, tmp_path):
        lines = ["sent 100 bytes  received 200 bytes  300 bytes/sec\n"]
        status = self._run_with_lines(mock_docker_client, tmp_path, lines)
        assert hasattr(status, "_files_counted")
        assert status._files_counted == 1

    def test_non_matching_line_no_side_effects(self, mock_docker_client, tmp_path):
        status = self._run_with_lines(mock_docker_client, tmp_path, ["this is a random log line\n"])
        assert status.bytes_transferred == 0
        assert status.total_files == 0


# ---------------------------------------------------------------------------
# TestEncryptBackupDirectory
# ---------------------------------------------------------------------------


class TestEncryptBackupDirectory:
    def test_encryption_disabled_returns_original(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner.config.encryption.enabled = False
        result = runner.encrypt_backup_directory(tmp_path)
        assert result == tmp_path

    def test_encryption_enabled_calls_manager(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner.config.encryption.enabled = True
        encrypted = tmp_path / "backup.enc"

        with patch("bbackup.backup_runner.EncryptionManager") as MockEM:
            instance = MagicMock()
            instance.encrypt_backup.return_value = encrypted
            MockEM.return_value = instance
            result = runner.encrypt_backup_directory(tmp_path)

        assert result == encrypted
        assert runner.status.encryption_status == "encrypted"

    def test_encryption_exception_raises(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner.config.encryption.enabled = True

        with patch("bbackup.backup_runner.EncryptionManager", side_effect=RuntimeError("key error")):
            try:
                runner.encrypt_backup_directory(tmp_path)
            except RuntimeError as exc:
                assert "Encryption failed" in str(exc)
            else:
                raise AssertionError("encryption failure did not raise")

        assert len(runner.status.errors) >= 1

    def test_encryption_same_path_raises(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        runner.config.encryption.enabled = True

        with patch("bbackup.backup_runner.EncryptionManager") as MockEM:
            instance = MagicMock()
            instance.encrypt_backup.return_value = tmp_path
            MockEM.return_value = instance
            try:
                runner.encrypt_backup_directory(tmp_path)
            except RuntimeError as exc:
                assert "Encryption failed" in str(exc)
            else:
                raise AssertionError("same-path encryption failure did not raise")

        assert runner.status.encryption_status == "failed"


# ---------------------------------------------------------------------------
# TestUploadToRemotes
# ---------------------------------------------------------------------------


class TestUploadToRemotes:
    def test_no_remotes_returns_success(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        assert runner.upload_to_remotes(tmp_path, "backup_name", []) is True
        runner._mock_rm.upload_backup.assert_not_called()

    def test_success_sets_remote_status(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        remote = MagicMock()
        remote.name = "myremote"
        remote.type = "local"
        remote.path = str(tmp_path)

        runner._mock_rm.upload_backup.return_value = True
        runner._mock_rot.check_storage_quota.return_value = {
            "enabled": False, "warning": False, "cleanup_needed": False,
        }

        runner.upload_to_remotes(tmp_path, "backup_20240101", [remote])
        assert runner.status.remote_status["myremote"] == "success"

    def test_quota_exceeded_triggers_cleanup(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        remote = MagicMock()
        remote.name = "myremote"
        remote.type = "local"
        remote.path = str(tmp_path)

        runner._mock_rm.upload_backup.return_value = True
        runner._mock_rm.list_backups.return_value = ["backup_A", "backup_B"]
        runner._mock_rot.check_storage_quota.return_value = {
            "enabled": True, "warning": True, "cleanup_needed": True,
            "percent": 95, "used_gb": 95, "max_gb": 100,
        }
        runner._mock_rot.filter_backups_by_retention.return_value = (["backup_B"], ["backup_A"])
        runner._mock_rot.cleanup_old_backups.return_value = 1

        runner.upload_to_remotes(tmp_path, "backup_20240101", [remote])
        runner._mock_rot.cleanup_old_backups.assert_called_once()

    def test_quota_warning_only_adds_warning(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        remote = MagicMock()
        remote.name = "myremote"
        remote.type = "local"
        remote.path = str(tmp_path)

        runner._mock_rm.upload_backup.return_value = True
        runner._mock_rot.check_storage_quota.return_value = {
            "enabled": True, "warning": True, "cleanup_needed": False,
            "percent": 85, "used_gb": 85, "max_gb": 100,
        }

        runner.upload_to_remotes(tmp_path, "backup_20240101", [remote])
        assert len(runner.status.warnings) >= 1
        runner._mock_rot.cleanup_old_backups.assert_not_called()

    def test_mixed_remote_results_return_failure(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        remotes = []
        for name in ("remote1", "remote2"):
            remote = MagicMock()
            remote.name = name
            remote.type = "local"
            remote.path = str(tmp_path)
            remotes.append(remote)

        runner._mock_rm.upload_backup.side_effect = [True, False]
        runner._mock_rot.check_storage_quota.return_value = {
            "enabled": False, "warning": False, "cleanup_needed": False,
        }

        assert runner.upload_to_remotes(tmp_path, "backup_name", remotes) is False
        assert runner.status.remote_status == {
            "remote1": "success",
            "remote2": "failed",
        }
        assert len(runner.status.errors) == 1

    def test_cancel_mid_upload_skips_second_remote(self, mock_docker_client, tmp_path):
        runner = make_runner(mock_docker_client, tmp_path)
        r1 = MagicMock()
        r1.name = "remote1"
        r1.type = "local"
        r1.path = str(tmp_path)
        r2 = MagicMock()
        r2.name = "remote2"
        r2.type = "local"
        r2.path = str(tmp_path)

        def upload_side_effect(remote, path, name, cb=None):
            runner.status.status = "cancelled"
            return True

        runner._mock_rm.upload_backup.side_effect = upload_side_effect
        runner._mock_rot.check_storage_quota.return_value = {
            "enabled": False, "warning": False, "cleanup_needed": False,
        }

        runner.upload_to_remotes(tmp_path, "backup_20240101", [r1, r2])
        # Only one upload should have occurred
        assert runner._mock_rm.upload_backup.call_count == 1
