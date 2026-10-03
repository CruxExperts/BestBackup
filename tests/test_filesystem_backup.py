"""
Tests for bbackup.filesystem_backup (FilesystemBackup) and
bbackup.restore.DockerRestore.restore_filesystem_path.

Unit tests only - no real rsync or filesystem side-effects outside tmp_path.
All subprocess calls are mocked via the mock_subprocess conftest fixture or
targeted patches where finer control is needed.

Created: 2026-02-27
Last Updated: 2026-02-27
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from bbackup.config import Config, FilesystemTarget
from bbackup.filesystem_backup import FilesystemBackup
from bbackup.restore import DockerRestore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_target(
    name: str = "docs",
    path: str = "/tmp/source",
    excludes: list[str] | None = None,
    enabled: bool = True,
) -> FilesystemTarget:
    return FilesystemTarget(name=name, path=path, excludes=excludes or [], enabled=enabled)


def make_fs_backup(tmp_path: Path) -> FilesystemBackup:
    cfg = Config(config_path=None)
    return FilesystemBackup(cfg)


def make_restore(mock_docker_client) -> DockerRestore:
    cfg = Config(config_path=None)
    return DockerRestore(cfg)


# ---------------------------------------------------------------------------
# TestFilesystemBackupInit
# ---------------------------------------------------------------------------


class TestFilesystemBackupInit:
    def test_stores_config(self, tmp_path):
        cfg = Config(config_path=None)
        fb = FilesystemBackup(cfg)
        assert fb.config is cfg


# ---------------------------------------------------------------------------
# TestBuildRsyncCmd
# ---------------------------------------------------------------------------


class TestBuildRsyncCmd:
    def test_basic_command_structure(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target(path="/tmp/source")
        dest = tmp_path / "filesystems" / "docs"

        cmd = fb._build_rsync_cmd(target, dest, incremental=False, exclude_file_path=None)

        assert cmd[0] == "rsync"
        assert "-av" in cmd
        assert "--delete" in cmd
        assert "--progress" in cmd
        assert "--info=progress2" in cmd
        # Source must end with /
        assert "/tmp/source/" in cmd
        # Dest must end with /
        assert str(dest) + "/" in cmd

    def test_source_trailing_slash_normalised(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        # Path already has trailing slash
        target = make_target(path="/tmp/source/")
        dest = tmp_path / "filesystems" / "docs"
        cmd = fb._build_rsync_cmd(target, dest, incremental=False, exclude_file_path=None)
        # Should not end up with double slash
        sources = [a for a in cmd if a.startswith("/tmp/source")]
        assert len(sources) == 1
        assert sources[0] == "/tmp/source/"

    def test_exclude_from_inserted(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        dest = tmp_path / "filesystems" / "docs"
        excl_file = tmp_path / "excl.txt"
        excl_file.write_text("*.tmp\n")

        cmd = fb._build_rsync_cmd(target, dest, incremental=False, exclude_file_path=excl_file)

        assert "--exclude-from" in cmd
        idx = cmd.index("--exclude-from")
        assert cmd[idx + 1] == str(excl_file)

    def test_link_dest_inserted_when_prev_found(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target(name="docs", path="/tmp/source")

        # dest = staging/run_dir/filesystems/<name>
        # _find_previous_backup receives dest.parent.parent.parent = staging
        staging = tmp_path / "staging"
        dest = staging / "backup_20260227_120000" / "filesystems" / "docs"
        dest.mkdir(parents=True, exist_ok=True)

        prev_dir = staging / "backup_20260226_120000" / "filesystems" / "docs"
        prev_dir.mkdir(parents=True, exist_ok=True)

        cmd = fb._build_rsync_cmd(target, dest, incremental=True, exclude_file_path=None)

        assert "--link-dest" in cmd
        idx = cmd.index("--link-dest")
        assert str(prev_dir) in cmd[idx + 1]

    def test_no_link_dest_when_no_prev(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target(name="docs")

        staging = tmp_path / "staging"
        staging.mkdir()
        # Only the current run dir exists; no sibling prev backup
        dest = staging / "backup_20260227_120000" / "filesystems" / "docs"
        dest.mkdir(parents=True, exist_ok=True)

        cmd = fb._build_rsync_cmd(target, dest, incremental=True, exclude_file_path=None)
        assert "--link-dest" not in cmd

    def test_exclude_and_link_dest_both_present(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target(name="docs", path="/tmp/source")

        staging = tmp_path / "staging"
        dest = staging / "backup_20260227_120000" / "filesystems" / "docs"
        dest.mkdir(parents=True, exist_ok=True)

        prev_dir = staging / "backup_20260226_120000" / "filesystems" / "docs"
        prev_dir.mkdir(parents=True, exist_ok=True)

        excl_file = tmp_path / "excl.txt"
        excl_file.write_text("*.pyc\n")

        cmd = fb._build_rsync_cmd(target, dest, incremental=True, exclude_file_path=excl_file)

        assert "--exclude-from" in cmd
        assert "--link-dest" in cmd


# ---------------------------------------------------------------------------
# TestFindPreviousBackup
# ---------------------------------------------------------------------------


class TestFindPreviousBackup:
    def test_returns_none_when_staging_dir_missing(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        result = fb._find_previous_backup("docs", tmp_path / "nonexistent")
        assert result is None

    def test_returns_none_when_no_backup_dirs(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        result = fb._find_previous_backup("docs", tmp_path)
        assert result is None

    def test_returns_none_when_target_subdir_absent(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        # Backup dir exists but has no filesystems/docs subdir
        (tmp_path / "backup_20260226_100000").mkdir()
        result = fb._find_previous_backup("docs", tmp_path)
        assert result is None

    def test_returns_most_recent_matching_dir(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        old = tmp_path / "backup_20260220_100000" / "filesystems" / "docs"
        new = tmp_path / "backup_20260226_100000" / "filesystems" / "docs"
        old.mkdir(parents=True)
        new.mkdir(parents=True)

        result = fb._find_previous_backup("docs", tmp_path)
        # Should pick the newer one
        assert result == new

    def test_skips_dirs_with_wrong_name_pattern(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        # Name doesn't match backup_YYYYMMDD_HHMMSS
        bad = tmp_path / "mybadname" / "filesystems" / "docs"
        bad.mkdir(parents=True)
        result = fb._find_previous_backup("docs", tmp_path)
        assert result is None

    def test_skips_dirs_with_partial_pattern(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        # Missing time component
        bad = tmp_path / "backup_20260226" / "filesystems" / "docs"
        bad.mkdir(parents=True)
        result = fb._find_previous_backup("docs", tmp_path)
        assert result is None

    def test_target_name_isolation(self, tmp_path):
        """A previous backup for 'media' should not match target 'docs'."""
        fb = make_fs_backup(tmp_path)
        media = tmp_path / "backup_20260226_100000" / "filesystems" / "media"
        media.mkdir(parents=True)
        result = fb._find_previous_backup("docs", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# TestRunRsync
# ---------------------------------------------------------------------------


class TestRunRsync:
    def test_returns_true_on_zero_exit(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = fb._run_rsync(["rsync", "-av", "/src/", "/dst/"], progress_callback=None)
        assert result is True

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            result = fb._run_rsync(["rsync", "-av", "/src/", "/dst/"], progress_callback=None)
        assert result is False

    def test_uses_subprocess_run_without_callback(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            fb._run_rsync(["rsync", "src", "dst"], progress_callback=None)
        mock_run.assert_called_once()

    def test_uses_popen_with_callback(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        lines_received = []

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line1\n", "line2\n"])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            result = fb._run_rsync(
                ["rsync", "src", "dst"],
                progress_callback=lines_received.append,
            )

        assert result is True
        assert lines_received == ["line1\n", "line2\n"]

    def test_callback_receives_all_lines(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        collected = []
        expected = [f"line{i}\n" for i in range(5)]

        mock_proc = MagicMock()
        mock_proc.stdout = iter(expected)
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            fb._run_rsync(["rsync", "src", "dst"], progress_callback=collected.append)

        assert collected == expected

    def test_popen_nonzero_returns_false(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 2
        mock_proc.wait.return_value = 2

        with patch("subprocess.Popen", return_value=mock_proc):
            result = fb._run_rsync(["rsync", "src", "dst"], progress_callback=lambda _line: None)

        assert result is False


# ---------------------------------------------------------------------------
# TestBackupPath
# ---------------------------------------------------------------------------


class TestBackupPath:
    def test_creates_dest_dir(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target(name="docs", path="/tmp/source")
        backup_dir = tmp_path / "backup_20260227_120000"

        with patch.object(fb, "_run_rsync", return_value=True):
            fb.backup_path(target, backup_dir)

        assert (backup_dir / "filesystems" / "docs").exists()

    def test_returns_true_on_success(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        with patch.object(fb, "_run_rsync", return_value=True):
            result = fb.backup_path(target, tmp_path)
        assert result is True

    def test_returns_false_on_rsync_failure(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        with patch.object(fb, "_run_rsync", return_value=False):
            result = fb.backup_path(target, tmp_path)
        assert result is False

    def test_returns_false_on_exception(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        with patch.object(fb, "_build_rsync_cmd", side_effect=RuntimeError("boom")):
            result = fb.backup_path(target, tmp_path)
        assert result is False

    def test_no_excludes_skips_tempfile(self, tmp_path):
        """When target has no excludes, no --exclude-from should appear in command."""
        fb = make_fs_backup(tmp_path)
        target = make_target(excludes=[])

        captured_cmd = []

        def capture(cmd, cb):
            captured_cmd.extend(cmd)
            return True

        with patch.object(fb, "_run_rsync", side_effect=capture):
            fb.backup_path(target, tmp_path)

        assert "--exclude-from" not in captured_cmd

    def test_excludes_written_to_tempfile(self, tmp_path):
        """When target has excludes, the file must exist during rsync call."""
        fb = make_fs_backup(tmp_path)
        target = make_target(excludes=["*.tmp", ".cache/"])

        seen_exclude_file = []

        def capture(cmd, cb):
            if "--exclude-from" in cmd:
                idx = cmd.index("--exclude-from")
                seen_exclude_file.append(Path(cmd[idx + 1]))
            return True

        with patch.object(fb, "_run_rsync", side_effect=capture):
            fb.backup_path(target, tmp_path)

        # File existed during the call
        assert len(seen_exclude_file) == 1

    def test_tempfile_cleaned_up_after_success(self, tmp_path):
        """Temp exclude file must be removed after a successful backup."""
        fb = make_fs_backup(tmp_path)
        target = make_target(excludes=["*.pyc"])

        created_files = []

        original_mktemp = __import__("tempfile").mktemp

        def track_mktemp(suffix="", **kwargs):
            path = original_mktemp(suffix=suffix)
            created_files.append(path)
            return path

        with patch("tempfile.mktemp", side_effect=track_mktemp):
            with patch.object(fb, "_run_rsync", return_value=True):
                fb.backup_path(target, tmp_path)

        for path in created_files:
            assert not Path(path).exists(), f"Temp file not cleaned up: {path}"

    def test_tempfile_cleaned_up_after_failure(self, tmp_path):
        """Temp exclude file must be removed even when rsync fails."""
        fb = make_fs_backup(tmp_path)
        target = make_target(excludes=["*.pyc"])

        created_files = []
        original_mktemp = __import__("tempfile").mktemp

        def track_mktemp(suffix="", **kwargs):
            path = original_mktemp(suffix=suffix)
            created_files.append(path)
            return path

        with patch("tempfile.mktemp", side_effect=track_mktemp):
            with patch.object(fb, "_run_rsync", return_value=False):
                fb.backup_path(target, tmp_path)

        for path in created_files:
            assert not Path(path).exists(), f"Temp file leaked on failure: {path}"

    def test_tempfile_cleaned_up_after_exception(self, tmp_path):
        """Temp exclude file must be removed even when an exception is raised."""
        fb = make_fs_backup(tmp_path)
        target = make_target(excludes=["*.pyc"])

        created_files = []
        original_mktemp = __import__("tempfile").mktemp

        def track_mktemp(suffix="", **kwargs):
            path = original_mktemp(suffix=suffix)
            created_files.append(path)
            return path

        with patch("tempfile.mktemp", side_effect=track_mktemp):
            with patch.object(fb, "_run_rsync", side_effect=RuntimeError("boom")):
                result = fb.backup_path(target, tmp_path)

        assert result is False
        for path in created_files:
            assert not Path(path).exists(), f"Temp file leaked on exception: {path}"

    def test_progress_callback_forwarded(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        callback = MagicMock()

        with patch.object(fb, "_run_rsync", return_value=True) as mock_run:
            fb.backup_path(target, tmp_path, progress_callback=callback)
            _, kwargs = mock_run.call_args if mock_run.call_args else ((), {})
            # callback passed as second positional arg
            assert mock_run.call_args[0][1] is callback

    def test_incremental_flag_forwarded_to_build_cmd(self, tmp_path):
        fb = make_fs_backup(tmp_path)
        target = make_target()
        captured = {}

        def capture_build(t, d, incremental, exclude_file_path):
            captured["incremental"] = incremental
            return ["rsync", "src", "dst"]

        with patch.object(fb, "_build_rsync_cmd", side_effect=capture_build):
            with patch.object(fb, "_run_rsync", return_value=True):
                fb.backup_path(target, tmp_path, incremental=True)

        assert captured["incremental"] is True


# ---------------------------------------------------------------------------
# TestRestoreFilesystemPath
# ---------------------------------------------------------------------------


class TestRestoreFilesystemPath:
    def test_returns_false_when_backup_src_missing(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        result = dr.restore_filesystem_path(
            "docs", tmp_path / "backup_20260227_120000", destination=tmp_path / "dest"
        )
        assert result is False

    def test_returns_false_when_destination_is_none(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        # Create the backup source so it exists
        src = tmp_path / "filesystems" / "docs"
        src.mkdir(parents=True)
        result = dr.restore_filesystem_path("docs", tmp_path, destination=None)
        assert result is False

    def test_creates_destination_dir(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        src = tmp_path / "backup" / "filesystems" / "docs"
        src.mkdir(parents=True)
        dest = tmp_path / "restore" / "deep" / "nested"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            dr.restore_filesystem_path("docs", tmp_path / "backup", destination=dest)

        assert dest.exists()

    def test_returns_true_on_rsync_success(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        src = tmp_path / "backup" / "filesystems" / "docs"
        src.mkdir(parents=True)
        dest = tmp_path / "dest"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = dr.restore_filesystem_path("docs", tmp_path / "backup", destination=dest)

        assert result is True

    def test_returns_false_on_rsync_failure(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        src = tmp_path / "backup" / "filesystems" / "docs"
        src.mkdir(parents=True)
        dest = tmp_path / "dest"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="rsync: error")
            result = dr.restore_filesystem_path("docs", tmp_path / "backup", destination=dest)

        assert result is False

    def test_restore_backup_returns_structured_failure_when_destination_mkdir_fails(
        self, tmp_path, mock_docker_client
    ):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        (backup / "filesystems" / "docs").mkdir(parents=True)

        with patch.object(Path, "mkdir", side_effect=PermissionError("protected")):
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=["docs"],
                filesystem_destination=tmp_path / "restore",
            )

        assert result["filesystems"] == {"docs": "failed"}
        assert result["errors"] == ["Failed to restore filesystem: docs"]

    def test_restore_backup_returns_structured_failure_when_rsync_launch_fails(
        self, tmp_path, mock_docker_client
    ):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        (backup / "filesystems" / "docs").mkdir(parents=True)

        with patch("bbackup.restore.subprocess.run", side_effect=OSError("rsync unavailable")):
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=["docs"],
                filesystem_destination=tmp_path / "restore",
            )

        assert result["filesystems"] == {"docs": "failed"}
        assert result["errors"] == ["Failed to restore filesystem: docs"]

    def test_restore_backup_rejects_target_path_escape_before_mutation(
        self, tmp_path, mock_docker_client
    ):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        (backup / "filesystems").mkdir(parents=True)
        outside = backup / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep")
        destination = tmp_path / "restore"

        with patch("bbackup.restore.subprocess.run") as mock_run:
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=["../outside"],
                filesystem_destination=destination,
            )

        assert result["filesystems"] == {"../outside": "failed"}
        assert result["errors"] == ["Failed to restore filesystem: ../outside"]
        assert sentinel.read_text() == "keep"
        assert not destination.exists()
        mock_run.assert_not_called()

    def test_restore_backup_rejects_absolute_target_before_mutation(
        self, tmp_path, mock_docker_client
    ):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        source = backup / "filesystems" / "docs"
        source.mkdir(parents=True)
        sentinel = source / "sentinel.txt"
        sentinel.write_text("keep")
        destination = tmp_path / "restore"
        absolute_target = str(source)

        with patch("bbackup.restore.subprocess.run") as mock_run:
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=[absolute_target],
                filesystem_destination=destination,
            )

        assert result["filesystems"] == {absolute_target: "failed"}
        assert result["errors"] == [f"Failed to restore filesystem: {absolute_target}"]
        assert sentinel.read_text() == "keep"
        assert not destination.exists()
        mock_run.assert_not_called()


    def test_restore_backup_rejects_symlinked_source_escape(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        filesystems = backup / "filesystems"
        filesystems.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep")
        (filesystems / "docs").symlink_to(outside, target_is_directory=True)
        destination = tmp_path / "restore"

        with patch("bbackup.restore.subprocess.run") as mock_run:
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=["docs"],
                filesystem_destination=destination,
            )

        assert result["filesystems"] == {"docs": "failed"}
        assert result["errors"] == ["Failed to restore filesystem: docs"]
        assert sentinel.read_text() == "keep"
        assert not destination.exists()
        mock_run.assert_not_called()

    def test_restore_backup_rejects_symlinked_filesystems_root(
        self, tmp_path, mock_docker_client
    ):
        dr = make_restore(mock_docker_client)
        backup = tmp_path / "backup"
        backup.mkdir()
        outside = tmp_path / "outside"
        (outside / "docs").mkdir(parents=True)
        (backup / "filesystems").symlink_to(outside, target_is_directory=True)
        destination = tmp_path / "restore"

        with patch("bbackup.restore.subprocess.run") as mock_run:
            result = dr.restore_backup(
                backup_path=backup,
                filesystems=["docs"],
                filesystem_destination=destination,
            )

        assert result["filesystems"] == {"docs": "failed"}
        assert result["errors"] == ["Failed to restore filesystem: docs"]
        assert not destination.exists()
        mock_run.assert_not_called()


    def test_rsync_command_uses_trailing_slash(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        src = tmp_path / "backup" / "filesystems" / "docs"
        src.mkdir(parents=True)
        dest = tmp_path / "dest"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            dr.restore_filesystem_path("docs", tmp_path / "backup", destination=dest)

        cmd = mock_run.call_args[0][0]
        # Both source and dest must end with /
        assert cmd[-2].endswith("/")
        assert cmd[-1].endswith("/")

    def test_rsync_uses_delete_flag(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        src = tmp_path / "backup" / "filesystems" / "docs"
        src.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            dr.restore_filesystem_path("docs", tmp_path / "backup", destination=tmp_path / "d")

        cmd = mock_run.call_args[0][0]
        assert "--delete" in cmd


# ---------------------------------------------------------------------------
# TestRestoreBackupFilesystemIntegration
# ---------------------------------------------------------------------------


class TestRestoreBackupFilesystems:
    """Tests for restore_backup() filesystem section."""

    def test_filesystems_key_in_results(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        results = dr.restore_backup(backup_path=tmp_path)
        assert "filesystems" in results

    def test_filesystems_none_skips_loop(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        with patch.object(dr, "restore_filesystem_path") as mock_rfp:
            dr.restore_backup(backup_path=tmp_path, filesystems=None)
        mock_rfp.assert_not_called()

    def test_filesystems_empty_list_skips_loop(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        with patch.object(dr, "restore_filesystem_path") as mock_rfp:
            dr.restore_backup(backup_path=tmp_path, filesystems=[])
        mock_rfp.assert_not_called()

    def test_filesystems_success_populates_results(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        with patch.object(dr, "restore_filesystem_path", return_value=True):
            results = dr.restore_backup(
                backup_path=tmp_path,
                filesystems=["docs", "photos"],
                filesystem_destination=tmp_path / "dest",
            )
        assert results["filesystems"]["docs"] == "success"
        assert results["filesystems"]["photos"] == "success"
        assert results["errors"] == []

    def test_filesystems_failure_populates_errors(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        with patch.object(dr, "restore_filesystem_path", return_value=False):
            results = dr.restore_backup(
                backup_path=tmp_path,
                filesystems=["docs"],
                filesystem_destination=tmp_path / "dest",
            )
        assert results["filesystems"]["docs"] == "failed"
        assert len(results["errors"]) == 1
        assert "docs" in results["errors"][0]

    def test_filesystem_destination_forwarded(self, tmp_path, mock_docker_client):
        dr = make_restore(mock_docker_client)
        dest = tmp_path / "custom_dest"

        with patch.object(dr, "restore_filesystem_path", return_value=True) as mock_rfp:
            dr.restore_backup(
                backup_path=tmp_path,
                filesystems=["docs"],
                filesystem_destination=dest,
            )

        mock_rfp.assert_called_once_with("docs", tmp_path, dest)

    def test_partial_failure_continues(self, tmp_path, mock_docker_client):
        """One target failing should not prevent others from running."""
        dr = make_restore(mock_docker_client)

        def side_effect(name, backup_path, destination):
            return name != "bad"

        with patch.object(dr, "restore_filesystem_path", side_effect=side_effect):
            results = dr.restore_backup(
                backup_path=tmp_path,
                filesystems=["good", "bad", "also_good"],
                filesystem_destination=tmp_path / "dest",
            )

        assert results["filesystems"]["good"] == "success"
        assert results["filesystems"]["bad"] == "failed"
        assert results["filesystems"]["also_good"] == "success"
        assert len(results["errors"]) == 1


# ---------------------------------------------------------------------------
# TestBackupRunnerFilesystemLoop
# ---------------------------------------------------------------------------


class TestBackupRunnerFilesystemLoop:
    """Verify BackupRunner.run_backup() correctly wires the filesystem loop."""

    def _make_runner_and_status(self, mock_docker_client):
        from bbackup.backup_runner import BackupRunner
        from bbackup.tui import BackupStatus

        cfg = Config(config_path=None)
        status = BackupStatus()

        with patch("bbackup.backup_runner.DockerBackup") as MockDB, \
             patch("bbackup.backup_runner.RemoteStorageManager"), \
             patch("bbackup.backup_runner.BackupRotation"):
            mock_db = MagicMock()
            mock_db.get_all_containers.return_value = []
            mock_db.get_all_volumes.return_value = []
            mock_db.get_all_networks.return_value = []
            MockDB.return_value = mock_db
            runner = BackupRunner(cfg, status)
            runner._mock_db = mock_db

        return runner, status

    def test_filesystem_key_in_results(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            mock_fs.backup_path.return_value = True
            MockFS.return_value = mock_fs
            results = runner.run_backup(backup_dir=tmp_path)
        assert "filesystems" in results

    def test_filesystem_targets_backed_up(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        targets = [
            make_target(name="docs", path="/src/docs"),
            make_target(name="media", path="/src/media"),
        ]
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            mock_fs.backup_path.return_value = True
            MockFS.return_value = mock_fs
            results = runner.run_backup(
                backup_dir=tmp_path,
                filesystem_targets=targets,
            )
        assert mock_fs.backup_path.call_count == 2
        assert results["filesystems"]["docs"] == "success"
        assert results["filesystems"]["media"] == "success"

    def test_failed_target_adds_error(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        targets = [make_target(name="docs", path="/src/docs")]
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            mock_fs.backup_path.return_value = False
            MockFS.return_value = mock_fs
            results = runner.run_backup(
                backup_dir=tmp_path,
                filesystem_targets=targets,
            )
        assert results["filesystems"]["docs"] == "failed"
        assert len(results["errors"]) >= 1

    def test_filesystems_status_updated(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        targets = [make_target(name="photos", path="/src/photos")]
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            mock_fs.backup_path.return_value = True
            MockFS.return_value = mock_fs
            runner.run_backup(backup_dir=tmp_path, filesystem_targets=targets)
        assert runner.status.filesystems_status.get("photos") == "success"

    def test_no_targets_loop_skipped(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            MockFS.return_value = mock_fs
            runner.run_backup(backup_dir=tmp_path, filesystem_targets=[])
        mock_fs.backup_path.assert_not_called()

    def test_scope_filesystems_false_skips_loop(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        scope = Config(config_path=None).scope
        scope.filesystems = False
        targets = [make_target(name="docs")]
        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            MockFS.return_value = mock_fs
            runner.run_backup(backup_dir=tmp_path, scope=scope, filesystem_targets=targets)
        mock_fs.backup_path.assert_not_called()

    def test_cancelled_before_filesystem_loop(self, tmp_path, mock_docker_client):
        """status.start() is called at the top of run_backup; we must set cancelled after that."""
        runner, status = self._make_runner_and_status(mock_docker_client)
        targets = [make_target(name="docs")]

        original_start = status.start

        def start_then_cancel():
            original_start()
            status.status = "cancelled"

        status.start = start_then_cancel

        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            MockFS.return_value = mock_fs
            runner.run_backup(backup_dir=tmp_path, filesystem_targets=targets)
        mock_fs.backup_path.assert_not_called()

    def test_skip_current_marks_skipped(self, tmp_path, mock_docker_client):
        runner, status = self._make_runner_and_status(mock_docker_client)
        targets = [make_target(name="docs")]

        def set_skip_before_backup(*args, **kwargs):
            # skip_current gets consumed by the filesystem loop on the next
            # check, simulated here by pre-setting it
            return True

        runner.status.skip_current = True

        with patch("bbackup.backup_runner.FilesystemBackup") as MockFS:
            mock_fs = MagicMock()
            mock_fs.backup_path.side_effect = set_skip_before_backup
            MockFS.return_value = mock_fs
            results = runner.run_backup(backup_dir=tmp_path, filesystem_targets=targets)

        assert results["filesystems"].get("docs") == "skipped"
        assert runner.status.skip_current is False
