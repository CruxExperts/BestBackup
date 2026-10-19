"""
Remote storage integration.
Handles rclone (Google Drive) and SFTP remote backups.
"""

import os
import subprocess
import shutil
from pathlib import Path
from typing import List
from rich.console import Console

from .archive import is_solid_archive_name
from .config import RemoteStorage, Config, get_effective_rclone_options
from .logging import get_logger

logger = get_logger('remote')


class RemoteStorageManager:
    """Manage remote storage operations."""
    
    def __init__(self, config: Config):
        self.config = config
        self.console = Console()
    
    def upload_to_rclone(
        self,
        remote: RemoteStorage,
        local_path: Path,
        remote_path: str,
        progress_callback=None,
    ) -> bool:
        """Upload to remote via rclone."""
        if not remote.remote_name:
            self.console.print(f"[red]Error: rclone remote name not configured for {remote.name}[/red]")
            return False
        
        # Check if rclone is installed
        if not shutil.which("rclone"):
            self.console.print("[red]Error: rclone not found. Please install rclone first.[/red]")
            return False
        
        # Build rclone command
        final_rclone_path = f"{remote.remote_name}:{remote_path}"
        partial_rclone_path = f"{final_rclone_path}.partial"
        opts = get_effective_rclone_options(self.config, remote)
        rclone_copy_command = "copyto" if local_path.is_file() else "copy"
        cmd = [
            "rclone",
            rclone_copy_command,
            str(local_path),
            partial_rclone_path,
            "--progress",
            "--stats=1s",
            "--transfers",
            str(opts.transfers),
            "--checkers",
            str(opts.checkers),
        ]
        if progress_callback:
            # Use rclone's JSON output for progress tracking
            cmd.append("--stats-log-level=NOTICE")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            
            if progress_callback:
                for line in process.stdout:
                    if progress_callback:
                        progress_callback(line)
            
            process.wait()
            success = process.returncode == 0
            if not success:
                logger.error(f"Failed to upload to rclone: {remote_path}")
                self._cleanup_rclone_partial(remote, remote_path, local_path)
                return False

            move_cmd = [
                "rclone",
                "moveto",
                partial_rclone_path,
                final_rclone_path,
                "--transfers",
                str(opts.transfers),
                "--checkers",
                str(opts.checkers),
            ]
            move_result = subprocess.run(move_cmd, capture_output=True, text=True, check=False)
            if move_result.returncode != 0:
                logger.error(f"Failed to promote rclone partial upload: {move_result.stderr.strip()}")
                self._cleanup_rclone_partial(remote, remote_path, local_path)
                return False

            logger.info(f"Successfully uploaded to rclone: {remote_path}")
            return True
        except Exception as e:
            logger.error(f"Error uploading to rclone: {e}")
            self.console.print(f"[red]Error uploading to rclone: {e}[/red]")
            self._cleanup_rclone_partial(remote, remote_path, local_path)
            return False

    def _cleanup_rclone_partial(self, remote: RemoteStorage, remote_path: str, local_path: Path) -> None:
        """Best-effort removal of an incomplete rclone upload."""
        if not remote.remote_name:
            return
        try:
            opts = get_effective_rclone_options(self.config, remote)
            partial_rclone_path = f"{remote.remote_name}:{remote_path}.partial"
            cmd = [
                "rclone",
                "deletefile" if local_path.is_file() else "purge",
                partial_rclone_path,
                "--transfers",
                str(opts.transfers),
                "--checkers",
                str(opts.checkers),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception as e:
            logger.warning(f"Could not remove rclone partial upload: {e}")
    
    def upload_to_sftp(
        self,
        remote: RemoteStorage,
        local_path: Path,
        remote_path: str,
    ) -> bool:
        """Upload to remote via SFTP."""
        partial_remote_path = f"{remote_path}.partial"
        sftp = None
        ssh = None
        try:
            import paramiko
        except ImportError:
            self.console.print("[red]Error: paramiko not installed. Run: uv sync[/red]")
            return False
        
        try:
            # Setup SSH client
            ssh = paramiko.SSHClient()
            known_hosts_files = (
                os.path.expanduser("~/.ssh/known_hosts"),
                os.path.expanduser("~/.ssh/known_hosts2"),
                "/etc/ssh/ssh_known_hosts",
                "/etc/ssh/ssh_known_hosts2",
            )
            for known_hosts_file in known_hosts_files:
                if os.path.isfile(known_hosts_file):
                    ssh.load_system_host_keys(known_hosts_file)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            
            # Load key file
            key_path = os.path.expanduser(remote.key_file) if remote.key_file else None
            if key_path and os.path.exists(key_path):
                key = paramiko.RSAKey.from_private_key_file(key_path)
            else:
                key = None
            
            # Connect
            ssh.connect(
                hostname=remote.host,
                port=remote.port,
                username=remote.user,
                pkey=key,
                timeout=30,
            )
            
            # Setup SFTP
            sftp = ssh.open_sftp()

            if local_path.is_file():
                # remote_path is full destination path (e.g. path/backup_20260304.tar.gz); create parent only (Gap 1)
                parts = remote_path.rstrip("/").split("/")
                for i in range(1, len(parts)):
                    parent = "/".join(parts[:i])
                    if parent:
                        try:
                            sftp.stat(parent)
                        except IOError:
                            try:
                                sftp.mkdir(parent)
                            except IOError:
                                pass
                sftp.put(str(local_path), partial_remote_path)
            else:
                try:
                    sftp.mkdir(partial_remote_path)
                except IOError:
                    pass
                self._upload_directory_sftp(sftp, local_path, partial_remote_path)

            self._rename_sftp(sftp, partial_remote_path, remote_path)
            
            sftp.close()
            ssh.close()
            return True
        except Exception as e:
            try:
                if sftp is not None:
                    try:
                        sftp.remove(partial_remote_path)
                    except Exception:
                        sftp.rmdir(partial_remote_path)
            except Exception:
                pass
            try:
                if sftp is not None:
                    sftp.close()
                if ssh is not None:
                    ssh.close()
            except Exception:
                pass
            self.console.print(f"[red]Error uploading to SFTP: {e}[/red]")
            return False

    def _rename_sftp(self, sftp, src: str, dest: str) -> None:
        """Rename an SFTP path, preferring POSIX overwrite semantics when available."""
        if hasattr(sftp, "posix_rename"):
            sftp.posix_rename(src, dest)
        else:
            sftp.rename(src, dest)
    
    def _upload_directory_sftp(self, sftp, local_dir: Path, remote_dir: str):
        """Recursively upload directory via SFTP."""
        for item in local_dir.iterdir():
            remote_path = f"{remote_dir}/{item.name}"
            if item.is_file():
                sftp.put(str(item), remote_path)
            elif item.is_dir():
                try:
                    sftp.mkdir(remote_path)
                except IOError:
                    pass
                self._upload_directory_sftp(sftp, item, remote_path)
    
    def upload_to_local(
        self,
        remote: RemoteStorage,
        local_path: Path,
        remote_path: str,
    ) -> bool:
        """Copy to local directory (or single file when backup is solid archive)."""
        try:
            dest = Path(os.path.expanduser(remote_path))
            partial_dest = dest.with_name(f"{dest.name}.partial")
            if partial_dest.exists():
                if partial_dest.is_dir():
                    shutil.rmtree(partial_dest)
                else:
                    partial_dest.unlink()

            if local_path.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, partial_dest)
                os.replace(partial_dest, dest)
            elif local_path.is_dir():
                dest.parent.mkdir(parents=True, exist_ok=True)
                def ignore_special_files(src, names):
                    ignored = []
                    for name in names:
                        src_path = Path(src) / name
                        if src_path.is_socket() or (src_path.is_symlink() and not src_path.exists()):
                            ignored.append(name)
                    return ignored
                shutil.copytree(local_path, partial_dest, ignore=ignore_special_files)
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                partial_dest.replace(dest)
            return True
        except Exception as e:
            try:
                if partial_dest.exists():
                    if partial_dest.is_dir():
                        shutil.rmtree(partial_dest)
                    else:
                        partial_dest.unlink()
            except Exception:
                pass
            self.console.print(f"[red]Error copying to local: {e}[/red]")
            return False
    
    def upload_backup(
        self,
        remote: RemoteStorage,
        backup_path: Path,
        backup_name: str,
        progress_callback=None,
    ) -> bool:
        """Upload backup to remote storage."""
        """Upload backup to remote storage."""
        remote_path = os.path.join(remote.path, backup_name).replace("\\", "/")
        
        if remote.type == "rclone":
            return self.upload_to_rclone(remote, backup_path, remote_path, progress_callback)
        elif remote.type == "sftp":
            return self.upload_to_sftp(remote, backup_path, remote_path)
        elif remote.type == "local":
            return self.upload_to_local(remote, backup_path, remote_path)
        else:
            self.console.print(f"[red]Unknown remote type: {remote.type}[/red]")
            return False
    
    def list_backups(self, remote: RemoteStorage) -> List[str]:
        """List available backups on remote."""
        if remote.type == "rclone":
            return self._list_rclone_backups(remote)
        elif remote.type == "local":
            return self._list_local_backups(remote)
        else:
            return []
    
    def _list_rclone_backups(self, remote: RemoteStorage) -> List[str]:
        """List backups on rclone remote."""
        if not remote.remote_name:
            return []
        opts = get_effective_rclone_options(self.config, remote)
        try:
            cmd = [
                "rclone",
                "lsf",
                f"{remote.remote_name}:{remote.path}",
                "--max-depth",
                "1",
                "--format",
                "p",
                "--transfers",
                str(opts.transfers),
                "--checkers",
                str(opts.checkers),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                # Parse output to get backup names
                backups = []
                for line in result.stdout.splitlines():
                    name = line.strip()
                    if not name or "/" in name.rstrip("/"):
                        continue
                    backups.append(name.rstrip("/"))
                return backups
        except Exception:
            pass
        
        return []
    
    def _list_local_backups(self, remote: RemoteStorage) -> List[str]:
        """List backups in local directory (directories and solid archive files)."""
        try:
            backup_dir = Path(os.path.expanduser(remote.path))
            if not backup_dir.exists():
                return []
            names = []
            for item in backup_dir.iterdir():
                if item.is_dir():
                    names.append(item.name)
                elif item.is_file() and is_solid_archive_name(item.name):
                    names.append(item.name)
            return names
        except Exception:
            pass
        return []
