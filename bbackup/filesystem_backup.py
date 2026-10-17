"""
Filesystem backup using host-direct rsync.
Backs up local file paths and directory trees without Docker involvement.
Purpose: Provide rsync-based backup for arbitrary host filesystem paths.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import re
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config, FilesystemTarget, ensure_private_directory
from .logging import get_logger
from .encryption import EncryptionManager

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
            # rsync -a omits ACLs and xattrs. Check the original tree before
            # those attributes can disappear from the staged copy.
            EncryptionManager.validate_directory_tree(Path(target.path))
            ensure_private_directory(dest)
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

            cmd = self._build_rsync_cmd(target, dest, incremental, exclude_file)
            return self._run_rsync(cmd, progress_callback)
        except Exception as e:
            logger.error(f"Filesystem backup failed for '{target.name}': {e}")
            return False
        finally:
            if exclude_file and exclude_file.exists():
                exclude_file.unlink()

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
