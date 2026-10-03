"""
Docker restore functionality.
Handles restoring containers, volumes, networks, and metadata from backups.
"""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import docker
from docker.errors import DockerException, APIError

from .archive import is_solid_archive_name, strip_solid_archive_suffix, unpack_solid_archive
from .config import Config
from .logging import get_logger
from .encryption import EncryptionManager
from .manifest import verify_backup_manifest

logger = get_logger('restore')


VOLUME_ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")


def _strip_volume_archive_suffix(filename: str) -> Optional[str]:
    for suffix in VOLUME_ARCHIVE_SUFFIXES:
        if filename.endswith(suffix):
            return filename[:-len(suffix)]
    return None


def list_volume_backup_names(backup_path: Path) -> List[str]:
    """List volume backup names stored as directories or supported tar archives."""
    volumes_dir = Path(backup_path) / "volumes"
    if not volumes_dir.exists():
        return []

    names = set()
    for item in volumes_dir.iterdir():
        if item.is_dir():
            names.add(item.name)
            continue
        if item.is_file():
            archive_name = _strip_volume_archive_suffix(item.name)
            if archive_name:
                names.add(archive_name)
    return sorted(names)


def list_available_backups(backup_dir: Path) -> List[Dict]:
    """List available local backups in a directory without connecting to Docker."""
    backups = []
    if not backup_dir.exists():
        return backups
    items = list(backup_dir.iterdir())
    for backup_path in sorted(items, key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if backup_path.is_dir() and backup_path.name.startswith("backup_"):
            metadata_file = backup_path / "backup_metadata.json"
            timestamp = None
            if metadata_file.exists():
                try:
                    with open(metadata_file, 'r') as f:
                        metadata = json.load(f)
                        timestamp = metadata.get("timestamp")
                except Exception:
                    pass
            if not timestamp:
                try:
                    name_part = backup_path.name.replace("backup_", "")
                    timestamp = datetime.strptime(name_part, "%Y%m%d_%H%M%S").isoformat()
                except Exception:
                    timestamp = backup_path.name
            backups.append({"name": backup_path.name, "path": backup_path, "timestamp": timestamp})
        elif backup_path.is_file() and is_solid_archive_name(backup_path.name):
            try:
                base = strip_solid_archive_suffix(backup_path.name)
                name_part = base.replace("backup_", "")
                timestamp = datetime.strptime(name_part, "%Y%m%d_%H%M%S").isoformat()
            except Exception:
                timestamp = backup_path.name
            backups.append({"name": backup_path.name, "path": backup_path, "timestamp": timestamp})
    return backups


class DockerRestore:
    """Docker restore manager."""
    
    def __init__(self, config: Config):
        self.config = config
        try:
            # Apply timeout from config if specified
            timeout = config.data.get("docker", {}).get("timeout", 300)
            self.client = docker.from_env(timeout=timeout)
        except DockerException as e:
            raise RuntimeError(f"Failed to connect to Docker: {e}")
    
    def list_backups(self, backup_dir: Path) -> List[Dict]:
        """List available backups in backup directory (dirs and solid archive files)."""
        return list_available_backups(backup_dir)
    
    def restore_container_config(self, container_name: str, backup_path: Path, new_name: Optional[str] = None) -> bool:
        """Restore container from backup configuration."""
        try:
            config_file = backup_path / "configs" / f"{container_name}_config.json"
            if not config_file.exists():
                return False
            
            with open(config_file, 'r') as f:
                container_config = json.load(f)
            
            # Extract container configuration
            image = container_config.get("Config", {}).get("Image", "")
            if not image:
                return False
            
            # Get environment variables
            env_vars = container_config.get("Config", {}).get("Env", [])
            env_dict = {}
            for env in env_vars:
                if "=" in env:
                    key, value = env.split("=", 1)
                    env_dict[key] = value
            
            # Get port mappings
            port_bindings = {}
            if "NetworkSettings" in container_config and "Ports" in container_config["NetworkSettings"]:
                ports = container_config["NetworkSettings"]["Ports"]
                for container_port, host_ports in ports.items():
                    if host_ports:
                        port_bindings[container_port] = host_ports[0]["HostPort"]
            
            # Get volume mounts
            mounts = []
            if "Mounts" in container_config:
                for mount in container_config["Mounts"]:
                    if mount.get("Type") == "volume":
                        mounts.append(f"{mount['Name']}:{mount['Destination']}")
                    elif mount.get("Type") == "bind":
                        mounts.append(f"{mount['Source']}:{mount['Destination']}")
            
            # Get restart policy
            restart_policy = container_config.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no")
            
            # Get network
            networks = []
            if "NetworkSettings" in container_config and "Networks" in container_config["NetworkSettings"]:
                networks = list(container_config["NetworkSettings"]["Networks"].keys())
            
            # Stop and remove existing container if it exists
            target_name = new_name or container_name
            try:
                existing = self.client.containers.get(target_name)
                existing.stop()
                existing.remove()
            except APIError:
                pass  # Container doesn't exist
            
            # Create new container
            container = self.client.containers.create(
                image=image,
                name=target_name,
                environment=env_dict,
                ports=port_bindings,
                volumes=mounts,
                restart_policy={"Name": restart_policy},
                detach=True,
            )
            
            # Connect to networks
            for network_name in networks:
                try:
                    network = self.client.networks.get(network_name)
                    network.connect(container)
                except APIError:
                    pass  # Network might not exist
            
            return True
        except Exception:
            return False
    
    def restore_volume(self, volume_name: str, backup_path: Path, new_name: Optional[str] = None) -> bool:
        """Restore Docker volume from backup."""
        temp_extract_dir: Optional[Path] = None
        try:
            volume_backup_dir = backup_path / "volumes" / volume_name
            if not volume_backup_dir.exists():
                archive_path = self._find_volume_archive(backup_path, volume_name)
                if archive_path is None:
                    return False
                temp_extract_dir = Path(tempfile.mkdtemp(prefix=f"bbackup_volume_{volume_name}_"))
                with tarfile.open(archive_path, self._tar_read_mode(archive_path)) as tar:
                    tar.extractall(temp_extract_dir, filter="data")
                extracted_volume_dir = temp_extract_dir / volume_name
                volume_backup_dir = extracted_volume_dir if extracted_volume_dir.exists() else temp_extract_dir

            if not volume_backup_dir.exists():
                return False
            
            target_volume_name = new_name or volume_name

            existing_volume = None
            try:
                existing_volume = self.client.volumes.get(target_volume_name)
            except APIError:
                pass  # Volume doesn't exist

            staging_volume = None
            staging_volume_name = f"bbackup_restore_stage_{target_volume_name}_{os.getpid()}"
            if existing_volume is not None:
                staging_volume = self.client.volumes.create(name=staging_volume_name)
                if not self._copy_backup_dir_to_volume(volume_backup_dir, staging_volume_name, volume_name):
                    try:
                        staging_volume.remove()
                    except Exception:
                        pass
                    return False
                try:
                    staging_volume.remove()
                except Exception:
                    pass
                existing_volume.remove()

            self.client.volumes.create(name=target_volume_name)
            return self._copy_backup_dir_to_volume(volume_backup_dir, target_volume_name, volume_name)
                
        except APIError:
            return False
        except (tarfile.TarError, OSError) as e:
            logger.error(f"Failed to read volume archive for {volume_name}: {e}")
            return False
        finally:
            if temp_extract_dir is not None and temp_extract_dir.exists():
                try:
                    shutil.rmtree(temp_extract_dir)
                except OSError:
                    pass

    def _copy_backup_dir_to_volume(
        self,
        volume_backup_dir: Path,
        target_volume_name: str,
        source_volume_name: str,
    ) -> bool:
        """Copy backup data into a Docker volume through a temporary container."""
        temp_container_name = f"bbackup_restore_{target_volume_name}_{os.getpid()}"
        try:
            temp_container = self.client.containers.run(
                "alpine:latest",
                command="sleep 3600",
                name=temp_container_name,
                volumes={target_volume_name: {"bind": "/volume_data", "mode": "rw"}},
                detach=True,
                remove=False,
            )

            import time
            time.sleep(1)

            copy_result = subprocess.run(
                ["docker", "cp", str(volume_backup_dir) + "/.", f"{temp_container_name}:/volume_data/"],
                capture_output=True,
                text=True,
                check=False,
            )

            temp_container.stop()
            temp_container.remove()

            if copy_result.returncode != 0:
                logger.error(
                    f"docker cp restore failed for volume {source_volume_name}: {copy_result.stderr.strip()}"
                )
                return False

            return True
        except Exception:
            try:
                temp_container = self.client.containers.get(temp_container_name)
                temp_container.stop()
                temp_container.remove()
            except Exception:
                pass
            return False

    def _find_volume_archive(self, backup_path: Path, volume_name: str) -> Optional[Path]:
        volumes_dir = backup_path / "volumes"
        for suffix in VOLUME_ARCHIVE_SUFFIXES:
            archive_path = volumes_dir / f"{volume_name}{suffix}"
            if archive_path.exists():
                return archive_path
        return None

    def _tar_read_mode(self, archive_path: Path) -> str:
        if archive_path.name.endswith(".tar.bz2"):
            return "r:bz2"
        if archive_path.name.endswith(".tar.xz"):
            return "r:xz"
        return "r:gz"
    
    def restore_network(self, network_name: str, backup_path: Path, new_name: Optional[str] = None) -> bool:
        """Restore network configuration."""
        try:
            network_file = backup_path / "networks" / f"{network_name}.json"
            if not network_file.exists():
                return False
            
            with open(network_file, 'r') as f:
                network_config = json.load(f)
            
            target_name = new_name or network_name
            
            # Remove existing network if it exists (except default networks)
            if target_name not in ["bridge", "host", "none"]:
                try:
                    existing = self.client.networks.get(target_name)
                    existing.remove()
                except APIError:
                    pass  # Network doesn't exist
                
                # Create network
                driver = network_config.get("Driver", "bridge")
                options = network_config.get("Options", {})
                ipam = network_config.get("IPAM", {})
                
                self.client.networks.create(
                    name=target_name,
                    driver=driver,
                    options=options,
                    ipam=ipam,
                )
            
            return True
        except APIError:
            return False
    
    def decrypt_backup_directory(self, backup_path: Path) -> Path:
        """
        Decrypt backup directory if encrypted.
        
        Args:
            backup_path: Backup directory path (may be encrypted)
        
        Returns:
            Path to decrypted backup directory (or original if not encrypted)
        """
        # Check if backup is encrypted
        metadata_file = backup_path / "encryption_metadata.json"
        if not metadata_file.exists():
            logger.debug("Backup appears to be unencrypted")
            return backup_path
        
        if not self.config.encryption.enabled:
            logger.warning("Backup is encrypted but encryption is disabled in config")
            return backup_path
        
        try:
            logger.info("Decrypting backup directory...")
            encryption_mgr = EncryptionManager(self.config.encryption)
            decrypted_dir = encryption_mgr.decrypt_backup(backup_path)
            
            if decrypted_dir != backup_path:
                logger.info(f"Backup decrypted: {decrypted_dir}")
                return decrypted_dir
            else:
                logger.error("Decryption failed")
                return backup_path
        except Exception as e:
            logger.error(f"Decryption error: {e}")
            return backup_path
    
    def restore_filesystem_path(
        self,
        target_name: str,
        backup_path: Path,
        destination: Optional[Path] = None,
    ) -> bool:
        """
        Restore a filesystem backup using rsync.

        Args:
            target_name: The target name under backup_path/filesystems/.
            backup_path: Root backup directory for this run.
            destination: Destination path to restore into. Required; caller must supply
                         the original path since it is not stored in the backup structure.

        Returns:
            True on success, False on any failure.
        """
        filesystems_root = backup_path / "filesystems"
        target_path = Path(target_name)
        if target_path.is_absolute():
            logger.error(f"Invalid absolute filesystem restore target: {target_name}")
            return False
        try:
            if filesystems_root.is_symlink():
                logger.error(f"Filesystem restore root is a symlink: {filesystems_root}")
                return False
            resolved_backup_root = Path(backup_path).resolve()
            resolved_root = filesystems_root.resolve()
            src = (filesystems_root / target_path).resolve()
        except (OSError, RuntimeError) as exc:
            logger.error(f"Failed to resolve filesystem restore target '{target_name}': {exc}")
            return False

        if (
            resolved_root == resolved_backup_root
            or not resolved_root.is_relative_to(resolved_backup_root)
        ):
            logger.error(f"Filesystem restore root escapes backup root: {filesystems_root}")
            return False

        if src == resolved_root or not src.is_relative_to(resolved_root):
            logger.error(f"Filesystem restore target escapes backup root: {target_name}")
            return False
        if not src.exists():
            logger.error(f"Filesystem backup not found: {src}")
            return False

        if destination is None:
            logger.error(f"No destination specified for filesystem restore: {target_name}")
            return False

        destination = Path(destination)
        try:
            destination.mkdir(parents=True, exist_ok=True)
            cmd = ["rsync", "-av", "--delete", str(src) + "/", str(destination) + "/"]
            result = subprocess.run(cmd, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(f"Filesystem restore failed for '{target_name}': {exc}")
            return False

        if result.returncode != 0:
            logger.error(f"rsync restore failed for '{target_name}': {result.stderr.strip()}")
        return result.returncode == 0

    def restore_backup(
        self,
        backup_path: Path,
        containers: Optional[List[str]] = None,
        volumes: Optional[List[str]] = None,
        networks: Optional[List[str]] = None,
        filesystems: Optional[List[str]] = None,
        filesystem_destination: Optional[Path] = None,
        rename_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, any]:
        """Restore complete backup (from directory or solid archive file)."""
        backup_path = Path(backup_path)
        temp_root_to_remove: Optional[Path] = None

        if backup_path.is_file() and is_solid_archive_name(backup_path.name):
            if backup_path.name.endswith(".enc") or ".enc" in backup_path.suffixes:
                if not self.config.encryption.enabled:
                    raise ValueError(
                        "Backup file is encrypted; set encryption.enabled to true and ensure keys are configured."
                    )
            unpacked, temp_root_to_remove = unpack_solid_archive(
                backup_path,
                dest_dir=None,
                encryption_config=self.config.encryption if self.config.encryption.enabled else None,
            )
            backup_path = unpacked

        try:
            # Decrypt backup dir if encrypted (per-file; no-op for unpacked solid archive)
            backup_path = self.decrypt_backup_directory(backup_path)

            manifest_errors = verify_backup_manifest(backup_path)
            if manifest_errors:
                return {
                    "containers": {},
                    "volumes": {},
                    "networks": {},
                    "filesystems": {},
                    "errors": manifest_errors,
                }

            rename_map = rename_map or {}

            results = {
                "containers": {},
                "volumes": {},
                "networks": {},
                "filesystems": {},
                "errors": [],
            }

            # Restore containers
            if containers is not None:
                configs_dir = backup_path / "configs"
                if configs_dir.exists():
                    for container_name in containers:
                        new_name = rename_map.get(container_name)
                        success = self.restore_container_config(container_name, backup_path, new_name)
                        results["containers"][container_name] = "success" if success else "failed"
                        if not success:
                            results["errors"].append(f"Failed to restore container: {container_name}")

            # Restore volumes
            if volumes is not None:
                volumes_dir = backup_path / "volumes"
                if volumes_dir.exists():
                    for volume_name in volumes:
                        new_name = rename_map.get(volume_name)
                        success = self.restore_volume(volume_name, backup_path, new_name)
                        results["volumes"][volume_name] = "success" if success else "failed"
                        if not success:
                            results["errors"].append(f"Failed to restore volume: {volume_name}")

            # Restore networks
            if networks is not None:
                networks_dir = backup_path / "networks"
                if networks_dir.exists():
                    for network_name in networks:
                        new_name = rename_map.get(network_name)
                        success = self.restore_network(network_name, backup_path, new_name)
                        results["networks"][network_name] = "success" if success else "failed"
                        if not success:
                            results["errors"].append(f"Failed to restore network: {network_name}")

            # Restore filesystem paths
            if filesystems is not None:
                for target_name in filesystems:
                    success = self.restore_filesystem_path(target_name, backup_path, filesystem_destination)
                    results["filesystems"][target_name] = "success" if success else "failed"
                    if not success:
                        results["errors"].append(f"Failed to restore filesystem: {target_name}")

            return results
        finally:
            if temp_root_to_remove is not None and temp_root_to_remove.exists():
                try:
                    shutil.rmtree(temp_root_to_remove)
                except OSError:
                    pass
