"""
Tests for bbackup.config, bbackup.__init__, and bbackup.logging.
Purpose: Covers all dataclass defaults, Config load paths, retention parsing,
         encryption parsing, remote parsing, and setup_logging coverage.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import logging
import textwrap

import pytest

import bbackup
from bbackup.config import (
    BackupScope,
    BackupSet,
    Config,
    EncryptionSettings,
    get_effective_rclone_options,
    IncrementalSettings,
    RcloneOptions,
    RemoteStorage,
    RetentionPolicy,
    ensure_private_directory,
)
from bbackup.logging import setup_logging


def test_private_directory_rejects_writable_preexisting_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(OSError, match="untrusted ancestor"):
        ensure_private_directory(parent / "backup" / "child")
    assert not (parent / "backup").exists()


# ---------------------------------------------------------------------------
# TestConfigDefaults
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_backup_scope_defaults(self):
        s = BackupScope()
        assert s.containers is True
        assert s.volumes is True
        assert s.networks is True
        assert s.configs is True

    def test_retention_policy_defaults(self):
        r = RetentionPolicy()
        assert r.daily == 7
        assert r.weekly == 4
        assert r.monthly == 12
        assert r.max_storage_gb == 0
        assert r.warning_threshold_percent == 80
        assert r.cleanup_threshold_percent == 90
        assert r.cleanup_strategy == "oldest_first"

    def test_incremental_settings_defaults(self):
        i = IncrementalSettings()
        assert i.enabled is True
        assert i.use_link_dest is True
        assert i.min_file_size == 1048576

    def test_encryption_settings_defaults(self):
        e = EncryptionSettings()
        assert e.enabled is False
        assert e.method == "symmetric"
        assert e.symmetric == {}
        assert e.asymmetric == {}
        assert e.encrypt_volumes is True

    def test_remote_storage_defaults(self):
        r = RemoteStorage(name="test")
        assert r.enabled is False
        assert r.type == "local"
        assert r.port == 22
        assert r.host is None
        assert r.key_file is None
        assert r.remote_name is None

    def test_backup_set_defaults(self):
        bs = BackupSet(name="myset")
        assert bs.description == ""
        assert bs.containers == []

    def test_init_version_exists(self):
        assert hasattr(bbackup, "__version__")
        assert isinstance(bbackup.__version__, str)
        assert len(bbackup.__version__) > 0


# ---------------------------------------------------------------------------
# TestConfigLoad
# ---------------------------------------------------------------------------


class TestConfigLoad:
    def test_load_from_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            retention:
              daily: 5
              weekly: 2
              monthly: 6
            encryption:
              enabled: true
              method: symmetric
        """))
        cfg = Config(config_path=str(cfg_file))
        assert cfg.retention.daily == 5
        assert cfg.retention.weekly == 2
        assert cfg.encryption.enabled is True
        assert cfg.encryption.method == "symmetric"

    def test_partial_yaml_fills_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("retention:\n  daily: 3\n")
        cfg = Config(config_path=str(cfg_file))
        assert cfg.retention.daily == 3
        assert cfg.retention.weekly == 4  # default

    def test_malformed_yaml_raises_value_error(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("key: [unclosed bracket\n")
        with pytest.raises(ValueError):
            Config(config_path=str(cfg_file))

    def test_missing_file_path_loads_defaults(self, tmp_path):
        # Config doesn't raise on missing file path; it just calls _load_defaults
        cfg = Config(config_path=str(tmp_path / "nonexistent.yaml"))
        assert cfg.retention.daily == 7  # default

    def test_enabled_remotes_filter(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            remotes:
              r1:
                enabled: true
                type: local
                path: /tmp/r1
              r2:
                enabled: false
                type: local
                path: /tmp/r2
        """))
        cfg = Config(config_path=str(cfg_file))
        enabled = cfg.get_enabled_remotes()
        assert len(enabled) == 1
        assert enabled[0].name == "r1"

    def test_get_backup_set_returns_named_set(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            backup:
              backup_sets:
                myset:
                  description: My test set
                  containers:
                    - web
        """))
        cfg = Config(config_path=str(cfg_file))
        bs = cfg.get_backup_set("myset")
        assert bs is not None
        assert bs.name == "myset"
        assert "web" in bs.containers

    def test_get_backup_set_unknown_returns_none(self, tmp_path):
        cfg = Config(config_path=None)
        assert cfg.get_backup_set("doesnotexist") is None

    def test_get_staging_dir_default(self):
        cfg = Config(config_path=None)
        staging = cfg.get_staging_dir()
        assert isinstance(staging, str)
        assert len(staging) > 0

    def test_incremental_parsed_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("incremental:\n  enabled: false\n  use_link_dest: false\n")
        cfg = Config(config_path=str(cfg_file))
        assert cfg.incremental.enabled is False
        assert cfg.incremental.use_link_dest is False

    def test_backup_scope_parsed_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            backup:
              default_scope:
                containers: true
                volumes: false
                networks: false
                configs: true
        """))
        cfg = Config(config_path=str(cfg_file))
        assert cfg.scope.volumes is False
        assert cfg.scope.networks is False

    def test_remote_sftp_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            remotes:
              mysftp:
                enabled: true
                type: sftp
                host: backup.example.com
                port: 2222
                user: backupuser
                key_file: /home/user/.ssh/id_rsa
        """))
        cfg = Config(config_path=str(cfg_file))
        r = cfg.remotes["mysftp"]
        assert r.type == "sftp"
        assert r.host == "backup.example.com"
        assert r.port == 2222
        assert r.user == "backupuser"

    def test_remote_rclone_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            remotes:
              myrclone:
                enabled: true
                type: rclone
                remote_name: myremote
        """))
        cfg = Config(config_path=str(cfg_file))
        r = cfg.remotes["myrclone"]
        assert r.type == "rclone"
        assert r.remote_name == "myremote"

    def test_remote_rclone_options_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            remotes:
              myrclone:
                enabled: true
                type: rclone
                remote_name: myremote
                rclone_options:
                  transfers: 16
                  checkers: 4
        """))
        cfg = Config(config_path=str(cfg_file))
        r = cfg.remotes["myrclone"]
        assert r.rclone_options is not None
        assert r.rclone_options.transfers == 16
        assert r.rclone_options.checkers == 4

    def test_rclone_default_options_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            rclone:
              default_options:
                transfers: 8
                checkers: 8
            remotes:
              local:
                enabled: true
                type: local
                path: /tmp/backups
        """))
        cfg = Config(config_path=str(cfg_file))
        assert cfg.rclone_default_options is not None
        assert cfg.rclone_default_options.transfers == 8

    def test_solid_archive_parsed(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            backup:
              solid_archive: true
        """))
        cfg = Config(config_path=str(cfg_file))
        assert cfg.solid_archive is True

    def test_solid_archive_default_false(self):
        cfg = Config(config_path=None)
        assert cfg.solid_archive is False

    def test_duplicate_filesystem_target_names_raise(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            filesystem:
              local:
                targets:
                  - name: data
                    path: /srv/data
                  - name: data
                    path: /opt/data
        """))

        with pytest.raises(ValueError, match="Duplicate filesystem target name"):
            Config(config_path=str(cfg_file))

    def test_get_backup_compression_returns_dict_with_defaults(self):
        cfg = Config(config_path=None)
        comp = cfg.get_backup_compression()
        assert comp["enabled"] is True
        assert comp["level"] == 6
        assert comp["format"] == "gzip"

    def test_get_backup_compression_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            backup:
              compression:
                enabled: false
                level: 3
                format: bzip2
        """))
        cfg = Config(config_path=str(cfg_file))
        comp = cfg.get_backup_compression()
        assert comp["enabled"] is False
        assert comp["level"] == 3
        assert comp["format"] == "bzip2"


