"""
Backup runner with status tracking for live TUI updates.
"""

import time
from pathlib import Path
from typing import List, Optional
from .config import Config, BackupScope, FilesystemTarget
from .docker_backup import DockerBackup
from .filesystem_backup import FilesystemBackup
from .remote import RemoteStorageManager
from .rotation import BackupRotation
from .tui import BackupStatus
from .logging import get_logger
from .encryption import EncryptionManager
from .manifest import generate_backup_manifest

logger = get_logger('backup_runner')


def _metadata_archive_path(backup_dir: Path, config: Config) -> Path:
    compression = config.data.get("backup", {}).get("compression", {})
    ext = {"gzip": "gz", "bzip2": "bz2", "xz": "xz"}.get(compression.get("format", "gzip"), "gz")
    return backup_dir / f"metadata.tar.{ext}"


class BackupRunner:
    """Runs backup operations with status tracking."""
    
    def __init__(self, config: Config, status: BackupStatus):
        self.config = config
        self.status = status
        self.docker_backup = DockerBackup(config)
        self.remote_mgr = RemoteStorageManager(config)
        self.rotation = BackupRotation(config.retention, config=config)
    
    def run_backup(
        self,
        backup_dir: Path,
        containers: Optional[List[str]] = None,
        scope: Optional[BackupScope] = None,
        incremental: bool = False,
        filesystem_targets: Optional[List[FilesystemTarget]] = None,
        defer_completion: bool = False,
    ) -> dict:
        """Run backup with status updates."""
        if scope is None:
            scope = self.config.scope

        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

        results = {
            "containers": {},
            "volumes": {},
            "networks": {},
            "filesystems": {},
            "errors": [],
        }
        
        self.status.start()

        fs_targets = filesystem_targets or []

        # Calculate total items
        total_items = 0
        if scope.containers or scope.configs:
            containers_to_backup = containers or [c["name"] for c in self.docker_backup.get_all_containers()]
            total_items += len(containers_to_backup)
        if scope.volumes:
            if containers:
                container_volumes = self.docker_backup._get_container_volumes(containers)
            else:
                container_volumes = [v["name"] for v in self.docker_backup.get_all_volumes()]
            total_items += len(container_volumes)
        if scope.networks:
            networks = self.docker_backup.get_all_networks()
            total_items += len(networks)
        if scope.filesystems:
            total_items += len(fs_targets)

        self.status.update(total=total_items, completed=0)
        
        completed = 0
        
        # Backup containers
        if (scope.containers or scope.configs) and not self.status.get_status() == "cancelled":
            containers_to_backup = containers or [c["name"] for c in self.docker_backup.get_all_containers()]
            
            configs_dir = backup_dir / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            
            for container_name in containers_to_backup:
                if self.status.get_status() == "cancelled":
                    break
                
                # Wait if paused
                while self.status.get_status() == "paused" and not self.status.get_status() == "cancelled":
                    time.sleep(0.5)
                
                if self.status.get_status() == "cancelled":
                    break
                
                # Check if skip requested
                if self.status.consume_skip():
                    logger.info(f"Skipping container: {container_name}")
                    results["containers"][container_name] = "skipped"
                    self.status.set_item_status("containers", container_name, "skipped")
                    completed += 1
                    self.status.update(completed=completed)
                    continue
                
                self.status.update(
                    action=f"Backing up container: {container_name}",
                    item=container_name,
                    completed=completed,
                )
                
                if scope.configs:
                    logger.info(f"Backing up container config: {container_name}")
                    success = self.docker_backup.backup_container_config(container_name, configs_dir)
                    results["containers"][container_name] = "success" if success else "failed"
                    self.status.set_item_status(
                        "containers",
                        container_name,
                        "success" if success else "failed",
                    )
                    if not success:
                        error_msg = f"Failed to backup container config: {container_name}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)
                        self.status.add_error(error_msg)
                
                completed += 1
                self.status.update(completed=completed)
        
        # Backup volumes
        if scope.volumes and not self.status.get_status() == "cancelled":
            volumes_dir = backup_dir / "volumes"
            volumes_dir.mkdir(parents=True, exist_ok=True)
            
            # Get volumes for selected containers
            if containers:
                container_volumes = self.docker_backup._get_container_volumes(containers)
            else:
                container_volumes = [v["name"] for v in self.docker_backup.get_all_volumes()]
            
            for volume_name in container_volumes:
                if self.status.get_status() == "cancelled":
                    break
                
                # Wait if paused
                while self.status.get_status() == "paused" and not self.status.get_status() == "cancelled":
                    time.sleep(0.5)
                
                if self.status.get_status() == "cancelled":
                    break
                
                # Check if skip requested
                if self.status.consume_skip():
                    logger.info(f"Skipping volume: {volume_name}")
                    results["volumes"][volume_name] = "skipped"
                    self.status.set_item_status("volumes", volume_name, "skipped")
                    completed += 1
                    self.status.update(completed=completed)
                    continue
                
                self.status.update(
                    action=f"Backing up volume: {volume_name}",
                    item=volume_name,
                    completed=completed,
                )
                
                logger.info(f"Backing up volume: {volume_name} (incremental={incremental})")

                success = self.docker_backup.backup_volume(
                    volume_name, backup_dir, incremental,
                    progress_callback=self._parse_rsync_progress
                )
                results["volumes"][volume_name] = "success" if success else "failed"
                self.status.set_item_status(
                    "volumes",
                    volume_name,
                    "success" if success else "failed",
                )
                if not success:
                    error_msg = f"Failed to backup volume: {volume_name}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                    self.status.add_error(error_msg)
                
                completed += 1
                self.status.update(completed=completed)
        
        # Backup networks
        if scope.networks and not self.status.get_status() == "cancelled":
            networks = self.docker_backup.get_all_networks()
            for network in networks:
                if self.status.get_status() == "cancelled":
                    break
                
                # Wait if paused
                while self.status.get_status() == "paused" and not self.status.get_status() == "cancelled":
                    time.sleep(0.5)
                
                if self.status.get_status() == "cancelled":
                    break
                
                # Check if skip requested
                if self.status.consume_skip():
                    logger.info(f"Skipping network: {network['name']}")
                    results["networks"][network["name"]] = "skipped"
                    self.status.set_item_status("networks", network["name"], "skipped")
                    completed += 1
                    self.status.update(completed=completed)
                    continue
                
                self.status.update(
                    action=f"Backing up network: {network['name']}",
                    item=network["name"],
                    completed=completed,
                )
                
                logger.info(f"Backing up network: {network['name']}")
                success = self.docker_backup.backup_network(network["name"], backup_dir)
                results["networks"][network["name"]] = "success" if success else "failed"
                self.status.set_item_status(
                    "networks",
                    network["name"],
                    "success" if success else "failed",
                )
                if not success:
                    error_msg = f"Failed to backup network: {network['name']}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                    self.status.add_error(error_msg)
                
                completed += 1
                self.status.update(completed=completed)
        
        # Backup filesystem paths
        fs_backup = FilesystemBackup(self.config)
        if scope.filesystems and fs_targets and not self.status.get_status() == "cancelled":
            for target in fs_targets:
                if self.status.get_status() == "cancelled":
                    break

                while self.status.get_status() == "paused" and not self.status.get_status() == "cancelled":
                    time.sleep(0.5)

                if self.status.get_status() == "cancelled":
                    break

                if self.status.consume_skip():
                    logger.info(f"Skipping filesystem path: {target.name}")
                    results["filesystems"][target.name] = "skipped"
                    self.status.set_item_status("filesystems", target.name, "skipped")
                    completed += 1
                    self.status.update(completed=completed)
                    continue

                self.status.update(
                    action=f"Backing up path: {target.path}",
                    item=target.name,
                    completed=completed,
                )

                logger.info(f"Backing up filesystem path: {target.path}")
                success = fs_backup.backup_path(
                    target, backup_dir, incremental,
                    progress_callback=self._parse_rsync_progress,
                )
                results["filesystems"][target.name] = "success" if success else "failed"
                self.status.set_item_status(
                    "filesystems",
                    target.name,
                    "success" if success else "failed",
                )
                if not success:
                    error_msg = f"Failed to backup path: {target.path}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                    self.status.add_error(error_msg)

                completed += 1
                self.status.update(completed=completed)

        if self.status.get_status() == "cancelled":
            self.status.set_status("cancelled", preserve_cancelled=False)
        else:
            self.status.set_status("finalizing")
            metadata_inputs_exist = any(
                (backup_dir / name).exists()
                for name in ("configs", "networks")
            )
            if metadata_inputs_exist:
                metadata_archive = _metadata_archive_path(backup_dir, self.config)
                if not self.docker_backup.create_metadata_archive(backup_dir, metadata_archive):
                    metadata_archive.unlink(missing_ok=True)
                    error_msg = "Failed to create metadata archive"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                    self.status.add_error(error_msg)
        if self.status.get_status() == "cancelled":
            self.status.set_status("cancelled", preserve_cancelled=False)
        elif results["errors"]:
            self.status.set_status("partial")
        else:
            if defer_completion:
                self.status.set_status("finalizing")
            else:
                self.status.set_status("completed")

        if self.status.get_status() != "cancelled":
            encryption_mode = self.config.encryption.method if self.config.encryption.enabled else "disabled"
            generate_backup_manifest(
                backup_dir,
                scope,
                filesystem_targets=fs_targets,
                encryption_mode=encryption_mode,
                item_results={
                    "containers": results["containers"],
                    "volumes": results["volumes"],
                    "networks": results["networks"],
                    "filesystems": results["filesystems"],
                },
                errors=results["errors"],
            )
        return results
    
    def _parse_rsync_progress(self, line: str) -> None:
        """Parse rsync --info=progress2 output and update status metrics."""
        import re
        # progress2 format: "    123,456,789  50%  123.45MB/s    0:00:05"
        progress_match = re.search(r'(\d+(?:,\d+)*)\s+(\d+)%\s+([\d.]+)([KMGT]?B/s)', line)
        if progress_match:
            bytes_str = progress_match.group(1).replace(',', '')
            percentage = int(progress_match.group(2))
            speed_str = progress_match.group(3)
            speed_unit = progress_match.group(4)
            try:
                bytes_transferred = int(bytes_str)
                speed = float(speed_str)
                speed_multiplier = {
                    'B/s': 1 / (1024 * 1024), 'KB/s': 1 / 1024,
                    'MB/s': 1, 'GB/s': 1024, 'TB/s': 1024 * 1024,
                }
                speed_mb = speed * speed_multiplier.get(speed_unit, 1)
                total_bytes = int(bytes_transferred * 100 / percentage) if percentage > 0 else 0
                self.status.update(
                    bytes_transferred=bytes_transferred,
                    total_bytes=total_bytes if total_bytes > 0 else None,
                )
                # transfer_speed is not a kwarg of update(); set directly
                self.status.transfer_speed = speed_mb
            except (ValueError, ZeroDivisionError):
                pass

        files_match = re.search(r'Number of files:\s*(\d+(?:,\d+)*)', line)
        if files_match:
            try:
                self.status.update(total_files=int(files_match.group(1).replace(',', '')))
            except ValueError:
                pass

        file_match = re.search(r'([^/\s]+\.\w+)\s*$', line.strip())
        if file_match and not progress_match:
            self.status.update(current_file=file_match.group(1))

        if "sent" in line.lower() and "received" in line.lower():
            if not hasattr(self.status, '_files_counted'):
                self.status._files_counted = 0
            self.status._files_counted += 1
            self.status.update(files_transferred=self.status._files_counted)

    def encrypt_backup_directory(self, backup_dir: Path) -> Path:
        """
        Encrypt backup directory if encryption is enabled.
        
        Args:
            backup_dir: Backup directory to encrypt
        
        Returns:
            Path to encrypted backup directory, or original if encryption is disabled.

        Raises:
            RuntimeError: If encryption is enabled but encryption fails.
        """
        if not self.config.encryption.enabled:
            return backup_dir
        
        try:
            logger.info("Encrypting backup directory...")
            self.status.update(action="Encrypting backup...", item="")
            self.status.set_encryption_status("encrypting")
            
            encryption_mgr = EncryptionManager(self.config.encryption)
            encrypted_dir = encryption_mgr.encrypt_backup(backup_dir)
            
            if encrypted_dir != backup_dir:
                logger.info(f"Backup encrypted: {encrypted_dir}")
                self.status.update(action="Backup encrypted successfully", item="")
                self.status.set_encryption_status("encrypted")
                return encrypted_dir
            else:
                self.status.set_encryption_status("failed")
                message = "Encryption failed"
                self.status.add_error(message)
                raise RuntimeError(message)
        except Exception as e:
            logger.error(f"Encryption error: {e}")
            message = f"Encryption failed: {e}"
            if message not in self.status.errors:
                self.status.add_error(message)
            self.status.set_encryption_status("failed")
            raise RuntimeError(message) from e
    
    def upload_to_remotes(
        self,
        backup_path: Path,
        backup_name: str,
        remotes: List,
    ) -> bool:
        """Upload backup to remote destinations."""
        if not remotes:
            return True
        
        all_succeeded = True
        self.status.update(
            action=f"Uploading to {len(remotes)} remote destination(s)",
            item="",
        )
        
        for remote in remotes:
            if self.status.get_status() == "cancelled":
                all_succeeded = False
                break
            
            logger.info(f"Uploading to remote: {remote.name} ({remote.type})")
            self.status.update(
                action=f"Uploading to {remote.name}...",
                item=remote.name,
            )
            
            self.status.set_item_status("remote", remote.name, "uploading")
            
            # Create progress callback for TUI updates
            def progress_callback(line: str):
                """Parse rclone progress and update status."""
                if "Transferred:" in line or "Speed:" in line:
                    # Update status with progress info
                    self.status.update(item=f"{remote.name}: {line.strip()[:50]}")
            
            success = self.remote_mgr.upload_backup(remote, backup_path, backup_name, progress_callback)
            
            if success:
                logger.info(f"Successfully uploaded to {remote.name}")
                self.status.set_item_status("remote", remote.name, "success")
                
                # Check storage quota and cleanup if needed
                try:
                    # remote.path is a string, not a Path
                    # For quota checking, we need the base remote path
                    quota_path = Path(remote.path).expanduser() if remote.type == "local" else Path("/tmp")
                    quota_status = self.rotation.check_storage_quota(remote, quota_path)
                    
                    if quota_status["cleanup_needed"]:
                        logger.warning(f"Storage quota exceeded for {remote.name}, starting cleanup")
                        self.status.add_warning(f"Storage quota exceeded for {remote.name}, cleaning up old backups")
                        
                        # Get list of backups
                        backups = self.remote_mgr.list_backups(remote)
                        if backups:
                            # Filter backups by retention policy
                            # filter_backups_by_retention expects a Path but only uses it for local remotes
                            rotation_path = Path(remote.path).expanduser() if remote.type == "local" else Path("/tmp")
                            to_keep, to_delete = self.rotation.filter_backups_by_retention(backups, rotation_path)
                            if to_delete:
                                # For cleanup, use the actual remote path
                                cleanup_path = Path(remote.path).expanduser() if remote.type == "local" else Path("/tmp")
                                deleted_count = self.rotation.cleanup_old_backups(remote, cleanup_path, to_delete)
                                logger.info(f"Cleaned up {deleted_count} old backup(s) from {remote.name}")
                                self.status.add_warning(f"Cleaned up {deleted_count} old backup(s) from {remote.name}")
                    elif quota_status["warning"]:
                        logger.warning(f"Storage quota warning for {remote.name}: {quota_status['percent']:.1f}% used")
                        self.status.add_warning(f"Storage quota warning for {remote.name}: {quota_status['percent']:.1f}% used")
                except Exception as e:
                    logger.error(f"Error during rotation check for {remote.name}: {e}")
                    # Don't fail the upload if rotation check fails
            else:
                logger.error(f"Failed to upload to {remote.name}")
                all_succeeded = False
                self.status.set_item_status("remote", remote.name, "failed")
                self.status.add_error(f"Failed to upload to {remote.name}")
        return all_succeeded
