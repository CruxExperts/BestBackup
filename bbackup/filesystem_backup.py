"""
Filesystem backup using host-direct rsync.
Backs up local file paths and directory trees without Docker involvement.
Purpose: Provide rsync-based backup for arbitrary host filesystem paths.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import re
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config, FilesystemTarget, ensure_private_directory
from .logging import get_logger

logger = get_logger("filesystem_backup")


class FilesystemBackup:
    """Back up host filesystem paths using rsync directly."""

    def __init__(self, config: Config):
        self.config = config

    def backup_path(
        self,
        target: FilesystemTarget,
        backup_dir: Path,
        incremental: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """
        Back up a single filesystem target using rsync.

        Args:
            target: FilesystemTarget describing the source path and excludes.
            backup_dir: Root backup directory for this run (e.g. staging/backup_YYYYMMDD_HHMMSS).
            incremental: Whether to use --link-dest for hard-linked incremental backup.
            progress_callback: Optional line-by-line rsync output handler.

        Returns:
            True on success, False on any failure.
        """
        target_name = Path(target.name)
        if target_name.is_absolute() or len(target_name.parts) != 1 or target_name.name in ("", ".", ".."):
            logger.error("Invalid filesystem backup target name: %s", target.name)
            return False
        dest = backup_dir / "filesystems" / target.name

        exclude_file: Optional[Path] = None
        try:
            if target.excludes:
                fd, tmp_name = tempfile.mkstemp(prefix="bbackup_excludes_", suffix=".txt")
                exclude_file = Path(tmp_name)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                        tmp.write("\n".join(target.excludes))
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise

            # Ask rsync itself which source entries survive the configured
            # filters. A Python-side matcher would drift from rsync's filter
            # rules, and a post-copy check would miss metadata rsync -a drops.
            self._preflight_source_selection(target, exclude_file)
            ensure_private_directory(dest)
            cmd = self._build_rsync_cmd(target, dest, incremental, exclude_file)
            return self._run_rsync(cmd, progress_callback)
        except Exception as e:
            logger.error(f"Filesystem backup failed for '{target.name}': {e}")
            return False
        finally:
            if exclude_file and exclude_file.exists():
                exclude_file.unlink()

    def _preflight_source_selection(
        self,
        target: FilesystemTarget,
        exclude_file_path: Optional[Path],
    ) -> None:
        """Reject unsupported metadata only for paths selected by rsync."""
        source = Path(target.path)
        root_info = os.lstat(source)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise OSError(f"Source root is not a real directory: {source}")
        if root_info.st_uid != os.geteuid():
            raise OSError(f"Source root is owned by another user: {source}")
        self._validate_selected_source_entry(source, root_info)

        with tempfile.TemporaryDirectory(prefix="bbackup_preflight_") as temp_dir:
            selected_destination = Path(temp_dir) / "selected"
            selected_destination.mkdir(mode=0o700)
            cmd = [
                "rsync", "-av", "--dry-run", "--ignore-times", "--delete",
                "--out-format=BBACKUP_SELECTED:%n",
            ]
            if exclude_file_path is not None:
                cmd += ["--exclude-from", str(exclude_file_path)]
            cmd += [str(source).rstrip("/") + "/", str(selected_destination) + "/"]
            result = subprocess.run(cmd, capture_output=True, check=False)
            if result.returncode != 0:
                stderr = result.stderr
                if isinstance(stderr, bytes):
                    stderr = os.fsdecode(stderr)
                raise OSError(f"Rsync source preflight failed: {str(stderr).strip()}")

            output = result.stdout
            if isinstance(output, str):
                output = os.fsencode(output)
            for relative_path in self._parse_rsync_selection(output):
                current = source
                parts = Path(relative_path).parts
                for index, part in enumerate(parts):
                    current = current / part
                    info = os.lstat(current)
                    if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                        raise OSError(f"Invalid selected source path: {relative_path}")
                self._validate_selected_source_entry(current, info)

    @staticmethod
    def _parse_rsync_selection(output: bytes) -> List[str]:
        """Read rsync's escaped, newline-terminated selected path records."""
        marker = b"BBACKUP_SELECTED:"
        records: List[str] = []
        for line in output.split(b"\n"):
            if not line.startswith(marker):
                continue
            encoded_path = line[len(marker):]
            raw_path = bytearray()
            cursor = 0
            while cursor < len(encoded_path):
                if (
                    encoded_path[cursor:cursor + 2] == b"\\#"
                    and cursor + 5 <= len(encoded_path)
                    and all(byte in b"01234567" for byte in encoded_path[cursor + 2:cursor + 5])
                ):
                    value = int(encoded_path[cursor + 2:cursor + 5], 8)
                    if value > 0o377:
                        raise OSError("Rsync source preflight returned an invalid escaped path")
                    raw_path.append(value)
                    cursor += 5
                else:
                    raw_path.append(encoded_path[cursor])
                    cursor += 1
            relative_path = os.fsdecode(bytes(raw_path))
            if relative_path in ("", ".", "./"):
                continue
            if relative_path.startswith("./"):
                relative_path = relative_path[2:]
            relative_path = relative_path.rstrip("/")
            path = Path(relative_path)
            if path.is_absolute() or ".." in path.parts:
                raise OSError(f"Rsync source preflight returned an unsafe path: {relative_path}")
            records.append(relative_path)
        return records

    @staticmethod
    def _validate_selected_source_entry(path: Path, info: os.stat_result) -> None:
        if info.st_mode & 0o7000:
            raise OSError(f"Unsupported archive special permission bits: {path}")
        if os.geteuid() != 0 and (
            info.st_uid != os.geteuid()
            or info.st_gid not in {os.getegid(), *os.getgroups()}
        ):
            raise OSError(f"Cannot restore tree ownership as the current user: {path}")
        try:
            xattrs = os.listxattr(path, follow_symlinks=False)
        except (AttributeError, NotImplementedError):
            xattrs = []
        if xattrs:
            raise OSError(f"Unsupported extended attributes or ACLs on: {path}")
        if not (
            stat.S_ISDIR(info.st_mode)
            or stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
        ):
            raise OSError(f"Unsupported special file type: {path}")

    def _build_rsync_cmd(
        self,
        target: FilesystemTarget,
        dest: Path,
        incremental: bool,
        exclude_file_path: Optional[Path],
    ) -> List[str]:
        """Build the rsync command list for a filesystem target."""
        src = str(target.path).rstrip("/") + "/"
        cmd = [
            "rsync", "-av", "--delete",
            "--progress", "--info=progress2",
        ]

        if exclude_file_path:
            cmd += ["--exclude-from", str(exclude_file_path)]

        if incremental:
            # dest = <staging>/<run_dir>/filesystems/<name>; staging is 3 levels up
            run_dir = dest.parent.parent
            staging_dir = run_dir.parent
            prev = self._find_previous_backup(target.name, staging_dir, current_run_dir=run_dir)
            if prev:
                cmd += ["--link-dest", str(prev)]

        cmd += [src, str(dest) + "/"]
        return cmd

    def _find_previous_backup(
        self,
        target_name: str,
        staging_dir: Path,
        current_run_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        """
        Find the most recent previous backup dir that contains this target.

        Scans siblings of the current run dir for backup_YYYYMMDD_HHMMSS pattern,
        sorted newest-first, returning the first that has a filesystems/name subdir.
        Excludes current_run_dir (if provided) so a backup never links to itself.
        """
        if not staging_dir.exists():
            return None

        pattern = re.compile(r"^backup_\d{8}_\d{6}$")
        candidates = sorted(
            [
                d for d in staging_dir.iterdir()
                if d.is_dir()
                and pattern.match(d.name)
                and d != current_run_dir
            ],
            key=lambda d: d.name,
            reverse=True,
        )
        for candidate in candidates:
            candidate_path = candidate / "filesystems" / target_name
            if candidate_path.exists():
                return candidate_path
        return None

    def _run_rsync(
        self,
        cmd: List[str],
        progress_callback: Optional[Callable[[str], None]],
    ) -> bool:
        """
        Execute rsync, streaming output to progress_callback if provided.

        Returns True if rsync exits with code 0.
        """
        if progress_callback:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            for line in process.stdout:
                progress_callback(line)
            process.wait()
            return process.returncode == 0

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"rsync failed (rc={result.returncode}): {result.stderr.strip()}")
        return result.returncode == 0
