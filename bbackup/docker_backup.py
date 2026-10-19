"""
Docker backup functionality.
Handles backing up containers, volumes, networks, and metadata.
"""

import os
import json
import shutil
import subprocess
import tarfile
import io
from pathlib import Path
from typing import List, Dict, Optional, Set, Callable
from datetime import datetime
import docker
from docker.errors import DockerException, APIError

from .config import BackupScope, Config, ensure_private_directory, open_private_file
from .logging import get_logger
from .container_images import ALPINE_HELPER_IMAGE
from .archive import extract_archive_safely
from .encryption import EncryptionManager

logger = get_logger('docker_backup')


class DockerBackup:
    """Docker backup manager."""
    
    def __init__(self, config: Config):
        self.config = config
        try:
            # Apply timeout from config if specified
            timeout = config.data.get("docker", {}).get("timeout", 300)
            self.client = docker.from_env(timeout=timeout)
        except DockerException as e:
            raise RuntimeError(f"Failed to connect to Docker: {e}")
    
    def get_all_containers(self) -> List[Dict]:
        """Get list of all containers."""
        try:
            containers = self.client.containers.list(all=True)
            return [
                {
                    "id": c.id,
                    "name": c.name,
                    "status": c.status,
                    "image": c.image.tags[0] if c.image.tags else "unknown",
                }
                for c in containers
            ]
        except APIError as e:
            raise RuntimeError(f"Failed to list containers: {e}")
    
    def get_all_volumes(self) -> List[Dict]:
        """Get list of all volumes."""
        try:
            volumes = self.client.volumes.list()
            return [
                {
                    "name": v.name,
                    "driver": v.attrs.get("Driver", "local"),
                    "mountpoint": v.attrs.get("Mountpoint", ""),
                }
                for v in volumes
            ]
        except APIError as e:
            raise RuntimeError(f"Failed to list volumes: {e}")
    
    def get_all_networks(self) -> List[Dict]:
        """Get list of all networks."""
        try:
            networks = self.client.networks.list()
            return [
                {
                    "id": n.id,
                    "name": n.name,
                    "driver": n.attrs.get("Driver", ""),
                }
                for n in networks
                if not n.name.startswith("bridge") and n.name != "host" and n.name != "none"
            ]
        except APIError as e:
            raise RuntimeError(f"Failed to list networks: {e}")
    
    def backup_container_config(self, container_name: str, backup_dir: Path) -> bool:
        """Backup container configuration (inspect data)."""
        logger.debug(f"Backing up container config: {container_name}")
        try:
            container = self.client.containers.get(container_name)
            inspect_data = container.attrs
            
            config_file = backup_dir / f"{container_name}_config.json"
            with open_private_file(config_file, 'w', encoding="utf-8") as f:
                json.dump(inspect_data, f, indent=2)
            
            # Also save logs
            try:
                logs = container.logs(tail=1000).decode('utf-8', errors='replace')
                log_file = backup_dir / f"{container_name}_logs.txt"
                with open_private_file(log_file, 'w', encoding="utf-8") as f:
                    f.write(logs)
            except Exception:
                pass  # Logs might not be available
            
            logger.debug(f"Successfully backed up container config: {container_name}")
            return True
        except APIError as e:
            logger.error(f"API error backing up container config {container_name}: {e}")
            return False
    
    def backup_volume(self, volume_name: str, backup_dir: Path, incremental: bool = False,
                     progress_callback: Optional[Callable[[str], None]] = None) -> bool:
        """Backup Docker volume using Docker container and rsync."""
        logger.info(f"Backing up volume: {volume_name} (incremental={incremental})")
        try:
            volume = self.client.volumes.get(volume_name)
            if self.config.encryption.enabled:
                try:
                    mountpoint = volume.attrs.get("Mountpoint") if isinstance(volume.attrs, dict) else None
                    if not mountpoint or not Path(mountpoint).is_dir():
                        raise OSError("Cannot inspect original Docker volume attributes before encryption")
                    EncryptionManager.validate_directory_tree(Path(mountpoint))
                except OSError as exc:
                    logger.error("Docker volume source preflight failed for %s: %s", volume_name, exc)
                    return False
            volumes_dir = backup_dir / "volumes"
            ensure_private_directory(volumes_dir)
            volume_backup_dir = volumes_dir / volume_name
            ensure_private_directory(volume_backup_dir)
            
            # Use a temporary container to access the volume
            # This avoids permission issues with direct mountpoint access
            temp_container_name = f"bbackup_temp_{volume_name}_{os.getpid()}"
            temp_tar: Optional[Path] = None
            
            try:
                # Create temporary container with volume mounted
                temp_container = self.client.containers.run(
                    ALPINE_HELPER_IMAGE,
                    command="sleep 3600",  # Keep container running
                    name=temp_container_name,
                    volumes={volume_name: {"bind": "/volume_data", "mode": "ro"}},
                    detach=True,
                    remove=False,
                )
                
                # Wait a moment for container to start
                import time
                time.sleep(1)
                
                # Use rsync via docker exec to copy from container
                # First, check if rsync is available in alpine
                # If not, use tar for backup
                check_rsync = subprocess.run(
                    ["docker", "exec", temp_container_name, "which", "rsync"],
                    capture_output=True,
                    text=True,
                )
                
                if check_rsync.returncode == 0 and os.geteuid() == 0:
                    # Non-root docker cp -a can silently discard numeric owners.
                    # Use the ownership-checked tar path unless root can retain them.
                    # Use rsync if available
                    # Add --progress and --info=progress2 for detailed progress
                    rsync_cmd = [
                        "docker", "exec", temp_container_name,
                        "rsync", "-av", "--delete", "--progress", "--info=progress2",
                        "/volume_data/", "/tmp/backup/"
                    ]
                    
                    # Add --link-dest for incremental backups
                    if incremental:
                        prev_backup = self._find_previous_volume_backup(volume_name, backup_dir.parent)
                        if prev_backup:
                            # Mount previous backup as read-only in container for --link-dest
                            # We need to copy the path into the container's filesystem
                            # Since we can't easily mount host paths, we'll use the host path directly
                            # by copying previous backup into container first
                            prev_backup_in_container = f"/tmp/prev_backup_{volume_name}"
                            subprocess.run(
                                ["docker", "exec", temp_container_name, "mkdir", "-p", prev_backup_in_container],
                                check=False,
                            )
                            # Copy previous backup into container
                            subprocess.run(
                                ["docker", "cp", f"{str(prev_backup)}/.", f"{temp_container_name}:{prev_backup_in_container}/"],
                                check=False,
                            )
                            rsync_cmd.extend(["--link-dest", prev_backup_in_container])
                    
                    # Create backup directory in container
                    subprocess.run(
                        ["docker", "exec", temp_container_name, "mkdir", "-p", "/tmp/backup"],
                        check=False,
                    )
                    
                    # Run rsync with real-time output parsing for progress
                    if progress_callback:
                        process = subprocess.Popen(
                            rsync_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1,
                            universal_newlines=True
                        )
                        
                        # Parse rsync progress output
                        result_code = 0
                        for line in process.stdout:
                            if progress_callback:
                                progress_callback(line)
                            # Check for completion
                            if process.poll() is not None:
                                result_code = process.returncode
                                break
                        
                        # Wait for process to complete
                        if process.poll() is None:
                            result_code = process.wait()
                        
                        # Create a result-like object
                        class Result:
                            def __init__(self, code):
                                self.returncode = code
                        result = Result(result_code)
                    else:
                        result = subprocess.run(rsync_cmd, capture_output=True, text=True, check=False)
                    
                    if result.returncode == 0:
                        # Copy from container to host
                        copy_result = subprocess.run(
                            ["docker", "cp", "-a", f"{temp_container_name}:/tmp/backup/.", str(volume_backup_dir)],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if copy_result.returncode != 0:
                            logger.error(
                                f"docker cp backup failed for volume {volume_name}: {copy_result.stderr.strip()}"
                            )
                            raise RuntimeError(f"docker cp failed for volume {volume_name}")
                    else:
                        stderr = getattr(result, "stderr", "")
                        logger.error(f"rsync backup failed for volume {volume_name}: {stderr.strip()}")
                        raise RuntimeError(f"rsync failed for volume {volume_name}")
                else:
                    # Tar verifies every original numeric owner before extraction.
                    tar_cmd = [
                        "docker", "exec", temp_container_name,
                        "tar", "czf", "/tmp/volume_backup.tar.gz", "-C", "/volume_data", "."
                    ]
                    result = subprocess.run(tar_cmd, capture_output=True, text=True, check=False)
                    
                    if result.returncode == 0:
                        # Copy tar from container and extract
                        temp_tar = backup_dir / "volumes" / f"{volume_name}.tar.gz"
                        copy_result = subprocess.run(
                            ["docker", "cp", f"{temp_container_name}:/tmp/volume_backup.tar.gz", str(temp_tar)],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if copy_result.returncode != 0:
                            logger.error(
                                f"docker cp tar backup failed for volume {volume_name}: {copy_result.stderr.strip()}"
                            )
                            raise RuntimeError(f"docker cp failed for volume {volume_name}")
                        # Extract tar (tarfile imported at module level)
                        with tarfile.open(temp_tar, "r:gz") as tar:
                            extract_archive_safely(
                                tar,
                                volume_backup_dir,
                            )
                        temp_tar.unlink()
                    else:
                        stderr = getattr(result, "stderr", "")
                        logger.error(f"tar backup failed for volume {volume_name}: {stderr.strip()}")
                        raise RuntimeError(f"tar failed for volume {volume_name}")
                
                # Cleanup
                temp_container.stop()
                temp_container.remove()
                
                # Apply compression if enabled
                compression = self.config.data.get("backup", {}).get("compression", {})
                if compression.get("enabled", False) and volume_backup_dir.exists():
                    # Determine compression format
                    format_map = {
                        "gzip": "gz",
                        "bzip2": "bz2",
                        "xz": "xz"
                    }
                    comp_format = compression.get("format", "gzip")
                    comp_ext = format_map.get(comp_format, "gz")
                    
                    # Create compressed tar archive
                    tar_file = backup_dir / "volumes" / f"{volume_name}.tar.{comp_ext}"
                    mode_map = {
                        "gzip": "w:gz",
                        "bzip2": "w:bz2",
                        "xz": "w:xz"
                    }
                    mode = mode_map.get(comp_format, "w:gz")
                    
                    with open_private_file(tar_file, "wb") as archive_file:
                        with tarfile.open(fileobj=archive_file, mode=mode) as tar:
                            tar.add(volume_backup_dir, arcname=volume_name)
                    
                    # Remove uncompressed directory
                    shutil.rmtree(volume_backup_dir)
                
                return True
                
            except Exception as e:
                # Cleanup on error
                try:
                    temp_container = self.client.containers.get(temp_container_name)
                    temp_container.stop()
                    temp_container.remove()
                except Exception as cleanup_error:
                    logger.error(f"Error during cleanup: {cleanup_error}")
                if volume_backup_dir.exists():
                    try:
                        shutil.rmtree(volume_backup_dir)
                    except OSError as cleanup_error:
                        logger.error(f"Error removing failed volume backup artifact: {cleanup_error}")
                if temp_tar is not None:
                    temp_tar.unlink(missing_ok=True)
                logger.error(f"Failed to backup volume {volume_name}: {e}")
                return False
                
        except APIError:
            return False
    
    def _find_previous_volume_backup(self, volume_name: str, backups_root: Path) -> Optional[Path]:
        """Find previous backup of volume for incremental backup."""
        if not backups_root.exists():
            return None
        
        # Look for most recent backup
        backup_dirs = sorted(
            [d for d in backups_root.iterdir() if d.is_dir()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        
        for backup_dir in backup_dirs:
            prev_volume_dir = backup_dir / "volumes" / volume_name
            if prev_volume_dir.exists():
                return prev_volume_dir
        
        return None
    
    def backup_network(self, network_name: str, backup_dir: Path) -> bool:
        """Backup network configuration."""
        try:
            network = self.client.networks.get(network_name)
            network_data = network.attrs
            
            network_file = backup_dir / "networks" / f"{network_name}.json"
            ensure_private_directory(network_file.parent)
            
            with open_private_file(network_file, 'w', encoding="utf-8") as f:
                json.dump(network_data, f, indent=2)
            
            logger.debug(f"Successfully backed up network: {network_name}")
            return True
        except APIError as e:
            logger.error(f"API error backing up network {network_name}: {e}")
            return False
    
    def create_metadata_archive(self, backup_dir: Path, output_file: Path) -> bool:
        """Create compressed tar archive of metadata (configs, networks)."""
        try:
            compression = self.config.data.get("backup", {}).get("compression", {})
            format = compression.get("format", "gzip")
            
            mode_map = {
                "gzip": "w:gz",
                "bzip2": "w:bz2",
                "xz": "w:xz",
            }
            mode = mode_map.get(format, "w:gz")
            
            ensure_private_directory(backup_dir)
            with open_private_file(output_file, "wb") as archive_file:
                with tarfile.open(fileobj=archive_file, mode=mode) as tar:
                    # Add configs
                    configs_dir = backup_dir / "configs"
                    if configs_dir.exists():
                        tar.add(configs_dir, arcname="configs")

                    # Add networks
                    networks_dir = backup_dir / "networks"
                    if networks_dir.exists():
                        tar.add(networks_dir, arcname="networks")

                    metadata = {
                        "timestamp": datetime.now().isoformat(),
                        "backup_version": "1.0",
                        "docker_version": self.client.version().get("Version", "unknown"),
                    }
                    metadata_bytes = json.dumps(metadata, indent=2).encode("utf-8")
                    metadata_info = tarfile.TarInfo("backup_metadata.json")
                    metadata_info.size = len(metadata_bytes)
                    metadata_info.mode = 0o600
                    tar.addfile(metadata_info, io.BytesIO(metadata_bytes))
            
            return True
        except Exception:
            return False
    
    def create_backup(
        self,
        backup_dir: Path,
        containers: Optional[List[str]] = None,
        scope: Optional[BackupScope] = None,
        incremental: bool = False,
    ) -> Dict[str, any]:
        """Create complete backup."""
        if scope is None:
            scope = self.config.scope
        
        backup_dir = Path(backup_dir)
        ensure_private_directory(backup_dir)
        
        results = {
            "containers": {},
            "volumes": {},
            "networks": {},
            "errors": [],
        }
        
        # Backup containers
        if scope.containers or scope.configs:
            containers_to_backup = containers or [c["name"] for c in self.get_all_containers()]
            
            configs_dir = backup_dir / "configs"
            ensure_private_directory(configs_dir)
            
            for container_name in containers_to_backup:
                if scope.configs:
                    success = self.backup_container_config(container_name, configs_dir)
                    results["containers"][container_name] = "success" if success else "failed"
                    if not success:
                        results["errors"].append(f"Failed to backup container config: {container_name}")
        
        # Backup volumes
        if scope.volumes:
            volumes_dir = backup_dir / "volumes"
            ensure_private_directory(volumes_dir)
            
            # Get volumes for selected containers
            if containers:
                container_volumes = self._get_container_volumes(containers)
            else:
                container_volumes = [v["name"] for v in self.get_all_volumes()]
            
            for volume_name in container_volumes:
                success = self.backup_volume(volume_name, backup_dir, incremental)
                results["volumes"][volume_name] = "success" if success else "failed"
                if not success:
                    results["errors"].append(f"Failed to backup volume: {volume_name}")
        
        # Backup networks
        if scope.networks:
            networks = self.get_all_networks()
            for network in networks:
                success = self.backup_network(network["name"], backup_dir)
                results["networks"][network["name"]] = "success" if success else "failed"
                if not success:
                    results["errors"].append(f"Failed to backup network: {network['name']}")
        
        return results
    
    def _get_container_volumes(self, container_names: List[str]) -> Set[str]:
        """Get volume names used by containers."""
        volumes = set()
        for container_name in container_names:
            try:
                container = self.client.containers.get(container_name)
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    if mount.get("Type") == "volume":
                        volumes.add(mount.get("Name"))
            except APIError:
                continue
        return volumes
