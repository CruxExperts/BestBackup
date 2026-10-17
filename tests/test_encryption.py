"""
Tests for bbackup.encryption - AES-GCM primitives, EncryptionManager construction,
GitHub shortcut resolution, key cache, PBKDF2, file/directory/backup encryption.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import os
import json
import stat
import tempfile
import base64
import hashlib
import hmac
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from bbackup.config import EncryptionSettings
from bbackup.encryption import EncryptionManager, TREE_FORMAT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_sym_config(key_file=None, method="symmetric", enabled=True):
    return EncryptionSettings(
        enabled=enabled,
        method=method,
        symmetric={"key_file": key_file} if key_file else {},
    )


def make_manager_with_key(tmp_path, key_bytes=None):
    """Create an EncryptionManager backed by a real key file."""
    key = key_bytes or os.urandom(32)
    key_file = tmp_path / "test.key"
    key_file.write_bytes(key)
    cfg = make_sym_config(key_file=str(key_file))
    mgr = EncryptionManager(cfg)
    return mgr, key


# ---------------------------------------------------------------------------
# TestAESGCMPrimitives
# ---------------------------------------------------------------------------


class TestAESGCMPrimitives:
    """Direct AESGCM primitive tests without EncryptionManager."""

    def test_roundtrip_preserves_data(self):
        key = os.urandom(32)
        iv = os.urandom(12)
        plaintext = b"Hello, world!"
        aesgcm = AESGCM(key)
        ct = aesgcm.encrypt(iv, plaintext, None)
        assert aesgcm.decrypt(iv, ct, None) == plaintext

    def test_nonce_uniqueness(self):
        key = os.urandom(32)
        plaintext = b"same plaintext"
        aesgcm = AESGCM(key)
        iv1 = os.urandom(12)
        iv2 = os.urandom(12)
        ct1 = aesgcm.encrypt(iv1, plaintext, None)
        ct2 = aesgcm.encrypt(iv2, plaintext, None)
        # Two encryptions of the same plaintext with different nonces produce different ciphertexts
        assert ct1 != ct2

    def test_wrong_key_raises(self):
        key = os.urandom(32)
        wrong_key = os.urandom(32)
        iv = os.urandom(12)
        aesgcm = AESGCM(key)
        ct = aesgcm.encrypt(iv, b"data", None)
        with pytest.raises(Exception):
            AESGCM(wrong_key).decrypt(iv, ct, None)

    def test_tampered_ciphertext_raises(self):
        key = os.urandom(32)
        iv = os.urandom(12)
        aesgcm = AESGCM(key)
        ct = bytearray(aesgcm.encrypt(iv, b"data", None))
        ct[0] ^= 0xFF  # flip bits
        with pytest.raises(Exception):
            aesgcm.decrypt(iv, bytes(ct), None)

    def test_key_exactly_32_bytes(self):
        key = b"A" * 32
        AESGCM(key)  # Should not raise

    def test_key_short_raises(self):
        key = b"short"
        with pytest.raises(Exception):
            AESGCM(key)


# ---------------------------------------------------------------------------
# TestEncryptionManagerConstruction
# ---------------------------------------------------------------------------


class TestEncryptionManagerConstruction:
    def test_disabled_config_no_key_loaded(self):
        cfg = EncryptionSettings(enabled=False, method="symmetric", symmetric={})
        mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is None

    def test_no_key_source_key_is_none(self):
        cfg = make_sym_config(key_file=None)
        mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is None

    def test_missing_key_file_key_is_none(self, tmp_path):
        cfg = make_sym_config(key_file=str(tmp_path / "nonexistent.key"))
        mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is None

    def test_valid_key_file_loaded(self, tmp_path):
        _, key = make_manager_with_key(tmp_path)
        mgr, _ = make_manager_with_key(tmp_path, key)
        assert mgr.symmetric_key == key

    def test_short_key_padded_to_32(self, tmp_path):
        short_key = b"short"
        key_file = tmp_path / "short.key"
        key_file.write_bytes(short_key)
        cfg = make_sym_config(key_file=str(key_file))
        mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is not None
        assert len(mgr.symmetric_key) == 32

    def test_long_key_truncated_to_32(self, tmp_path):
        long_key = b"A" * 64
        key_file = tmp_path / "long.key"
        key_file.write_bytes(long_key)
        cfg = make_sym_config(key_file=str(key_file))
        mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is not None
        assert len(mgr.symmetric_key) == 32
        assert mgr.symmetric_key == long_key[:32]


# ---------------------------------------------------------------------------
# TestGitHubShortcutResolution
# ---------------------------------------------------------------------------


class TestGitHubShortcutResolution:
    def test_github_prefix_triggers_head_calls(self, tmp_path):
        """github:user format tries standard repo URLs via requests.head."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser"},
        )
        with patch("bbackup.encryption.requests.head") as mock_head, \
             patch("bbackup.encryption.requests.get"):
            mock_head.return_value = MagicMock(status_code=200)
            EncryptionManager(cfg)
        mock_head.assert_called()

    def test_first_200_url_returned(self, tmp_path):
        """First URL returning 200 is returned as resolved URL."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser"},
        )
        with patch("bbackup.encryption.requests.head") as mock_head, \
             patch("bbackup.encryption.requests.get") as mock_get:
            mock_head.return_value = MagicMock(status_code=200)
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b"A" * 32,
                raise_for_status=MagicMock(),
            )
            EncryptionManager(cfg)
        # Verifies no exception was raised and head was called
        mock_head.assert_called()

    def test_username_only_does_not_probe_gist_names(self):
        """github:user does not try unsupported guessed Gist names."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser"},
        )

        checked_urls = []

        def head_side_effect(url, **kwargs):
            checked_urls.append(url)
            return MagicMock(status_code=404)

        with patch("bbackup.encryption.requests.head", side_effect=head_side_effect):
            mgr = EncryptionManager(cfg)

        assert mgr.symmetric_key is None
        assert checked_urls
        assert all(urlsplit(url).hostname != "gist.githubusercontent.com" for url in checked_urls)

    def test_all_404_ssh_fallback(self):
        """All standard URLs 404; SSH keys endpoint checked as fallback."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser"},
        )
        def head_side_effect(url, **kwargs):
            m = MagicMock()
            if "github.com/testuser.keys" in url:
                m.status_code = 200
            else:
                m.status_code = 404
            return m

        with patch("bbackup.encryption.requests.head", side_effect=head_side_effect), \
             patch("bbackup.encryption.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b"A" * 32,
                raise_for_status=MagicMock(),
            )
            EncryptionManager(cfg)
        # No assertion on key value; just verifying it runs without exception

    def test_all_request_exceptions_returns_none(self):
        """All requests.head calls raise RequestException -> key is None."""
        from requests import RequestException
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser"},
        )
        with patch("bbackup.encryption.requests.head", side_effect=RequestException("network error")):
            mgr = EncryptionManager(cfg)
        assert mgr.symmetric_key is None

    def test_explicit_gist_format_no_head_calls(self):
        """github:user/gist:ID format constructs URL directly, no head calls."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser/gist:abc123"},
        )
        with patch("bbackup.encryption.requests.head") as mock_head, \
             patch("bbackup.encryption.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b"A" * 32,
                raise_for_status=MagicMock(),
            )
            EncryptionManager(cfg)
        mock_head.assert_not_called()

    def test_explicit_repo_format_no_head_calls(self):
        """github:user/repo:NAME format constructs URL directly, no head calls."""
        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": "github:testuser/repo:mykeys"},
        )
        with patch("bbackup.encryption.requests.head") as mock_head, \
             patch("bbackup.encryption.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b"A" * 32,
                raise_for_status=MagicMock(),
            )
            EncryptionManager(cfg)
        mock_head.assert_not_called()


