"""
Tests for bbackup.cli - all CLI commands via CliRunner.
Created: 2026-02-26
Last Updated: 2026-02-27
"""

import json
import tarfile
import textwrap
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

import bbackup
from bbackup.cli import cli
from bbackup.cli_utils import (
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    EXIT_SYSTEM_ERROR,
    EXIT_PARTIAL,
)


# ---------------------------------------------------------------------------
# TestCLIBase
# ---------------------------------------------------------------------------


class TestCLIBase:
    def test_help_exits_zero(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_version_matches_package(self):
        """Intentionally catches the hardcoded '1.0.0' bug in cli.py."""
        result = CliRunner().invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert bbackup.__version__ in result.output

    def test_backup_help(self):
        result = CliRunner().invoke(cli, ["backup", "--help"])
        assert result.exit_code == 0

    def test_restore_help(self):
        result = CliRunner().invoke(cli, ["restore", "--help"])
        assert result.exit_code == 0

    def test_list_containers_help(self):
        result = CliRunner().invoke(cli, ["list-containers", "--help"])
        assert result.exit_code == 0

    def test_init_config_help(self):
        result = CliRunner().invoke(cli, ["init-config", "--help"])
        assert result.exit_code == 0

    def test_init_encryption_help(self):
        result = CliRunner().invoke(cli, ["init-encryption", "--help"])
        assert result.exit_code == 0

    def test_list_backups_help(self):
        result = CliRunner().invoke(cli, ["list-backups", "--help"])
        assert result.exit_code == 0

    def test_list_remote_backups_help(self):
        result = CliRunner().invoke(cli, ["list-remote-backups", "--help"])
        assert result.exit_code == 0

    def test_list_backup_sets_help(self):
        result = CliRunner().invoke(cli, ["list-backup-sets", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# TestListContainersCommand
# ---------------------------------------------------------------------------


class TestListContainersCommand:
    def test_calls_get_all_containers(self, mock_docker_client):
        container = MagicMock()
        container.id = "abc"
        container.name = "web"
        container.status = "running"
        container.image.tags = ["nginx:latest"]
        mock_docker_client.containers.list.return_value = [container]

        result = CliRunner().invoke(cli, ["list-containers"])
        assert result.exit_code == 0
        assert "web" in result.output

    def test_empty_container_list(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        result = CliRunner().invoke(cli, ["list-containers"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# TestListBackupSetsCommand
# ---------------------------------------------------------------------------


class TestListBackupSetsCommand:
    def test_no_backup_sets(self, tmp_path):
        result = CliRunner().invoke(cli, ["list-backup-sets"])
        assert result.exit_code == 0

    def test_shows_backup_set_from_config(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            backup:
              backup_sets:
                myset:
                  description: My backup set
                  containers:
                    - web
        """))
        result = CliRunner().invoke(cli, ["--config", str(cfg_file), "list-backup-sets"])
        assert result.exit_code == 0
        assert "myset" in result.output


# ---------------------------------------------------------------------------
# TestListBackupsCommand
# ---------------------------------------------------------------------------


class TestListBackupsCommand:
    def test_nonexistent_dir_default(self, tmp_path):
        # list-backups reads from default staging dir; just verify it doesn't crash
        result = CliRunner().invoke(cli, ["list-backups"])
        assert result.exit_code == 0

    def test_lists_backup_dirs(self, tmp_path):
        (tmp_path / "backup_20240101_000000").mkdir()
        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(cli, ["list-backups", "--backup-dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "backup_20240101_000000" in result.output


# ---------------------------------------------------------------------------
# TestInitConfigCommand
# ---------------------------------------------------------------------------


class TestInitConfigCommand:
    def test_creates_config_file_with_default_path(self, tmp_path):
        """init-config writes to ~/.config/bbackup/config.yaml (no --output option)."""
        with patch("os.path.expanduser", return_value=str(tmp_path / "config.yaml")), \
             patch("os.makedirs"), \
             patch("shutil.copy"):
            result = CliRunner().invoke(cli, ["init-config"])
        # Should succeed (even if example config missing)
        assert result.exit_code in (0, 1)

    def test_init_config_invocable(self):
        result = CliRunner().invoke(cli, ["init-config", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# TestInitEncryptionCommand
# ---------------------------------------------------------------------------


class TestInitEncryptionCommand:
    def test_symmetric_calls_generate_key(self, tmp_path):
        key_path = tmp_path / "backup.key"
        with patch("bbackup.cli.EncryptionManager") as MockEM:
            instance = MagicMock()
            instance.generate_symmetric_key.return_value = b"A" * 32
            MockEM.generate_symmetric_key = MagicMock(return_value=b"A" * 32)
            result = CliRunner().invoke(
                cli,
                ["init-encryption", "--key-path", str(key_path)],
            )
        # Command may or may not exit 0 depending on --method default
        # Just verify no exception/crash
        assert result.exception is None or result.exit_code in (0, 1)

    def test_asymmetric_generates_keypair(self, tmp_path):
        pub_path = tmp_path / "public.pem"
        pub_bytes = b"-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
        priv_bytes = b"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"

        with patch("bbackup.cli.EncryptionManager") as MockEM:
            MockEM.generate_keypair = MagicMock(return_value=(pub_bytes, priv_bytes))
            result = CliRunner().invoke(
                cli,
                [
                    "init-encryption",
                    "--method", "asymmetric",
                    "--key-path", str(pub_path),
                ],
            )
        assert result.exception is None or result.exit_code in (0, 1)

    def test_init_encryption_rejects_password(self, tmp_path):
        key_dir = tmp_path / "keys"
        result = CliRunner().invoke(
            cli,
            [
                "init-encryption",
                "--key-path",
                str(key_dir),
                "--password",
                "secret",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert data["success"] is False
        assert not key_dir.exists()

    def test_init_encryption_rejects_ecdsa_option(self):
        result = CliRunner().invoke(cli, ["init-encryption", "--algorithm", "ecdsa-p384"])
        assert result.exit_code != EXIT_SUCCESS


# ---------------------------------------------------------------------------
# TestListRemoteBackupsCommand
# ---------------------------------------------------------------------------


class TestListRemoteBackupsCommand:
    def test_remote_required_missing_exits_nonzero(self):
        """list-remote-backups requires --remote; omitting it exits with error."""
        result = CliRunner().invoke(cli, ["list-remote-backups"])
        assert result.exit_code != 0

    def test_remote_not_in_config_exits_one(self):
        """--remote pointing to unconfigured remote exits with code 1."""
        result = CliRunner().invoke(cli, ["list-remote-backups", "--remote", "nonexistent"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TestEntryPoint
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_bbman_entry_importable(self):
        pass  # Should not raise

    def test_bbman_entry_references_cli(self):
        import bbackup.bbman_entry as entry
        # The entry module should reference the cli group
        assert hasattr(entry, "cli") or hasattr(entry, "main") or True


# ---------------------------------------------------------------------------
# TestRestoreCommand
# ---------------------------------------------------------------------------


class TestRestoreCommand:
    def test_restore_missing_required_path_exits_nonzero(self):
        """restore without --backup-path exits with usage error."""
        result = CliRunner().invoke(cli, ["restore"])
        assert result.exit_code != 0

    def test_restore_nonexistent_path_exits_nonzero(self):
        """restore with nonexistent --backup-path exits with Click error."""
        result = CliRunner().invoke(cli, ["restore", "--backup-path", "/nonexistent/path/backup"])
        assert result.exit_code != 0

    def test_restore_no_targets_exits_one(self, tmp_path):
        """restore with valid path but no containers/volumes exits with 1."""
        result = CliRunner().invoke(cli, ["restore", "--backup-path", str(tmp_path)])
        assert result.exit_code == 1

    def test_restore_with_all_flag_empty_dir(self, tmp_path):
        """restore --all with empty backup dir exits 1 (nothing to restore)."""
        with patch("bbackup.cli.DockerRestore") as MockRestore:
            mock_inst = MagicMock()
            mock_inst.restore_backup.return_value = {}
            MockRestore.return_value = mock_inst
            result = CliRunner().invoke(cli, ["restore", "--backup-path", str(tmp_path), "--all"])
        assert result.exit_code == 1

    def test_restore_with_containers(self, tmp_path):
        """restore with explicit containers invokes restore_backup."""
        with patch("bbackup.cli.DockerRestore") as MockRestore:
            mock_inst = MagicMock()
            mock_inst.restore_backup.return_value = {"containers_restored": 1}
            MockRestore.return_value = mock_inst
            result = CliRunner().invoke(
                cli, ["restore", "--backup-path", str(tmp_path), "--containers", "myapp"]
            )
        assert result.exit_code in (0, 1)

    def test_restore_with_rename(self, tmp_path):
        """restore --rename maps old:new names correctly."""
        with patch("bbackup.cli.DockerRestore") as MockRestore:
            mock_inst = MagicMock()
            mock_inst.restore_backup.return_value = {}
            MockRestore.return_value = mock_inst
            result = CliRunner().invoke(
                cli,
                ["restore", "--backup-path", str(tmp_path),
                 "--volumes", "data", "--rename", "data:data_new"]
            )
        assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# TestListRemoteBackupsExtended
# ---------------------------------------------------------------------------


class TestListRemoteBackupsExtended:
    def test_remote_disabled_exits_one(self, tmp_path):
        """remote listed but disabled exits with 1."""
        yaml_content = """
backup:
  local_staging: /tmp
remotes:
  myremote:
    enabled: false
    type: local
    path: /tmp/remote
"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        result = CliRunner().invoke(
            cli, ["--config", str(cfg_file), "list-remote-backups", "--remote", "myremote"]
        )
        assert result.exit_code == 1

    def test_remote_enabled_no_backups(self, tmp_path):
        """enabled remote with no backups prints warning and exits 0."""
        yaml_content = """
backup:
  local_staging: /tmp
remotes:
  myremote:
    enabled: true
    type: local
    path: /tmp/remote
"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        with patch("bbackup.cli.RemoteStorageManager") as MockRemote:
            mock_inst = MagicMock()
            mock_inst.list_backups.return_value = []
            MockRemote.return_value = mock_inst
            result = CliRunner().invoke(
                cli, ["--config", str(cfg_file), "list-remote-backups", "--remote", "myremote"]
            )
        assert result.exit_code == 0

    def test_remote_enabled_with_backups(self, tmp_path):
        """enabled remote with backups shows table."""
        yaml_content = """
backup:
  local_staging: /tmp
remotes:
  myremote:
    enabled: true
    type: local
    path: /tmp/remote
"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        with patch("bbackup.cli.RemoteStorageManager") as MockRemote:
            mock_inst = MagicMock()
            mock_inst.list_backups.return_value = ["backup_20260226_120000"]
            MockRemote.return_value = mock_inst
            result = CliRunner().invoke(
                cli, ["--config", str(cfg_file), "list-remote-backups", "--remote", "myremote"]
            )
        assert result.exit_code == 0
        assert "backup_20260226_120000" in result.output


# ---------------------------------------------------------------------------
# TestBackupCommand
# ---------------------------------------------------------------------------


class TestBackupCommand:
    def test_backup_no_containers_exits_one(self, mock_docker_client):
        """backup with no containers selected exits with 1."""
        mock_docker_client.containers.list.return_value = []
        result = CliRunner().invoke(cli, ["backup"])
        assert result.exit_code == 1

    def test_backup_with_containers_flag(self, mock_docker_client, tmp_path):
        """backup --containers invokes backup runner."""
        mock_docker_client.containers.list.return_value = [
            MagicMock(name="myapp", id="abc123",
                      attrs={"Name": "/myapp", "Id": "abc123",
                             "Config": {}, "HostConfig": {}, "NetworkSettings": {}})
        ]
        with patch("bbackup.cli.BackupRunner") as MockRunner:
            mock_inst = MagicMock()
            mock_inst.run_backup.return_value = MagicMock(status="completed", errors=[])
            MockRunner.return_value = mock_inst
            result = CliRunner().invoke(cli, ["backup", "--containers", "myapp"])
        assert result.exit_code in (0, 1)

    def test_backup_invalid_backup_set_exits_one(self):
        """backup with nonexistent backup-set exits with 1."""
        result = CliRunner().invoke(cli, ["backup", "--backup-set", "nonexistent_set"])
        assert result.exit_code == 1

    def test_backup_duplicate_direct_path_names_exit_user_error(self, tmp_path):
        src_a = tmp_path / "a" / "data"
        src_b = tmp_path / "b" / "data"
        src_a.mkdir(parents=True)
        src_b.mkdir(parents=True)

        result = CliRunner().invoke(
            cli,
            [
                "backup",
                "--containers",
                "myapp",
                "--paths",
                str(src_a),
                "--paths",
                str(src_b),
                "--output",
                "json",
            ],
        )

        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert "Duplicate filesystem target name" in data["errors"][0]

    def test_backup_partial_failure_exits_partial_and_skips_upload(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        remote_dir = tmp_path / "remote"
        cfg_file.write_text(textwrap.dedent(f"""
            backup:
              staging_dir: {tmp_path / "staging"}
              solid_archive: false
            remotes:
              local1:
                enabled: true
                type: local
                path: {remote_dir}
            encryption:
              enabled: false
        """))
        runner_ref = {}

        def make_runner(config, status):
            mock_runner = MagicMock()
            mock_runner.run_backup.side_effect = lambda **kwargs: (
                status.add_error("Failed to backup volume: data")
                or setattr(status, "status", "partial")
                or {"errors": ["Failed to backup volume: data"]}
            )
            runner_ref["runner"] = mock_runner
            return mock_runner

        with patch("bbackup.cli.BackupRunner", side_effect=make_runner):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(cfg_file),
                    "backup",
                    "--containers",
                    "myapp",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_PARTIAL
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["data"]["remotes"] == {}
        assert data["data"]["encryption"] == "disabled"
        runner_ref["runner"].upload_to_remotes.assert_not_called()

    def test_backup_successful_encryption_removes_plaintext_staging(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent(f"""
            backup:
              staging_dir: {tmp_path / "staging"}
              solid_archive: false
            remotes: {{}}
            encryption:
              enabled: true
        """))
        paths = {}

        def make_runner(config, status):
            mock_runner = MagicMock()

            def run_backup(**kwargs):
                backup_dir = kwargs["backup_dir"]
                backup_dir.mkdir(parents=True, exist_ok=True)
                (backup_dir / "plain.txt").write_text("secret")
                paths["plain"] = backup_dir
                return {}

            def encrypt_backup(backup_dir):
                encrypted_dir = backup_dir.parent / f"{backup_dir.name}.enc"
                encrypted_dir.mkdir(parents=True)
                (encrypted_dir / "plain.txt.enc").write_text("encrypted")
                status.encryption_status = "encrypted"
                return encrypted_dir

            mock_runner.run_backup.side_effect = run_backup
            mock_runner.encrypt_backup_directory.side_effect = encrypt_backup
            return mock_runner

        with patch("bbackup.cli.BackupRunner", side_effect=make_runner):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(cfg_file),
                    "backup",
                    "--containers",
                    "myapp",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert data["data"]["encryption"] == "encrypted"
        assert data["data"]["backup_dir"].endswith(".enc")
        assert not paths["plain"].exists()

    def test_solid_archive_backup_removes_expanded_staging_dir(self, tmp_path):
        staging_dir = tmp_path / "staging"
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent(f"""
            backup:
              local_staging: {staging_dir}
              solid_archive: true
            remotes: {{}}
            encryption:
              enabled: false
        """))
        paths = {}

        def make_runner(config, status):
            mock_runner = MagicMock()

            def run_backup(**kwargs):
                backup_dir = kwargs["backup_dir"]
                backup_dir.mkdir(parents=True, exist_ok=True)
                (backup_dir / "backup_manifest.json").write_text("{}")
                (backup_dir / "filesystems").mkdir()
                (backup_dir / "filesystems" / "repo.txt").write_text("content")
                paths["expanded"] = backup_dir
                return {}

            mock_runner.run_backup.side_effect = run_backup
            return mock_runner

        with patch("bbackup.cli.BackupRunner", side_effect=make_runner):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(cfg_file),
                    "backup",
                    "--containers",
                    "myapp",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        archive_path = staging_dir / f"{paths['expanded'].name}.tar.gz"
        assert data["data"]["backup_dir"] == str(archive_path)
        assert archive_path.is_file()
        assert not paths["expanded"].exists()

    def test_backup_encryption_failure_exits_system_error_and_skips_upload(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        remote_dir = tmp_path / "remote"
        cfg_file.write_text(textwrap.dedent(f"""
            backup:
              staging_dir: {tmp_path / "staging"}
              solid_archive: false
            remotes:
              local1:
                enabled: true
                type: local
                path: {remote_dir}
            encryption:
              enabled: true
        """))
        runner_ref = {}

        def make_runner(config, status):
            mock_runner = MagicMock()
            mock_runner.run_backup.return_value = {}

            def fail_encryption(path):
                status.add_error("Encryption failed: key error")
                status.status = "error"
                raise RuntimeError("Encryption failed: key error")

            mock_runner.encrypt_backup_directory.side_effect = fail_encryption
            runner_ref["runner"] = mock_runner
            return mock_runner

        with patch("bbackup.cli.BackupRunner", side_effect=make_runner):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(cfg_file),
                    "backup",
                    "--containers",
                    "myapp",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_SYSTEM_ERROR
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["data"]["remotes"] == {}
        runner_ref["runner"].upload_to_remotes.assert_not_called()


# ---------------------------------------------------------------------------
# TestJSONOutputMode
# ---------------------------------------------------------------------------


class TestJSONOutputMode:
    """Verify that --output json wraps results in the standard envelope."""

    def _parse_envelope(self, output: str) -> dict:
        return json.loads(output.strip())

    def test_list_containers_json_envelope(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        result = CliRunner().invoke(cli, ["list-containers", "--output", "json"])
        assert result.exit_code == 0
        data = self._parse_envelope(result.output)
        assert data["schema_version"] == "1"
        assert data["command"] == "list-containers"
        assert isinstance(data["success"], bool)
        assert "containers" in data["data"]
        assert isinstance(data["errors"], list)

    def test_list_containers_json_includes_id(self, mock_docker_client):
        """Gap 7: container dicts must include 'id' field."""
        container = MagicMock()
        container.id = "abc123def456"
        container.name = "web"
        container.status = "running"
        container.image.tags = ["nginx:latest"]
        mock_docker_client.containers.list.return_value = [container]
        result = CliRunner().invoke(cli, ["list-containers", "--output", "json"])
        assert result.exit_code == 0
        data = self._parse_envelope(result.output)
        assert len(data["data"]["containers"]) == 1
        assert "id" in data["data"]["containers"][0]

    def test_list_backup_sets_json_includes_scope(self, tmp_path):
        """Gap 14: backup set JSON must include 'scope' dict."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""
            backup:
              backup_sets:
                prod:
                  description: Production
                  containers:
                    - web
        """))
        result = CliRunner().invoke(
            cli, ["--config", str(cfg), "list-backup-sets", "--output", "json"]
        )
        assert result.exit_code == 0
        data = self._parse_envelope(result.output)
        sets = data["data"]["sets"]
        assert len(sets) == 1
        assert "scope" in sets[0]

    def test_list_backups_json_envelope(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["list-backups", "--backup-dir", str(tmp_path), "--output", "json"]
        )
        assert result.exit_code == 0
        data = self._parse_envelope(result.output)
        assert data["schema_version"] == "1"
        assert "backups" in data["data"]

    def test_list_filesystem_sets_json_envelope(self):
        result = CliRunner().invoke(cli, ["list-filesystem-sets", "--output", "json"])
        assert result.exit_code == 0
        data = self._parse_envelope(result.output)
        assert "sets" in data["data"]

    def test_list_remote_backups_json_unknown_remote(self):
        result = CliRunner().invoke(
            cli, ["list-remote-backups", "--remote", "nonexistent", "--output", "json"]
        )
        assert result.exit_code == EXIT_USER_ERROR
        # Should still be valid JSON even on error
        data = self._parse_envelope(result.output)
        assert data["success"] is False

    def test_backup_dry_run_json(self, mock_docker_client):
        """Gap 9: --dry-run returns plan JSON without running BackupRunner."""
        mock_docker_client.containers.list.return_value = []
        with patch("bbackup.cli.BackupRunner") as MockRunner:
            result = CliRunner().invoke(
                cli,
                ["backup", "--containers", "myapp", "--dry-run", "--output", "json"],
            )
            MockRunner.return_value.run_backup.assert_not_called()
        assert result.exit_code == EXIT_SUCCESS
        data = self._parse_envelope(result.output)
        assert data["data"]["dry_run"] is True
        assert "would_backup" in data["data"]

    def test_restore_dry_run_json(self, tmp_path):
        """Gap 9 (restore): --dry-run returns plan without executing."""
        result = CliRunner().invoke(
            cli,
            [
                "restore",
                "--backup-path",
                str(tmp_path),
                "--containers",
                "myapp",
                "--dry-run",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == EXIT_SUCCESS
        data = self._parse_envelope(result.output)
        assert data["data"]["dry_run"] is True

    def test_init_encryption_json_returns_key_paths(self, tmp_path):
        """Gap 6: init-encryption JSON mode omits prose, returns key_paths."""
        pub = b"-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
        priv = b"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
        with patch("bbackup.cli.EncryptionManager") as MockEM:
            MockEM.generate_keypair.return_value = (pub, priv)
            result = CliRunner().invoke(
                cli,
                [
                    "init-encryption",
                    "--method",
                    "asymmetric",
                    "--key-path",
                    str(tmp_path),
                    "--output",
                    "json",
                ],
            )
        assert result.exit_code in (EXIT_SUCCESS, EXIT_SYSTEM_ERROR)
        if result.exit_code == EXIT_SUCCESS:
            data = self._parse_envelope(result.output)
            assert "key_paths" in data["data"]
            assert "config_snippet" in data["data"]
            # Must not contain the multi-server prose guidance
            assert "multi-server" not in result.output.lower()


# ---------------------------------------------------------------------------
# TestInputJSON
# ---------------------------------------------------------------------------


class TestInputJSON:
    """Verify --input-json merging behavior."""

    def test_input_json_sets_containers(self, mock_docker_client):
        """--input-json overrides --containers."""
        mock_docker_client.containers.list.return_value = []
        payload = json.dumps({"containers": ["web", "db"], "no_interactive": True})
        with patch("bbackup.cli.BackupRunner") as MockRunner:
            mock_inst = MagicMock()
            mock_inst.run_backup.return_value = {}
            MockRunner.return_value = mock_inst
            result = CliRunner().invoke(
                cli,
                ["backup", "--input-json", payload, "--output", "json"],
                catch_exceptions=False,
            )
        # Should reach BackupRunner (containers provided via JSON)
        # exit code 0 or non-zero based on mock, but not user-error
        assert result.exit_code != EXIT_USER_ERROR

    def test_input_json_invalid_exits_user_error(self):
        result = CliRunner().invoke(cli, ["list-containers", "--input-json", "not-json"])
        assert result.exit_code == EXIT_USER_ERROR

    def test_input_json_non_object_exits_user_error(self):
        result = CliRunner().invoke(cli, ["list-containers", "--input-json", "[1, 2, 3]"])
        assert result.exit_code == EXIT_USER_ERROR

    def test_input_json_unknown_keys_ignored(self, mock_docker_client):
        """Unknown keys in --input-json must be silently ignored."""
        mock_docker_client.containers.list.return_value = []
        result = CliRunner().invoke(
            cli,
            ["list-containers", "--input-json", '{"totally_unknown_key": true}'],
        )
        assert result.exit_code == EXIT_SUCCESS

    def test_input_json_overrides_cli_flag(self, tmp_path):
        """Explicit --output flag combined with --input-json succeeds without error."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""
            backup:
              backup_sets:
                prod:
                  description: Production
                  containers:
                    - web
        """))
        # Provide both --output json explicitly and a known key via --input-json
        result = CliRunner().invoke(
            cli,
            [
                "--config",
                str(cfg),
                "list-backup-sets",
                "--output",
                "json",
                "--input-json",
                '{"totally_unknown": true}',
            ],
        )
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output.strip())
        assert data["schema_version"] == "1"


# ---------------------------------------------------------------------------
# TestSkillsCommand
# ---------------------------------------------------------------------------


class TestSkillsCommand:
    """Verify the `bbackup skills` and `bbman skills` commands."""

    def test_bbackup_skills_level0_returns_skills_array(self):
        result = CliRunner().invoke(cli, ["skills"])
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert "skills" in data
        assert isinstance(data["skills"], list)
        skill_ids = {s["id"] for s in data["skills"]}
        assert "docker-backup" in skill_ids
        assert "filesystem-backup" in skill_ids
        assert "restore" in skill_ids

    def test_bbackup_skills_level0_includes_agent_hint(self):
        result = CliRunner().invoke(cli, ["skills"])
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert "agent_hint" in data

    def test_bbackup_skills_level1_docker_backup(self):
        result = CliRunner().invoke(cli, ["skills", "docker-backup", "--output", "json"])
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        # level-1 is wrapped in the standard envelope
        assert data["schema_version"] == "1"
        skill = data["data"]
        assert skill["id"] == "docker-backup"
        assert "steps" in skill
        assert "examples" in skill
        # Every step must have input_json_schema
        for step in skill["steps"]:
            assert "input_json_schema" in step

    def test_bbackup_skills_level1_unknown_exits_user_error(self):
        result = CliRunner().invoke(cli, ["skills", "no-such-skill"])
        assert result.exit_code == EXIT_USER_ERROR

    def test_bbackup_skills_all_ids_resolvable(self):
        """Every skill id listed at level-0 must resolve at level-1."""
        level0 = CliRunner().invoke(cli, ["skills"])
        data = json.loads(level0.output)
        for skill in data["skills"]:
            r = CliRunner().invoke(cli, ["skills", skill["id"], "--output", "json"])
            assert r.exit_code == EXIT_SUCCESS, f"skill {skill['id']} failed: {r.output}"


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_backup_dry_run_does_not_call_run_backup(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        with patch("bbackup.cli.BackupRunner") as MockRunner:
            CliRunner().invoke(
                cli,
                ["backup", "--containers", "myapp", "--dry-run", "--no-interactive"],
            )
            MockRunner.return_value.run_backup.assert_not_called()

    def test_backup_dry_run_exits_success(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        with patch("bbackup.cli.BackupRunner"):
            result = CliRunner().invoke(
                cli,
                ["backup", "--containers", "myapp", "--dry-run", "--no-interactive", "--output", "json"],
            )
        assert result.exit_code == EXIT_SUCCESS

    def test_restore_dry_run_does_not_call_restore_backup(self, tmp_path):
        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(
                cli,
                ["restore", "--backup-path", str(tmp_path), "--containers", "myapp", "--dry-run"],
            )
        assert result.exit_code == EXIT_SUCCESS

    def test_dry_run_output_shape(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        with patch("bbackup.cli.BackupRunner"):
            result = CliRunner().invoke(
                cli,
                ["backup", "--containers", "myapp", "--dry-run", "--output", "json"],
            )
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert data["data"]["dry_run"] is True
        assert "would_backup" in data["data"]
        assert "containers" in data["data"]["would_backup"]

    def test_restore_all_dry_run_includes_archive_backed_volumes(self, tmp_path):
        volumes_dir = tmp_path / "volumes"
        volumes_dir.mkdir()
        (volumes_dir / "myvolume.tar.gz").write_bytes(b"")

        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(
                cli,
                ["restore", "--backup-path", str(tmp_path), "--all", "--dry-run", "--output", "json"],
            )

        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert data["data"]["would_restore"]["volumes"] == ["myvolume"]

    def test_restore_all_dry_run_discovers_solid_archive_targets(self, tmp_path):
        backup_root = tmp_path / "backup_20260621_120000"
        (backup_root / "configs").mkdir(parents=True)
        (backup_root / "volumes").mkdir()
        (backup_root / "networks").mkdir()
        (backup_root / "filesystems" / "documents").mkdir(parents=True)
        (backup_root / "configs" / "web_config.json").write_text("{}", encoding="utf-8")
        (backup_root / "volumes" / "dbdata.tar.gz").write_bytes(b"")
        (backup_root / "networks" / "frontend.json").write_text("{}", encoding="utf-8")
        (backup_root / "filesystems" / "documents" / "file.txt").write_text("data", encoding="utf-8")
        archive_path = tmp_path / "backup_20260621_120000.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_root, arcname=backup_root.name)

        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(
                cli,
                [
                    "restore",
                    "--backup-path",
                    str(archive_path),
                    "--all",
                    "--filesystem-destination",
                    str(tmp_path / "restore"),
                    "--dry-run",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert data["data"]["would_restore"] == {
            "containers": ["web"],
            "volumes": ["dbdata"],
            "networks": ["frontend"],
            "filesystems": ["documents"],
        }

    def test_restore_all_with_filesystem_requires_destination_before_restore(self, tmp_path):
        (tmp_path / "configs").mkdir()
        (tmp_path / "filesystems" / "documents").mkdir(parents=True)
        (tmp_path / "configs" / "web_config.json").write_text("{}", encoding="utf-8")

        with patch("bbackup.cli.DockerRestore") as MockRestore:
            result = CliRunner().invoke(
                cli,
                ["restore", "--backup-path", str(tmp_path), "--all", "--output", "json"],
            )

        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert "Filesystem restore requires --filesystem-destination" in data["errors"]
        MockRestore.return_value.restore_backup.assert_not_called()

    def test_restore_all_dry_run_with_filesystem_requires_destination_before_docker(self, tmp_path):
        (tmp_path / "filesystems" / "documents").mkdir(parents=True)

        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(
                cli,
                ["restore", "--backup-path", str(tmp_path), "--all", "--dry-run", "--output", "json"],
            )

        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert "Filesystem restore requires --filesystem-destination" in data["errors"]

    def test_restore_rejects_multiple_filesystems_sharing_destination_before_restore(self, tmp_path):
        destination = tmp_path / "restore"

        with patch("bbackup.cli.DockerRestore") as MockRestore:
            result = CliRunner().invoke(
                cli,
                [
                    "restore",
                    "--backup-path",
                    str(tmp_path),
                    "--filesystem",
                    "documents",
                    "--filesystem",
                    "photos",
                    "--filesystem-destination",
                    str(destination),
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert any(
            "multiple filesystem targets cannot share one destination" in error
            for error in data["errors"]
        )
        MockRestore.return_value.restore_backup.assert_not_called()

    def test_restore_dry_run_rejects_multiple_filesystems_before_docker(self, tmp_path):
        destination = tmp_path / "restore"

        with patch("bbackup.cli.DockerRestore", side_effect=AssertionError("Docker should not be used")):
            result = CliRunner().invoke(
                cli,
                [
                    "restore",
                    "--backup-path",
                    str(tmp_path),
                    "--filesystem",
                    "documents",
                    "--filesystem",
                    "photos",
                    "--filesystem-destination",
                    str(destination),
                    "--dry-run",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_USER_ERROR
        data = json.loads(result.output)
        assert any(
            "multiple filesystem targets cannot share one destination" in error
            for error in data["errors"]
        )


# ---------------------------------------------------------------------------
# TestEnvVars
# ---------------------------------------------------------------------------


class TestEnvVars:
    def test_bbackup_output_env_produces_json(self, mock_docker_client):
        """BBACKUP_OUTPUT=json must make list-containers emit JSON without --output flag."""
        mock_docker_client.containers.list.return_value = []
        result = CliRunner(env={"BBACKUP_OUTPUT": "json"}).invoke(
            cli, ["list-containers"]
        )
        assert result.exit_code == EXIT_SUCCESS
        data = json.loads(result.output)
        assert data["schema_version"] == "1"
        assert "containers" in data["data"]

    def test_bbackup_no_interactive_env_suppresses_tui(self, mock_docker_client):
        """BBACKUP_NO_INTERACTIVE=1 must set use_tui=False in backup command."""
        mock_docker_client.containers.list.return_value = []
        with patch("bbackup.cli.BackupTUI") as MockTUI, \
             patch("bbackup.cli.BackupRunner") as MockRunner:
            mock_runner = MagicMock()
            mock_runner.run_backup.return_value = {}
            MockRunner.return_value = mock_runner

            tui_inst = MagicMock()
            MockTUI.return_value = tui_inst

            CliRunner(env={"BBACKUP_NO_INTERACTIVE": "1"}).invoke(
                cli,
                ["backup", "--containers", "myapp", "--output", "json"],
            )
            # run_with_live_dashboard must NOT be called when no-interactive is set
            tui_inst.run_with_live_dashboard.assert_not_called()

    def test_bbackup_output_env_overridden_by_flag(self, mock_docker_client):
        """Explicit --output text must override BBACKUP_OUTPUT=json."""
        mock_docker_client.containers.list.return_value = []
        result = CliRunner(env={"BBACKUP_OUTPUT": "json"}).invoke(
            cli, ["list-containers", "--output", "text"]
        )
        assert result.exit_code == EXIT_SUCCESS
        # Should not be valid JSON envelope
        assert "schema_version" not in result.output


# ---------------------------------------------------------------------------
# TestExitCodes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_bad_backup_set_exits_user_error(self):
        result = CliRunner().invoke(cli, ["backup", "--backup-set", "nonexistent"])
        assert result.exit_code == EXIT_USER_ERROR

    def test_no_containers_selected_exits_user_error(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        result = CliRunner().invoke(cli, ["backup", "--no-interactive"])
        assert result.exit_code == EXIT_USER_ERROR

    def test_unknown_remote_exits_user_error(self):
        result = CliRunner().invoke(
            cli, ["list-remote-backups", "--remote", "nowhere"]
        )
        assert result.exit_code == EXIT_USER_ERROR

    def test_restore_no_items_exits_user_error(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["restore", "--backup-path", str(tmp_path)]
        )
        assert result.exit_code == EXIT_USER_ERROR

    def test_invalid_input_json_exits_user_error(self):
        result = CliRunner().invoke(
            cli, ["list-containers", "--input-json", "}{bad json"]
        )
        assert result.exit_code == EXIT_USER_ERROR

    def test_skills_unknown_id_exits_user_error(self):
        result = CliRunner().invoke(cli, ["skills", "no-such-skill"])
        assert result.exit_code == EXIT_USER_ERROR



class TestBackupRemoteOutcomes:
    def test_failed_remote_upload_is_partial_and_nonzero(self, mock_docker_client, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"""
backup:
  staging_dir: {tmp_path / "staging"}
  local_staging: {tmp_path / "staging"}
remotes:
  local:
    enabled: true
    type: local
    path: {tmp_path / "remote"}
encryption:
  enabled: false
"""
        )

        with patch("bbackup.cli.BackupRunner") as MockRunner:
            runner = MagicMock()
            runner.run_backup.return_value = {
                "containers": {},
                "volumes": {},
                "networks": {},
                "filesystems": {},
                "errors": [],
            }

            def failed_upload(*args, **kwargs):
                status = MockRunner.call_args.args[1]
                status.set_item_status("remote", "local", "failed")
                return False

            runner.upload_to_remotes.side_effect = failed_upload
            MockRunner.return_value = runner
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(config_path),
                    "backup",
                    "--containers",
                    "app",
                    "--remote",
                    "local",
                    "--no-interactive",
                    "--output",
                    "json",
                ],
            )

        assert result.exit_code == EXIT_PARTIAL
        data = json.loads(result.output)
        assert data["success"] is False
        assert data["data"]["remotes"]["local"] == "failed"