# ---------------------------------------------------------------------------
# TestGetEffectiveRcloneOptions
# ---------------------------------------------------------------------------


class TestGetEffectiveRcloneOptions:
    def test_per_remote_overrides_global(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            rclone:
              default_options:
                transfers: 4
                checkers: 4
            remotes:
              r1:
                enabled: true
                type: rclone
                remote_name: r1
                rclone_options:
                  transfers: 16
                  checkers: 8
        """))
        cfg = Config(config_path=str(cfg_file))
        remote = cfg.remotes["r1"]
        opts = get_effective_rclone_options(cfg, remote)
        assert opts.transfers == 16
        assert opts.checkers == 8

    def test_global_used_when_no_per_remote(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            rclone:
              default_options:
                transfers: 12
                checkers: 6
            remotes:
              r1:
                enabled: true
                type: rclone
                remote_name: r1
        """))
        cfg = Config(config_path=str(cfg_file))
        remote = cfg.remotes["r1"]
        opts = get_effective_rclone_options(cfg, remote)
        assert opts.transfers == 12
        assert opts.checkers == 6

    def test_builtin_default_when_nothing_set(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            remotes:
              r1:
                enabled: true
                type: rclone
                remote_name: r1
        """))
        cfg = Config(config_path=str(cfg_file))
        remote = cfg.remotes["r1"]
        opts = get_effective_rclone_options(cfg, remote)
        assert opts.transfers == RcloneOptions().transfers
        assert opts.checkers == RcloneOptions().checkers


# ---------------------------------------------------------------------------
# TestSetupLogging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def setup_method(self):
        # Remove all handlers from the 'bbackup' logger before each test
        # to prevent handler accumulation across tests
        logger = logging.getLogger("bbackup")
        logger.handlers.clear()

    def test_setup_logging_no_config(self, tmp_path):
        """setup_logging with no logging section should not raise."""
        cfg = Config(config_path=None)
        # Override log file to tmp_path to avoid writing to home dir in tests
        cfg.data["logging"] = {
            "level": "INFO",
            "file": str(tmp_path / "bbackup.log"),
        }
        logger = setup_logging(cfg)
        assert logger is not None

    def test_setup_logging_with_debug_level(self, tmp_path):
        """setup_logging sets correct log level from config."""
        cfg = Config(config_path=None)
        cfg.data["logging"] = {
            "level": "DEBUG",
            "file": str(tmp_path / "bbackup.log"),
        }
        logger = setup_logging(cfg)
        assert logger.level == logging.DEBUG

    def test_setup_logging_creates_log_file(self, tmp_path):
        """setup_logging creates the log file directory if missing."""
        log_file = tmp_path / "nested" / "dir" / "bbackup.log"
        cfg = Config(config_path=None)
        cfg.data["logging"] = {
            "level": "INFO",
            "file": str(log_file),
        }
        setup_logging(cfg)
        assert log_file.parent.exists()

    def test_setup_logging_does_not_duplicate_handlers(self, tmp_path):
        """Calling setup_logging twice does not duplicate handlers."""
        cfg = Config(config_path=None)
        cfg.data["logging"] = {
            "level": "INFO",
            "file": str(tmp_path / "bbackup.log"),
        }
        logger = setup_logging(cfg)
        handler_count = len(logger.handlers)
        # Second call should be a no-op because handlers already exist
        setup_logging(cfg)
        assert len(logger.handlers) == handler_count