# ---------------------------------------------------------------------------
# TestFetchKeyFromUrl
# ---------------------------------------------------------------------------


class TestFetchKeyFromUrl:
    def _make_mgr(self, tmp_path):
        """Create manager without auto-loading key (disabled config)."""
        cfg = EncryptionSettings(enabled=False, method="symmetric", symmetric={})
        mgr = EncryptionManager(cfg)
        return mgr

    def test_200_returns_bytes(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        with patch("bbackup.encryption.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b"key_data_here",
                raise_for_status=MagicMock(),
            )
            with patch.object(mgr, "_cache_key"):
                result = mgr._fetch_key_from_url("https://example.com/key.pem")
        assert result == b"key_data_here"

    def test_http_url_raises_value_error(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        with pytest.raises(ValueError, match="Only HTTPS"):
            mgr._fetch_key_from_url("http://insecure.com/key.pem")

    def test_request_exception_falls_back_to_cache(self, tmp_path):
        from requests import RequestException
        mgr = self._make_mgr(tmp_path)
        cached = b"cached_key_data"
        with patch("bbackup.encryption.requests.get", side_effect=RequestException("timeout")), \
             patch.object(mgr, "_load_cached_key", return_value=cached):
            result = mgr._fetch_key_from_url("https://example.com/key.pem")
        assert result == cached

    def test_request_exception_no_cache_raises(self, tmp_path):
        from requests import RequestException
        mgr = self._make_mgr(tmp_path)
        with patch("bbackup.encryption.requests.get", side_effect=RequestException("timeout")), \
             patch.object(mgr, "_load_cached_key", return_value=None):
            with pytest.raises(RequestException):
                mgr._fetch_key_from_url("https://example.com/key.pem")


# ---------------------------------------------------------------------------
# TestKeyCache
# ---------------------------------------------------------------------------


class TestKeyCache:
    def test_cache_write_and_read_roundtrip(self, tmp_path):
        cfg = EncryptionSettings(enabled=False, method="symmetric", symmetric={})
        mgr = EncryptionManager(cfg)
        url = "https://example.com/testkey.pem"
        data = b"my_secret_key_data"

        with patch("pathlib.Path.home", return_value=tmp_path):
            mgr._cache_key(url, data)
            result = mgr._load_cached_key(url)

        assert result == data

    def test_load_missing_cache_returns_none(self, tmp_path):
        cfg = EncryptionSettings(enabled=False, method="symmetric", symmetric={})
        mgr = EncryptionManager(cfg)
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = mgr._load_cached_key("https://notcached.example.com/key")
        assert result is None


# ---------------------------------------------------------------------------
# TestPBKDF2
# ---------------------------------------------------------------------------


class TestPBKDF2:
    def test_same_password_same_salt_same_key(self, tmp_path):
        """PBKDF2 is deterministic: same inputs produce same output."""
        salt = os.urandom(16)
        key_data = salt + os.urandom(16)  # first 16 bytes = salt
        key_file = tmp_path / "key.bin"
        key_file.write_bytes(key_data)

        cfg = EncryptionSettings(
            enabled=True,
            method="symmetric",
            symmetric={"key_file": str(key_file), "key_password": "mypassword"},
        )
        mgr1 = EncryptionManager(cfg)
        mgr2 = EncryptionManager(cfg)
        assert mgr1.symmetric_key == mgr2.symmetric_key

    def test_different_passwords_different_keys(self, tmp_path):
        salt_data = os.urandom(32)
        key_file = tmp_path / "key.bin"
        key_file.write_bytes(salt_data)

        cfg1 = EncryptionSettings(
            enabled=True, method="symmetric",
            symmetric={"key_file": str(key_file), "key_password": "pass1"},
        )
        cfg2 = EncryptionSettings(
            enabled=True, method="symmetric",
            symmetric={"key_file": str(key_file), "key_password": "pass2"},
        )
        mgr1 = EncryptionManager(cfg1)
        mgr2 = EncryptionManager(cfg2)
        assert mgr1.symmetric_key != mgr2.symmetric_key


# ---------------------------------------------------------------------------
# TestFileEncryption
# ---------------------------------------------------------------------------


class TestFileEncryption:
    def test_roundtrip_preserves_binary_content(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        src = tmp_path / "source.bin"
        original = os.urandom(1024)
        src.write_bytes(original)

        enc = tmp_path / "source.bin.enc"
        dec = tmp_path / "source.bin.dec"

        assert mgr.encrypt_file(src, enc) is True
        assert enc.exists()
        assert mgr.decrypt_file(enc, dec) is True
        assert dec.read_bytes() == original

    def test_encrypt_creates_different_output(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        src = tmp_path / "file.txt"
        src.write_text("hello world")
        enc = tmp_path / "file.txt.enc"
        mgr.encrypt_file(src, enc)
        assert enc.read_bytes() != src.read_bytes()


# ---------------------------------------------------------------------------
# TestDirectoryEncryption
# ---------------------------------------------------------------------------


class TestDirectoryEncryption:
    def test_roundtrip_3_file_tree(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)

        # Create 3-file tree
        src = tmp_path / "source"
        src.mkdir()
        (src / "file1.txt").write_text("content1")
        sub = src / "subdir"
        sub.mkdir()
        (sub / "file2.bin").write_bytes(b"\x00\x01\x02")
        (sub / "file3.txt").write_text("content3")

        enc_dir = tmp_path / "encrypted"
        dec_dir = tmp_path / "decrypted"

        assert mgr.encrypt_directory(src, enc_dir) is True
        assert (enc_dir / "encryption_metadata.json").exists()
        assert mgr.decrypt_directory(enc_dir, dec_dir) is True

        assert (dec_dir / "file1.txt").read_text() == "content1"
        assert (dec_dir / "subdir" / "file2.bin").read_bytes() == b"\x00\x01\x02"
        assert (dec_dir / "subdir" / "file3.txt").read_text() == "content3"

    def test_roundtrip_preserves_modes_ownership_timestamps_and_links(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        src = tmp_path / "source_tree"
        src.mkdir(mode=0o750)
        sub = src / "folder"
        sub.mkdir(mode=0o751)
        original = sub / "original.txt"
        original.write_text("linked data")
        original.chmod(0o640)
        hardlink = sub / "hardlink.txt"
        os.link(original, hardlink)
        symlink = sub / "symbolic.txt"
        symlink.symlink_to("original.txt")
        timestamp = 1_700_000_000_123_456_789
        os.utime(original, ns=(timestamp, timestamp))
        os.utime(sub, ns=(timestamp, timestamp))
        original_stat = original.stat()
        sub_stat = sub.stat()
        symlink_stat = symlink.lstat()

        encrypted = tmp_path / "encrypted_tree"
        decrypted = tmp_path / "decrypted_tree"
        assert mgr.encrypt_directory(src, encrypted) is True
        assert mgr.decrypt_directory(encrypted, decrypted) is True

        restored = decrypted / "folder" / "original.txt"
        restored_hardlink = decrypted / "folder" / "hardlink.txt"
        restored_link = decrypted / "folder" / "symbolic.txt"
        restored_stat = restored.stat()
        assert stat.S_IMODE((decrypted / "folder").stat().st_mode) == stat.S_IMODE(sub_stat.st_mode)
        assert stat.S_IMODE(restored_stat.st_mode) == stat.S_IMODE(original_stat.st_mode)
        assert (restored_stat.st_uid, restored_stat.st_gid) == (original_stat.st_uid, original_stat.st_gid)
        assert restored_stat.st_atime_ns == original_stat.st_atime_ns
        assert restored_stat.st_mtime_ns == original_stat.st_mtime_ns
        assert restored_stat.st_ino == restored_hardlink.stat().st_ino
        assert restored_link.is_symlink()
        assert os.readlink(restored_link) == "original.txt"
        assert restored_link.lstat().st_mtime_ns == symlink_stat.st_mtime_ns

    def test_authenticated_tree_inventory_cannot_be_stripped(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("secret")
        encrypted = tmp_path / "backup.enc"
        assert mgr.encrypt_directory(source, encrypted) is True
        (encrypted / "encryption_metadata.json").unlink()
        payload = next((encrypted / "payloads").glob("*.bbackup"))
        payload.write_bytes(payload.read_bytes()[8:])

        destination = tmp_path / "decrypted"
        assert mgr.decrypt_directory(encrypted, destination) is False
        assert not destination.exists()
        with pytest.raises(RuntimeError, match="inventory is missing"):
            mgr.decrypt_backup(encrypted)

    def test_empty_tree_identity_prevents_inventory_stripping_downgrade(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "empty-source"
        source.mkdir()
        encrypted = tmp_path / "empty-backup"
        assert mgr.encrypt_directory(source, encrypted) is True

        (encrypted / "encryption_metadata.json").unlink()
        with pytest.raises(RuntimeError, match="inventory is missing"):
            mgr.decrypt_backup(encrypted)

    def test_v3_inventory_and_payload_names_hide_source_metadata(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "private-record-name.txt").write_text("secret")
        (source / "private-link-name").symlink_to("private-record-name.txt")
        encrypted = tmp_path / "backup.enc"
        assert mgr.encrypt_directory(source, encrypted)

        public = (encrypted / "encryption_metadata.json").read_text()
        names = "\n".join(path.relative_to(encrypted).as_posix() for path in encrypted.rglob("*"))
        for sensitive in ("private-record-name", "private-link-name", "target", '"uid"', '"gid"'):
            assert sensitive not in public
            assert sensitive not in names
        assert json.loads(public)["version"] == 3
        assert "entries" not in json.loads(public)
        restored = tmp_path / "restored"
        assert mgr.decrypt_directory(encrypted, restored)
        assert (restored / "private-record-name.txt").read_text() == "secret"
        assert (restored / "private-link-name").is_symlink()

        (encrypted / "payloads" / ".inventory-v3").unlink()
        assert not mgr.decrypt_directory(encrypted, tmp_path / "missing-inventory")
        assert not (tmp_path / "missing-inventory").exists()

    def test_authenticated_v2_tree_remains_readable_with_warning(self, tmp_path, caplog):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("old format")
        entries = mgr.validate_directory_tree(source)
        encrypted = tmp_path / "v2.enc"
        payloads = encrypted / "payloads"
        payloads.mkdir(parents=True, mode=0o700)
        encrypted.chmod(0o700)
        salt = os.urandom(16)
        key = mgr.symmetric_key
        mac_key = mgr._tree_mac_key(key, salt, 2)
        marker = b"BBAKTR2\0" + hmac.new(mac_key, b"bbackup-tree-payload-identity-v2", hashlib.sha256).digest()
        (payloads / ".bbackup-tree-v2").write_bytes(marker)
        entry = next(entry for entry in entries if entry["type"] == "file")
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        nonce = os.urandom(12)
        payload = b"BBAKTR2\0" + nonce + AESGCM(key).encrypt(nonce, b"old format", canonical)
        (payloads / "file.txt.bbackup").write_bytes(payload)
        metadata = {
            "format": TREE_FORMAT, "version": 2, "algorithm": "aes-256-gcm",
            "salt": base64.b64encode(salt).decode(), "wrapped_key": None, "entries": entries,
        }
        metadata["auth"] = mgr._tree_metadata_auth(metadata, mac_key)
        (encrypted / "encryption_metadata.json").write_text(json.dumps(metadata))
        for file in (payloads.iterdir()):
            file.chmod(0o600)
        (encrypted / "encryption_metadata.json").chmod(0o600)

        output = tmp_path / "restored"
        assert mgr.decrypt_directory(encrypted, output)
        assert (output / "file.txt").read_text() == "old format"
        assert "v2 encrypted tree" in caplog.text

    def test_extra_payload_rejected_and_partial_output_removed(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("secret")
        encrypted = tmp_path / "encrypted"
        assert mgr.encrypt_directory(source, encrypted) is True
        unexpected = encrypted / "payloads" / "unexpected.bbackup"
        unexpected.write_bytes(b"extra")

        destination = tmp_path / "decrypted"
        assert mgr.decrypt_directory(encrypted, destination) is False
        assert not destination.exists()

    def test_wrong_key_and_tampered_payload_fail_without_leaving_plaintext(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("secret")
        encrypted = tmp_path / "encrypted"
        assert mgr.encrypt_directory(source, encrypted) is True

        wrong_key_file = tmp_path / "wrong.key"
        wrong_key_file.write_bytes(os.urandom(32))
        wrong_mgr = EncryptionManager(make_sym_config(key_file=str(wrong_key_file)))
        created_temps = []
        original_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(*args, **kwargs):
            path = original_mkdtemp(*args, **kwargs)
            created_temps.append(path)
            return path

        with patch("bbackup.encryption.tempfile.mkdtemp", side_effect=track_mkdtemp):
            with pytest.raises(RuntimeError, match="authenticated"):
                wrong_mgr.decrypt_backup(encrypted)
        assert created_temps and not os.path.exists(created_temps[0])

        payload = next((encrypted / "payloads").glob("*.bbackup"))
        payload.write_bytes(payload.read_bytes()[:-1] + b"x")
        destination = tmp_path / "decrypted"
        assert mgr.decrypt_directory(encrypted, destination) is False
        assert not destination.exists()

    def test_partial_legacy_directory_is_rejected(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        legacy = tmp_path / "legacy.enc"
        legacy.mkdir()
        legacy.chmod(0o700)
        (legacy / "encryption_metadata.json").write_text(json.dumps({"encrypted": True}))
        (legacy / "plain.txt").write_text("partial plaintext")
        destination = tmp_path / "decrypted"

        assert mgr.decrypt_directory(legacy, destination) is False
        assert not destination.exists()

    def test_failed_decryption_preserves_preexisting_destination(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        encrypted = tmp_path / "broken.enc"
        encrypted.mkdir()
        (encrypted / "encryption_metadata.json").write_text(json.dumps({"encrypted": True}))
        (encrypted / "broken.txt.enc").write_bytes(b"malformed")
        destination = tmp_path / "existing-output"
        destination.mkdir(mode=0o700)
        sentinel = destination / "keep.txt"
        sentinel.write_text("leave in place")

        assert mgr.decrypt_directory(encrypted, destination) is False
        assert sentinel.read_text() == "leave in place"

    def test_special_files_fail_before_output_creation(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            pytest.skip("named pipes are unavailable")
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        os.mkfifo(source / "pipe")
        encrypted = tmp_path / "encrypted"

        assert mgr.encrypt_directory(source, encrypted) is False
        assert not encrypted.exists()

    def test_legacy_per_file_directory_still_decrypts(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "message.txt").write_text("legacy content")
        legacy = tmp_path / "legacy.enc"
        legacy.mkdir()
        legacy.chmod(0o700)
        assert mgr.encrypt_file(source / "message.txt", legacy / "message.txt.enc") is True
        (legacy / "encryption_metadata.json").write_text(json.dumps({"encrypted": True, "method": "symmetric"}))

        restored = tmp_path / "legacy-restored"
        assert mgr.decrypt_directory(legacy, restored) is True
        assert (restored / "message.txt").read_text() == "legacy content"

    @pytest.mark.skipif(not hasattr(os, "setxattr"), reason="extended attributes are unavailable")
    def test_unsupported_extended_attributes_fail_before_encrypted_output(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        item = source / "with-xattr"
        item.write_text("data")
        try:
            os.setxattr(item, "user.bbackup-test", b"unsupported")
        except OSError as exc:
            pytest.skip(f"extended attributes unavailable on test filesystem: {exc}")

        encrypted = tmp_path / "encrypted"
        assert mgr.encrypt_directory(source, encrypted) is False
        assert not encrypted.exists()
        assert item.read_text() == "data"

    def test_encrypted_outputs_and_decryption_stage_are_private(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "secret.txt").write_text("secret")
        encrypted = tmp_path / "encrypted"
        assert mgr.encrypt_directory(source, encrypted) is True
        assert stat.S_IMODE(encrypted.stat().st_mode) == 0o700
        assert stat.S_IMODE((encrypted / "encryption_metadata.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(next((encrypted / "payloads").glob("*.bbackup")).stat().st_mode) == 0o600

        decrypted = mgr.decrypt_backup(encrypted)
        private_wrapper = decrypted.parent
        try:
            assert stat.S_IMODE(private_wrapper.stat().st_mode) == 0o700
        finally:
            import shutil
            shutil.rmtree(private_wrapper)


# ---------------------------------------------------------------------------
# TestBackupEncryption
# ---------------------------------------------------------------------------


class TestBackupEncryption:
    def test_encrypt_backup_creates_enc_dir(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        backup = tmp_path / "backup_20240101_000000"
        backup.mkdir()
        (backup / "data.bin").write_bytes(b"important data")

        result = mgr.encrypt_backup(backup)
        assert result.name.endswith(".enc")
        assert result.exists()

    def test_decrypt_backup_no_metadata_returns_original(self, tmp_path):
        mgr, _ = make_manager_with_key(tmp_path)
        backup = tmp_path / "backup_plain"
        backup.mkdir()
        result = mgr.decrypt_backup(backup)
        assert result == backup


# ---------------------------------------------------------------------------
# TestKeyGeneration
# ---------------------------------------------------------------------------


class TestKeyGeneration:
    def test_generate_symmetric_key_is_32_bytes(self):
        key = EncryptionManager.generate_symmetric_key()
        assert len(key) == 32

    def test_generate_symmetric_key_non_deterministic(self):
        k1 = EncryptionManager.generate_symmetric_key()
        k2 = EncryptionManager.generate_symmetric_key()
        assert k1 != k2

    def test_generate_keypair_returns_pem_bytes(self):
        pub, priv = EncryptionManager.generate_keypair(algorithm="rsa-4096")
        assert b"-----BEGIN PUBLIC KEY-----" in pub
        assert b"-----BEGIN PRIVATE KEY-----" in priv

    def test_generate_keypair_pem_loadable(self):
        pub_pem, priv_pem = EncryptionManager.generate_keypair(algorithm="rsa-4096")
        pub_key = serialization.load_pem_public_key(pub_pem, backend=default_backend())
        priv_key = serialization.load_pem_private_key(priv_pem, password=None, backend=default_backend())
        assert pub_key is not None
        assert priv_key is not None

    def test_generate_keypair_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            EncryptionManager.generate_keypair(algorithm="unsupported")

    def test_generate_keypair_ecdsa_unsupported(self):
        with pytest.raises(ValueError, match="Unsupported"):
            EncryptionManager.generate_keypair(algorithm="ecdsa-p384")


def test_tree_preflight_refuses_ownership_unrestorable_by_current_user(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source = tmp_path / "source"
    source.mkdir()
    item = source / "foreign-file"
    item.write_text("preserve original")
    real_lstat = os.lstat
    def lstat(path, *args, **kwargs):
        value = real_lstat(path, *args, **kwargs)
        if Path(path) == item:
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            fields["st_uid"] = 1001
            return SimpleNamespace(**fields)
        if Path(path) == source:
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            fields["st_uid"] = 1000
            return SimpleNamespace(**fields)
        return value
    from pathlib import Path
    monkeypatch.setattr("bbackup.encryption.os.lstat", lstat)
    monkeypatch.setattr("bbackup.encryption.os.geteuid", lambda: 1000)
    with pytest.raises(OSError, match="Cannot restore tree ownership"):
        EncryptionManager.validate_directory_tree(source)
    assert item.read_text() == "preserve original"
