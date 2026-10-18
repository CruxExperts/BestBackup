"""
Tests for bbackup.archive: solid archive create/unpack and naming helpers.
"""

import tarfile
import stat
import os
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bbackup.archive import (
    create_solid_archive,
    is_solid_archive_name,
    strip_solid_archive_suffix,
    unpack_solid_archive,
)


class TestNamingHelpers:
    def test_is_solid_archive_name_tar_gz(self):
        assert is_solid_archive_name("backup_20260304_120000.tar.gz") is True
        assert is_solid_archive_name("backup_20260304_120000.tar.gz.enc") is True

    def test_is_solid_archive_name_other_formats(self):
        assert is_solid_archive_name("backup_20260304.tar.bz2") is True
        assert is_solid_archive_name("backup_20260304.tar.xz.enc") is True

    def test_is_solid_archive_name_false(self):
        assert is_solid_archive_name("backup_20260304_120000") is False
        assert is_solid_archive_name("other.tar.gz") is True  # any name with suffix

    def test_strip_solid_archive_suffix(self):
        assert strip_solid_archive_suffix("backup_20260304_120000.tar.gz") == "backup_20260304_120000"
        assert strip_solid_archive_suffix("backup_20260304_120000.tar.gz.enc") == "backup_20260304_120000"
        assert strip_solid_archive_suffix("backup_20260304_120000") == "backup_20260304_120000"


class TestCreateSolidArchive:
    def test_archive_roundtrip_retains_supported_modes_and_ownership(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        nested = backup_dir / "data"
        nested.mkdir(parents=True)
        nested.chmod(0o750)
        item = nested / "file.txt"
        item.write_text("data")
        item.chmod(0o640)
        archive = create_solid_archive(backup_dir, {"format": "gzip"})
        unpacked, cleanup = unpack_solid_archive(archive)
        try:
            restored = unpacked / "data" / "file.txt"
            assert stat.S_IMODE(restored.stat().st_mode) == 0o640
            assert stat.S_IMODE(restored.parent.stat().st_mode) == 0o750
            assert (restored.stat().st_uid, restored.stat().st_gid) == (item.stat().st_uid, item.stat().st_gid)
        finally:
            import shutil
            shutil.rmtree(cleanup)

    def test_rejects_absolute_or_escaping_links_before_output(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        link = backup_dir / "link"
        for target in ("/etc/passwd", "../../outside"):
            link.symlink_to(target)
            with pytest.raises(OSError, match="link cannot be archived safely"):
                create_solid_archive(backup_dir, {"format": "gzip"})
            assert not (tmp_path / "backup_20260304_120000.tar.gz").exists()
            link.unlink()

    def test_create_solid_archive_plain(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        (backup_dir / "configs").mkdir()
        (backup_dir / "configs" / "c1.json").write_text("{}")
        compression = {"enabled": True, "level": 6, "format": "gzip"}
        out = create_solid_archive(backup_dir, compression, encryption_config=None)
        assert out == backup_dir.parent / "backup_20260304_120000.tar.gz"
        assert out.exists()
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        assert any("configs" in n for n in names)

    def test_create_solid_archive_rejects_special_files_before_output(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        fifo = backup_dir / "unsupported-pipe"
        try:
            import os
            os.mkfifo(fifo)
        except (AttributeError, NotImplementedError, OSError) as exc:
            pytest.skip(f"FIFO creation is unavailable: {exc}")

        with pytest.raises(OSError, match="Unsupported special file type"):
            create_solid_archive(
                backup_dir,
                {"enabled": True, "level": 6, "format": "gzip"},
            )

        assert fifo.exists()
        assert not (tmp_path / "backup_20260304_120000.tar.gz").exists()

    def test_create_solid_archive_with_encryption_skipped_when_disabled(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        (backup_dir / "volumes").mkdir()
        compression = {"enabled": True, "level": 1, "format": "gzip"}
        enc = MagicMock()
        enc.enabled = False
        out = create_solid_archive(backup_dir, compression, encryption_config=enc)
        assert out.suffix == ".gz"
        assert not str(out).endswith(".enc")

    def test_create_solid_archive_removes_partial_on_failure(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        compression = {"enabled": True, "level": 6, "format": "gzip"}
        enc = MagicMock()
        enc.enabled = True
        with patch("bbackup.archive.EncryptionManager") as MockEnc:
            MockEnc.return_value.encrypt_file.return_value = False
            with pytest.raises(OSError):
                create_solid_archive(backup_dir, compression, encryption_config=enc)
            # Partial .tar.gz should be removed
            partial = backup_dir.parent / "backup_20260304_120000.tar.gz"
            assert not partial.exists()

    def test_create_solid_archive_preserves_existing_final_on_failure(self, tmp_path):
        backup_dir = tmp_path / "backup_20260304_120000"
        backup_dir.mkdir()
        final = backup_dir.parent / "backup_20260304_120000.tar.gz"
        final.write_bytes(b"existing")
        compression = {"enabled": True, "level": 6, "format": "gzip"}
        enc = MagicMock()
        enc.enabled = True

        with patch("bbackup.archive.EncryptionManager") as MockEnc:
            MockEnc.return_value.encrypt_file.return_value = False
            with pytest.raises(OSError):
                create_solid_archive(backup_dir, compression, encryption_config=enc)

        assert final.read_bytes() == b"existing"
        assert not final.with_name(f"{final.name}.partial").exists()


class TestUnpackSolidArchive:
    @pytest.mark.skipif(os.geteuid() == 0, reason="non-root ownership refusal")
    def test_rejects_unrestorable_owner_before_extraction(self, tmp_path):
        archive = tmp_path / "backup_20260304.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            item = tarfile.TarInfo("content/file.txt")
            item.size = 4
            item.uid = os.geteuid() + 1
            item.gid = os.getegid()
            tar.addfile(item, io.BytesIO(b"data"))
        dest = tmp_path / "out"
        with pytest.raises(OSError, match="ownership"):
            unpack_solid_archive(archive, dest_dir=dest)
        assert not any(dest.iterdir())

    def test_unpack_returns_dir_unchanged(self, tmp_path):
        d = tmp_path / "backup_20260304"
        d.mkdir()
        unpacked, cleanup = unpack_solid_archive(d)
        assert unpacked == d
        assert cleanup is None

    def test_unpack_tar_gz(self, tmp_path):
        archive = tmp_path / "backup_20260304.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            add_dir = tmp_path / "content"
            add_dir.mkdir()
            (add_dir / "test.txt").write_text("hello")
            tar.add(add_dir, arcname="content")
        unpacked, cleanup = unpack_solid_archive(archive)
        assert cleanup is not None
        assert stat.S_IMODE(cleanup.stat().st_mode) == 0o700
        assert (unpacked / "test.txt").read_text() == "hello"
        if cleanup.exists():
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)

    def test_unpack_returns_tuple(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        unpacked, cleanup = unpack_solid_archive(d)
        assert isinstance(unpacked, Path)
        assert cleanup is None

    def test_dot_root_mode_is_restored_under_private_wrapper(self, tmp_path):
        archive = tmp_path / "dot-root.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            root.uid, root.gid = os.geteuid(), os.getegid()
            tar.addfile(root)

            item = tarfile.TarInfo("secret.txt")
            item.mode = 0o640
            item.uid, item.gid = os.geteuid(), os.getegid()
            item.size = len(b"private")
            tar.addfile(item, io.BytesIO(b"private"))

        unpacked, cleanup = unpack_solid_archive(archive)
        try:
            assert cleanup is not None
            assert stat.S_IMODE(cleanup.stat().st_mode) == 0o700
            assert cleanup in unpacked.parents
            assert stat.S_IMODE(unpacked.stat().st_mode) == 0o755
            assert stat.S_IMODE((unpacked / "secret.txt").stat().st_mode) == 0o640
            assert (unpacked / "secret.txt").read_text() == "private"
        finally:
            if cleanup is not None:
                import shutil
                shutil.rmtree(cleanup, ignore_errors=True)

    def test_dot_root_entry_requires_a_private_extraction_wrapper(self, tmp_path):
        from bbackup.archive import extract_archive_safely

        archive = tmp_path / "unwrapped-dot-root.tar"
        with tarfile.open(archive, "w") as tar:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            root.uid, root.gid = os.geteuid(), os.getegid()
            tar.addfile(root)
            item = tarfile.TarInfo("secret.txt")
            item.uid, item.gid = os.geteuid(), os.getegid()
            item.size = len(b"private")
            tar.addfile(item, io.BytesIO(b"private"))

        public_parent = tmp_path / "unprotected"
        public_parent.mkdir(mode=0o755)
        destination = public_parent / "destination"
        destination.mkdir(mode=0o700)
        with tarfile.open(archive) as tar:
            with pytest.raises(OSError, match="private extraction wrapper"):
                extract_archive_safely(tar, destination)

        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert not (destination / "secret.txt").exists()


def test_archive_does_not_restore_unnormalized_link_target(tmp_path):
    from bbackup.archive import extract_archive_safely
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    outside.chmod(0o640)
    before = outside.stat()
    destination = tmp_path / "restored"
    destination.mkdir()
    archive = tmp_path / "hostile.tar"
    with tarfile.open(archive, "w") as stream:
        link = tarfile.TarInfo("a")
        link.type = tarfile.SYMTYPE
        link.linkname = "."
        link.uid, link.gid = os.geteuid(), os.getegid()
        stream.addfile(link)
        hardlink = tarfile.TarInfo("h")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "a/../outside"
        hardlink.mode = 0o777
        hardlink.uid, hardlink.gid = os.geteuid(), os.getegid()
        stream.addfile(hardlink)
    with tarfile.open(archive) as stream:
        with pytest.raises((KeyError, OSError, tarfile.TarError)):
            extract_archive_safely(stream, destination)
    after = outside.stat()
    assert outside.read_text() == "keep me"
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns) == (
        before.st_ino, before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns)
    assert not (destination / "h").exists()


def test_archive_extract_requests_numeric_ownership(tmp_path, monkeypatch):
    from bbackup.archive import extract_archive_safely
    stream = MagicMock()
    item = tarfile.TarInfo("file")
    item.uid, item.gid = os.geteuid(), os.getegid()
    item.uname, item.gname = "another-user", "another-group"
    stream.getmembers.return_value = [item]
    extract_archive_safely(stream, tmp_path)
    assert stream.extractall.call_args.kwargs["numeric_owner"] is True


@pytest.mark.parametrize("protected", [False, True])
def test_root_symlink_alias_cannot_expose_extracted_payload(tmp_path, protected):
    from bbackup.archive import extract_archive_safely
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir(mode=0o700 if protected else 0o755)
    destination = wrapper / "payload"
    destination.mkdir(mode=0o700)
    archive = tmp_path / "alias.tar"
    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("alias")
        link.type = tarfile.SYMTYPE
        link.linkname = "."
        link.uid, link.gid = os.geteuid(), os.getegid()
        tar.addfile(link)
        directory = tarfile.TarInfo("alias")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid, directory.gid = os.geteuid(), os.getegid()
        tar.addfile(directory)
    with tarfile.open(archive) as tar:
        if not protected:
            with pytest.raises(OSError, match="private extraction wrapper"):
                extract_archive_safely(tar, destination)
            assert not (destination / "alias").exists()
            assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        else:
            extract_archive_safely(tar, destination)
            assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
            assert (destination / "alias").is_symlink()
