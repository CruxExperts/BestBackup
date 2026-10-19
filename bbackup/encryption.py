"""
Encryption module for bbackup.
Handles symmetric (AES-256-GCM) and asymmetric (RSA/ECDSA) encryption.
Supports URL-based key fetching for easy multi-server deployment.
"""

import os
import hashlib
import json
import base64
import hmac as stdlib_hmac
import stat
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Any, Dict, List
from datetime import datetime
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.backends import default_backend
import secrets

from .logging import get_logger
from .config import ensure_private_directory, open_private_file

logger = get_logger('encryption')

TREE_FORMAT = "bbackup-encrypted-tree"
TREE_VERSION = 3
TREE_PAYLOAD_MAGIC = b"BBAKTR3\0"
TREE_IDENTITY_NAME = ".bbackup-tree-v3"
TREE_INVENTORY_NAME = ".inventory-v3"
TREE_INVENTORY_MAGIC = b"BBAKIN3\0"
TREE_V2_PAYLOAD_MAGIC = b"BBAKTR2\0"
TREE_V2_IDENTITY_NAME = ".bbackup-tree-v2"
TREE_METADATA_NAME = "encryption_metadata.json"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _safe_tree_path(value: Any) -> Optional[Path]:
    if value == ".":
        return Path(".")
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    posix = Path(value)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return posix


def _check_owned_directory(path: Path, *, private: bool = False, empty: bool = False) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"Staging root is not a real directory: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise OSError(f"Staging root is owned by another user: {path}")
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(f"Staging root permissions are not private: {path}")
    if empty and any(path.iterdir()):
        raise OSError(f"Staging destination is not empty: {path}")


def _create_private_directory(path: Path) -> None:
    ensure_private_directory(path)
    _check_owned_directory(path, private=True)


def _read_regular_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source_file:
        if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
            raise OSError(f"Expected a regular file: {path}")
        return source_file.read()


def _read_regular_file_prefix(path: Path, size: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source_file:
        if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
            raise OSError(f"Expected a regular file: {path}")
        return source_file.read(size)


def _raise_walk_error(error: OSError) -> None:
    """Make os.walk traversal failures fail closed instead of skipping entries."""
    raise error


class EncryptionManager:
    """Main encryption manager for backup encryption/decryption."""
    
    def __init__(self, encryption_config: Any):
        """
        Initialize encryption manager.
        
        Args:
            encryption_config: EncryptionSettings dataclass from config
        """
        self.config = encryption_config
        self.symmetric_key: Optional[bytes] = None
        self.public_key: Optional[Any] = None
        self.private_key: Optional[Any] = None
        
        # Load keys based on configuration
        if encryption_config.method in ['symmetric', 'both']:
            self.symmetric_key = self._load_symmetric_key()
        
        if encryption_config.method in ['asymmetric', 'both']:
            self.public_key = self._load_public_key()
            self.private_key = self._load_private_key()
    
    def _load_symmetric_key(self) -> Optional[bytes]:
        """Load symmetric key from file or URL."""
        key_source = self.config.symmetric.get('key_file') or self.config.symmetric.get('key_url')
        if not key_source:
            logger.error("Symmetric key file or URL not configured")
            return None
        
        password = self.config.symmetric.get('key_password')
        
        try:
            # Check for GitHub shortcut
            if self._is_github_shortcut(key_source):
                resolved_url = self._resolve_github_key_url(key_source)
                if not resolved_url:
                    logger.error(f"Could not resolve GitHub shortcut: {key_source}")
                    return None
                key_data = self._fetch_key_from_url(resolved_url)
            elif self._is_url(key_source):
                key_data = self._fetch_key_from_url(key_source)
            else:
                key_path = Path(key_source).expanduser()
                if not key_path.exists():
                    logger.error(f"Symmetric key file not found: {key_path}")
                    return None
                key_data = key_path.read_bytes()
            
            # If password provided, derive key using PBKDF2
            if password:
                salt = key_data[:16]  # First 16 bytes as salt
                kdf = PBKDF2HMAC(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=salt,
                    iterations=100000,
                    backend=default_backend()
                )
                return kdf.derive(password.encode())
            else:
                # Use key directly (should be 32 bytes for AES-256)
                if len(key_data) < 32:
                    logger.warning(f"Key too short ({len(key_data)} bytes), padding to 32 bytes")
                    key_data = key_data.ljust(32, b'\0')
                elif len(key_data) > 32:
                    logger.warning(f"Key too long ({len(key_data)} bytes), truncating to 32 bytes")
                    key_data = key_data[:32]
                return key_data
        except Exception as e:
            logger.error(f"Failed to load symmetric key: {e}")
            return None
    
    def _load_public_key(self) -> Optional[Any]:
        """Load public key from file or URL."""
        key_source = self.config.asymmetric.get('public_key')
        if not key_source:
            logger.error("Public key file or URL not configured")
            return None
        
        try:
            # Check for GitHub shortcut
            if self._is_github_shortcut(key_source):
                resolved_url = self._resolve_github_key_url(key_source)
                if not resolved_url:
                    logger.error(f"Could not resolve GitHub shortcut: {key_source}")
                    return None
                key_data = self._fetch_key_from_url(resolved_url)
            elif self._is_url(key_source):
                key_data = self._fetch_key_from_url(key_source)
            else:
                key_path = Path(key_source).expanduser()
                if not key_path.exists():
                    logger.error(f"Public key file not found: {key_path}")
                    return None
                key_data = key_path.read_bytes()
            
            # Validate key format
            if not self._validate_key_format(key_data, 'public'):
                logger.error("Invalid public key format")
                return None
            
            # Load public key
            public_key = serialization.load_pem_public_key(key_data, backend=default_backend())
            logger.info("Public key loaded successfully")
            return public_key
        except Exception as e:
            logger.error(f"Failed to load public key: {e}")
            return None
    
    def _load_private_key(self) -> Optional[Any]:
        """Load private key from local file (never from URL for security)."""
        key_path_str = self.config.asymmetric.get('private_key')
        if not key_path_str:
            logger.error("Private key file not configured")
            return None
        
        key_path = Path(key_path_str).expanduser()
        if not key_path.exists():
            logger.error(f"Private key file not found: {key_path}")
            return None
        
        # Security check: private keys must be local files
        if self._is_url(key_path_str):
            logger.error("Private keys cannot be loaded from URLs (security risk)")
            return None
        
        password = self.config.asymmetric.get('private_key_password')
        password_bytes = password.encode() if password else None
        
        try:
            key_data = key_path.read_bytes()
            
            # Validate key format
            if not self._validate_key_format(key_data, 'private'):
                logger.error("Invalid private key format")
                return None
            
            # Load private key
            private_key = serialization.load_pem_private_key(
                key_data,
                password=password_bytes,
                backend=default_backend()
            )
            logger.info("Private key loaded successfully")
            return private_key
        except Exception as e:
            logger.error(f"Failed to load private key: {e}")
            return None
    
    def _is_url(self, source: str) -> bool:
        """Check if source is a URL."""
        return source.startswith('http://') or source.startswith('https://')
    
    def _is_github_shortcut(self, source: str) -> bool:
        """Check if source is a GitHub username shortcut."""
        return source.startswith('github:') or source.startswith('gh:')
    
    def _resolve_github_key_url(self, shortcut: str) -> Optional[str]:
        """
        Resolve GitHub username shortcut to actual key URL.
        
        Supports formats:
        - github:USERNAME
        - gh:USERNAME
        - github:USERNAME/gist:GIST_ID
        - github:USERNAME/repo:REPO_NAME
        
        Tries multiple strategies:
        1. If gist/repo specified, use that
        2. Try standard repo: backup-keys or bbackup-keys
        3. Try SSH keys endpoint (as fallback)
        
        Args:
            shortcut: GitHub shortcut (e.g., "github:USERNAME")
        
        Returns:
            Resolved URL or None if not found
        """
        # Parse shortcut
        if shortcut.startswith('github:'):
            username = shortcut[7:]  # Remove "github:"
        elif shortcut.startswith('gh:'):
            username = shortcut[3:]  # Remove "gh:"
        else:
            return None
        
        # Check for explicit gist/repo specification
        if '/gist:' in username:
            username, gist_id = username.split('/gist:', 1)
            return f"https://gist.githubusercontent.com/{username}/{gist_id}/raw/backup_public.pem"
        
        if '/repo:' in username:
            username, repo_name = username.split('/repo:', 1)
            return f"https://raw.githubusercontent.com/{username}/{repo_name}/main/backup_public.pem"
        
        # Strategy 1: Try standard repository names.
        standard_repo_urls = [
            f"https://raw.githubusercontent.com/{username}/bbackup-keys/main/backup_public.pem",
            f"https://raw.githubusercontent.com/{username}/backup-keys/main/backup_public.pem",
            f"https://raw.githubusercontent.com/{username}/bbackup-keys/master/backup_public.pem",
            f"https://raw.githubusercontent.com/{username}/backup-keys/master/backup_public.pem",
        ]
        
        # Try all strategies in order
        all_urls = standard_repo_urls
        
        logger.info(f"Resolving GitHub shortcut: {shortcut}")
        logger.info(f"Trying {len(all_urls)} potential URLs...")
        
        for url in all_urls:
            try:
                response = requests.head(url, timeout=5, verify=True, allow_redirects=True)
                if response.status_code == 200:
                    logger.info(f"Found key at: {url}")
                    return url
            except requests.RequestException:
                continue
        
        # Strategy 2: Fallback to SSH keys (inform user they need to convert)
        logger.warning(f"Could not find standard key locations for {username}")
        logger.info("Trying SSH keys endpoint as fallback...")
        ssh_keys_url = f"https://github.com/{username}.keys"
        
        try:
            response = requests.head(ssh_keys_url, timeout=5, verify=True)
            if response.status_code == 200:
                logger.warning(f"Found SSH keys at {ssh_keys_url}, but these may need conversion to PEM format")
                logger.info("Consider creating a gist or repo with your RSA public key in PEM format")
                # Return SSH keys URL as fallback (user will need to handle conversion)
                return ssh_keys_url
        except requests.RequestException:
            pass
        
        logger.error(f"Could not resolve GitHub shortcut: {shortcut}")
        logger.info("Suggestions:")
        logger.info("  1. Create a repo named 'bbackup-keys' with backup_public.pem")
        logger.info(f"  2. Use explicit format: github:{username}/gist:GIST_ID")
        logger.info(f"  3. Use explicit format: github:{username}/repo:REPO_NAME")
        
        return None
    
    def _fetch_key_from_url(self, url: str, verify_https: bool = True) -> bytes:
        """
        Fetch key from URL with validation.
        
        Args:
            url: URL to fetch key from
            verify_https: Require HTTPS (security)
        
        Returns:
            Key data as bytes
        """
        # Security: Require HTTPS
        if verify_https and not url.startswith('https://'):
            raise ValueError(f"Only HTTPS URLs allowed for key fetching: {url}")
        
        try:
            logger.info(f"Fetching key from URL: {url}")
            response = requests.get(url, timeout=10, verify=True)
            response.raise_for_status()
            
            key_data = response.content
            
            # Validate key format (basic check)
            if len(key_data) == 0:
                raise ValueError("Empty key data from URL")
            
            # Cache key locally for offline use
            self._cache_key(url, key_data)
            
            logger.info(f"Successfully fetched key from URL ({len(key_data)} bytes)")
            return key_data
        except requests.RequestException as e:
            logger.error(f"Failed to fetch key from URL {url}: {e}")
            # Try to load from cache
            cached_key = self._load_cached_key(url)
            if cached_key:
                logger.info("Using cached key")
                return cached_key
            raise
    
    def _cache_key(self, url: str, key_data: bytes) -> None:
        """Cache downloaded key locally."""
        cache_dir = Path.home() / '.cache' / 'bbackup' / 'keys'
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Create cache filename from URL hash
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_file = cache_dir / f"key_{url_hash}.cache"
        
        try:
            cache_file.write_bytes(key_data)
            cache_file.chmod(0o600)  # Secure permissions
            logger.debug(f"Cached key to {cache_file}")
        except Exception as e:
            logger.warning(f"Failed to cache key: {e}")
    
    def _load_cached_key(self, url: str) -> Optional[bytes]:
        """Load cached key if available."""
        cache_dir = Path.home() / '.cache' / 'bbackup' / 'keys'
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_file = cache_dir / f"key_{url_hash}.cache"
        
        if cache_file.exists():
            try:
                return cache_file.read_bytes()
            except Exception as e:
                logger.warning(f"Failed to load cached key: {e}")
        
        return None
    
    def _validate_key_format(self, key_data: bytes, key_type: str) -> bool:
        """
        Validate key format.
        
        Args:
            key_data: Key data bytes
            key_type: 'public' or 'private'
        
        Returns:
            True if valid format
        """
        try:
            key_str = key_data.decode('utf-8', errors='ignore')
            
            if key_type == 'public':
                # Check for PEM public key markers
                return '-----BEGIN PUBLIC KEY-----' in key_str or \
                       '-----BEGIN RSA PUBLIC KEY-----' in key_str
            elif key_type == 'private':
                # Check for PEM private key markers
                return '-----BEGIN PRIVATE KEY-----' in key_str or \
                       '-----BEGIN RSA PRIVATE KEY-----' in key_str or \
                       '-----BEGIN ENCRYPTED PRIVATE KEY-----' in key_str
            else:
                return False
        except Exception:
            return False
    
    def encrypt_file(self, source: Path, dest: Path, key: Optional[bytes] = None) -> bool:
        """
        Encrypt a file using AES-256-GCM.
        For asymmetric encryption, generates a session key and encrypts it with the public key.
        
        Args:
            source: Source file path
            dest: Destination encrypted file path
            key: Encryption key (uses symmetric_key if None, or generates session key for asymmetric)
        
        Returns:
            True if successful
        """
        # Determine encryption key based on method
        if key is None:
            if self.config.method in ['symmetric', 'both'] and self.symmetric_key:
                key = self.symmetric_key
            elif self.config.method in ['asymmetric', 'both'] and self.public_key:
                # Generate session key for asymmetric encryption
                key = secrets.token_bytes(32)  # 32 bytes for AES-256
            else:
                logger.error("No encryption key available")
                return False
        
        if key is None:
            logger.error("No encryption key available")
            return False
        
        try:
            # Read source file
            plaintext = _read_regular_file(source)
            
            # Generate random IV (12 bytes for GCM)
            iv = secrets.token_bytes(12)
            
            # Encrypt with AES
            aesgcm = AESGCM(key)
            ciphertext = aesgcm.encrypt(iv, plaintext, None)
            
            # For asymmetric encryption, encrypt the session key with public key
            encrypted_key = None
            if self.config.method in ['asymmetric', 'both'] and self.public_key and key != self.symmetric_key:
                try:
                    # Encrypt session key with public key
                    encrypted_key = self.public_key.encrypt(
                        key,
                        padding.OAEP(
                            mgf=padding.MGF1(algorithm=hashes.SHA256()),
                            algorithm=hashes.SHA256(),
                            label=None
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to encrypt session key: {e}")
                    return False
            
            # Write encrypted file
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open_private_file(dest, 'wb') as f:
                # Format: [encrypted_key_length (4 bytes)] [encrypted_key] [IV (12 bytes)] [ciphertext]
                if encrypted_key:
                    f.write(len(encrypted_key).to_bytes(4, 'big'))
                    f.write(encrypted_key)
                f.write(iv)
                f.write(ciphertext)
            
            logger.debug(f"Encrypted file: {source} -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to encrypt file {source}: {e}")
            return False
    
    def decrypt_file(self, source: Path, dest: Path, key: Optional[bytes] = None) -> bool:
        """
        Decrypt a file encrypted with AES-256-GCM.
        For asymmetric encryption, decrypts the session key with the private key first.
        
        Args:
            source: Encrypted file path
            dest: Destination decrypted file path
            key: Decryption key (uses symmetric_key if None, or decrypts session key for asymmetric)
        
        Returns:
            True if successful
        """
        try:
            # Read encrypted file
            encrypted_data = _read_regular_file(source)
            
            if len(encrypted_data) < 12:
                logger.error("Encrypted file too short (missing IV)")
                return False
            
            # Check if file has encrypted session key (asymmetric encryption)
            offset = 0
            if self.config.method in ['asymmetric', 'both'] and self.private_key:
                # Try to read encrypted key length (first 4 bytes)
                if len(encrypted_data) >= 4:
                    key_length = int.from_bytes(encrypted_data[:4], 'big')
                    # Reasonable key length check (RSA-4096 encrypted key is 512 bytes)
                    if 0 < key_length < 1024 and len(encrypted_data) >= 4 + key_length + 12:
                        try:
                            # Extract and decrypt session key
                            encrypted_key = encrypted_data[4:4+key_length]
                            key = self.private_key.decrypt(
                                encrypted_key,
                                padding.OAEP(
                                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                    algorithm=hashes.SHA256(),
                                    label=None
                                )
                            )
                            offset = 4 + key_length
                            logger.debug("Decrypted session key for asymmetric encryption")
                        except Exception as e:
                            # Not an asymmetric-encrypted file, try symmetric
                            logger.debug(f"Not asymmetric-encrypted file (trying symmetric): {e}")
                            offset = 0
                            pass
            
            # If no key yet, use symmetric key
            if key is None:
                if self.config.method in ['symmetric', 'both'] and self.symmetric_key:
                    key = self.symmetric_key
                else:
                    logger.error("No decryption key available")
                    return False
            
            if key is None:
                logger.error("No decryption key available")
                return False
            
            # Extract IV and ciphertext
            if len(encrypted_data) < offset + 12:
                logger.error("Encrypted file too short (missing IV)")
                return False
            
            iv = encrypted_data[offset:offset+12]
            ciphertext = encrypted_data[offset+12:]
            
            # Decrypt
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(iv, ciphertext, None)
            
            # Write decrypted file
            with open_private_file(dest, 'wb') as output:
                output.write(plaintext)
            
            logger.debug(f"Decrypted file: {source} -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to decrypt file {source}: {e}")
            return False
    
    def encrypt_directory(self, source: Path, dest: Path) -> bool:
        """Encrypt a filesystem tree as individually authenticated payload files."""
        source = Path(source)
        dest = Path(dest)
        staging: Optional[Path] = None
        try:
            source_info = os.lstat(source)
            if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
                raise OSError(f"Source root is not a real directory: {source}")
            if hasattr(os, "geteuid") and source_info.st_uid != os.geteuid():
                raise OSError(f"Source root is owned by another user: {source}")
            if dest.exists() or dest.is_symlink():
                raise OSError(f"Encrypted destination already exists: {dest}")
            source_resolved = source.resolve()
            dest_resolved = dest.resolve(strict=False)
            if dest_resolved == source_resolved or dest_resolved.is_relative_to(source_resolved):
                raise OSError("Encrypted destination must be outside the source tree")
            parent = dest.parent
            parent_info = os.lstat(parent)
            if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
                raise OSError(f"Encrypted destination parent is not a real directory: {parent}")
            if hasattr(os, "geteuid") and parent_info.st_uid != os.geteuid():
                raise OSError(f"Encrypted destination parent is owned by another user: {parent}")
            ensure_private_directory(parent)

            entries = self._collect_tree_entries(source)
            tree_key, wrapped_key = self._new_tree_key()
            salt = secrets.token_bytes(16)
            mac_key = self._tree_mac_key(tree_key, salt, TREE_VERSION)
            staging = Path(tempfile.mkdtemp(prefix=".bbackup-encrypt-", dir=parent))
            ensure_private_directory(staging)
            _check_owned_directory(staging, private=True, empty=True)
            payload_root = staging / "payloads"
            _create_private_directory(payload_root)

            identity = TREE_PAYLOAD_MAGIC + stdlib_hmac.new(
                mac_key, b"bbackup-tree-payload-identity-v3", hashlib.sha256
            ).digest()
            fd = os.open(
                payload_root / TREE_IDENTITY_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as identity_file:
                identity_file.write(identity)

            for entry in entries:
                if entry["type"] != "file":
                    continue
                source_file = source / entry["path"]
                entry["payload_id"] = secrets.token_hex(24)
                payload_path = payload_root / f"{entry['payload_id']}.bbackup"
                nonce = secrets.token_bytes(12)
                plaintext = _read_regular_file(source_file)
                if len(plaintext) != entry["size"]:
                    raise OSError(f"Source file changed during encryption: {source_file}")
                ciphertext = AESGCM(tree_key).encrypt(nonce, plaintext, _canonical_json(entry))
                fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as payload_file:
                    payload_file.write(TREE_PAYLOAD_MAGIC + nonce + ciphertext)

            metadata: Dict[str, Any] = {
                "encrypted": True,
                "format": TREE_FORMAT,
                "version": TREE_VERSION,
                "method": self.config.method,
                "algorithm": "aes-256-gcm",
                "key_id": hashlib.sha256(tree_key).hexdigest()[:16],
                "salt": base64.b64encode(salt).decode("ascii"),
                "wrapped_key": base64.b64encode(wrapped_key).decode("ascii") if wrapped_key else None,
                "timestamp": datetime.now().isoformat(),
                "inventory": TREE_INVENTORY_NAME,
            }
            metadata["auth"] = self._tree_metadata_auth(metadata, mac_key)
            inventory_nonce = secrets.token_bytes(12)
            inventory_ciphertext = AESGCM(tree_key).encrypt(
                inventory_nonce, _canonical_json(entries), _canonical_json(metadata)
            )
            inventory_path = payload_root / TREE_INVENTORY_NAME
            fd = os.open(inventory_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as inventory_file:
                inventory_file.write(TREE_INVENTORY_MAGIC + inventory_nonce + inventory_ciphertext)
            metadata_path = staging / TREE_METADATA_NAME
            fd = os.open(metadata_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as metadata_file:
                json.dump(metadata, metadata_file, indent=2, sort_keys=True)
            os.replace(staging, dest)
            staging = None
            logger.info(f"Encrypted directory: {source} -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to encrypt directory {source}: {e}")
            return False
        finally:
            if staging is not None and staging.exists():
                import shutil
                shutil.rmtree(staging, ignore_errors=True)

    @classmethod
    def validate_directory_tree(cls, source: Path) -> List[Dict[str, Any]]:
        """Preflight a source tree without writing output or loading encryption keys."""
        return cls._collect_tree_entries(Path(source))

    @classmethod
    def _collect_tree_entries(cls, source: Path) -> List[Dict[str, Any]]:
        """Inventory supported ordinary files, directories, symlinks and hardlinks."""
        source_info = os.lstat(source)
        if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
            raise OSError(f"Source root is not a real directory: {source}")
        if hasattr(os, "geteuid") and source_info.st_uid != os.geteuid():
            raise OSError(f"Source root is owned by another user: {source}")
        entries: List[Dict[str, Any]] = []
        inode_paths: Dict[Tuple[int, int], str] = {}
        for root, dirs, files in os.walk(
            source,
            topdown=True,
            followlinks=False,
            onerror=_raise_walk_error,
        ):
            root_path = Path(root)
            children = sorted(dirs + files)
            dirs[:] = [name for name in sorted(dirs) if not (root_path / name).is_symlink()]
            paths = [root_path] if root_path == source else []
            paths.extend(root_path / name for name in children)
            for path in paths:
                info = os.lstat(path)
                if os.geteuid() != 0 and (info.st_uid != os.geteuid()
                        or info.st_gid not in {os.getegid(), *os.getgroups()}):
                    raise OSError(f"Cannot restore tree ownership as the current user: {path}")
                try:
                    xattrs = os.listxattr(path, follow_symlinks=False)
                except (AttributeError, NotImplementedError):
                    xattrs = []
                if xattrs:
                    raise OSError(f"Unsupported extended attributes or ACLs on: {path}")

                rel = "." if path == source else path.relative_to(source).as_posix()
                base = {
                    "path": rel,
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "atime_ns": info.st_atime_ns,
                    "mtime_ns": info.st_mtime_ns,
                }
                if stat.S_ISDIR(info.st_mode):
                    base["type"] = "directory"
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink > 1:
                        inode_key = (info.st_dev, info.st_ino)
                        target = inode_paths.get(inode_key)
                        if target is not None:
                            base.update({"type": "hardlink", "target": target})
                        else:
                            inode_paths[inode_key] = rel
                            base.update({"type": "file", "size": info.st_size})
                    else:
                        base.update({"type": "file", "size": info.st_size})
                elif stat.S_ISLNK(info.st_mode):
                    base.update({"type": "symlink", "target": os.readlink(path)})
                else:
                    raise OSError(f"Unsupported special file type: {path}")
                entries.append(base)
        entries.sort(key=lambda item: item["path"])
        cls._validate_tree_entries(entries)
        return entries

    def _new_tree_key(self) -> Tuple[bytes, Optional[bytes]]:
        if self.config.method in ["symmetric", "both"] and self.symmetric_key:
            return self.symmetric_key, None
        if self.config.method in ["asymmetric", "both"] and self.public_key:
            key = secrets.token_bytes(32)
            wrapped = self.public_key.encrypt(
                key,
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            )
            return key, wrapped
        raise ValueError("No encryption key available for authenticated tree")

    @staticmethod
    def _tree_mac_key(tree_key: bytes, salt: bytes, version: int = 2) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=f"bbackup-tree-manifest-v{version}".encode()).derive(tree_key)

    @staticmethod
    def _tree_metadata_auth(metadata: Dict[str, Any], mac_key: bytes) -> str:
        unsigned = {key: value for key, value in metadata.items() if key != "auth"}
        signer = crypto_hmac.HMAC(mac_key, hashes.SHA256())
        signer.update(_canonical_json(unsigned))
        return base64.b64encode(signer.finalize()).decode("ascii")

    @staticmethod
    def _validate_tree_entries(entries: Any) -> List[Dict[str, Any]]:
        if not isinstance(entries, list) or not entries:
            raise ValueError("Encrypted tree inventory is missing")
        by_path: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Malformed encrypted tree inventory entry")
            path = entry.get("path")
            safe_path = _safe_tree_path(path)
            kind = entry.get("type")
            if safe_path is None or kind not in {"directory", "file", "symlink", "hardlink"}:
                raise ValueError(f"Malformed encrypted tree path or type: {path!r}")
            if path in by_path:
                raise ValueError(f"Duplicate encrypted tree path: {path}")
            for field in ("mode", "uid", "gid", "atime_ns", "mtime_ns"):
                if not isinstance(entry.get(field), int) or entry[field] < 0:
                    raise ValueError(f"Malformed encrypted tree metadata for {path}")
            if entry["mode"] > 0o7777:
                raise ValueError(f"Malformed encrypted tree mode for {path}")
            if kind == "file" and (not isinstance(entry.get("size"), int) or entry["size"] < 0):
                raise ValueError(f"Malformed encrypted file size for {path}")
            if kind in {"symlink", "hardlink"} and not isinstance(entry.get("target"), str):
                raise ValueError(f"Malformed encrypted link target for {path}")
            by_path[path] = entry
        if by_path.get(".", {}).get("type") != "directory":
            raise ValueError("Encrypted tree inventory has no root directory")
        for path, entry in by_path.items():
            safe_path = _safe_tree_path(path)
            assert safe_path is not None
            if path != ".":
                parent = safe_path.parent.as_posix()
                if by_path.get(parent, {}).get("type") != "directory":
                    raise ValueError(f"Encrypted tree parent is missing or not a directory: {path}")
            if entry["type"] == "hardlink":
                target = entry["target"]
                target_path = _safe_tree_path(target)
                if target_path is None or target not in by_path or by_path[target]["type"] not in {"file", "hardlink"}:
                    raise ValueError(f"Invalid hardlink target for {path}")
        return entries

    @staticmethod
    def contains_versioned_tree_payload(source: Path) -> bool:
        """Return whether any tree payload marker exists, even if inventory was removed."""
        source = Path(source)
        if not source.is_dir() or source.is_symlink():
            return False
        for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
            for filename in files:
                path = root_path / filename
                try:
                    if stat.S_ISREG(os.lstat(path).st_mode):
                        if filename.endswith(".bbackup") and path.relative_to(source).parts[0] == "payloads":
                            return True
                        if _read_regular_file_prefix(path, len(TREE_PAYLOAD_MAGIC)) in (TREE_PAYLOAD_MAGIC, TREE_V2_PAYLOAD_MAGIC):
                            return True
                except OSError:
                    continue
        return False
    
    def decrypt_directory(self, source: Path, dest: Path) -> bool:
        """Decrypt a versioned authenticated tree or a legacy per-file directory."""
        source = Path(source)
        dest = Path(dest)
        try:
            source_info = os.lstat(source)
            if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
                raise OSError(f"Encrypted source is not a real directory: {source}")
            if hasattr(os, "geteuid") and source_info.st_uid != os.geteuid():
                raise OSError(f"Encrypted source is owned by another user: {source}")
            metadata_path = source / TREE_METADATA_NAME
            if not metadata_path.is_file() or metadata_path.is_symlink():
                if self.contains_versioned_tree_payload(source) or source.name.endswith(".enc"):
                    raise ValueError("Encrypted tree inventory is missing")
                raise ValueError("Encrypted directory metadata is missing")
            else:
                metadata = json.loads(_read_regular_file(metadata_path).decode("utf-8"))
                if metadata.get("format") == TREE_FORMAT:
                    self._decrypt_versioned_tree(source, dest, metadata)
                elif "version" in metadata or "format" in metadata:
                    raise ValueError("Unsupported encrypted tree format version")
                else:
                    logger.warning("Decrypting legacy encrypted backup; file modes, ownership, links and timestamps cannot be preserved")
                    self._decrypt_legacy_directory(source, dest)
            logger.info(f"Decrypted directory: {source} -> {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to decrypt directory {source}: {e}")
            return False

    def _destination_root(self, dest: Path) -> None:
        if dest.exists() or dest.is_symlink():
            raise OSError(f"Decryption destination already exists: {dest}")
        _create_private_directory(dest)

    def _decrypt_versioned_tree(self, source: Path, dest: Path, metadata: Dict[str, Any]) -> None:
        _check_owned_directory(source, private=True)
        metadata_path = source / TREE_METADATA_NAME
        metadata_info = os.lstat(metadata_path)
        if (
            not stat.S_ISREG(metadata_info.st_mode)
            or stat.S_ISLNK(metadata_info.st_mode)
            or stat.S_IMODE(metadata_info.st_mode) & 0o077
            or metadata_info.st_nlink != 1
            or (hasattr(os, "geteuid") and metadata_info.st_uid != os.geteuid())
        ):
            raise ValueError("Encrypted tree inventory file is unsafe")
        version = metadata.get("version")
        if version not in (2, TREE_VERSION) or metadata.get("algorithm") != "aes-256-gcm":
            raise ValueError("Unsupported encrypted tree format version")
        salt = base64.b64decode(metadata["salt"], validate=True)
        wrapped_value = metadata.get("wrapped_key")
        if wrapped_value:
            if self.private_key is None:
                raise ValueError("No private key available for encrypted tree")
            wrapped_key = base64.b64decode(wrapped_value, validate=True)
            tree_key = self.private_key.decrypt(
                wrapped_key,
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            )
        else:
            tree_key = self.symmetric_key
            if tree_key is None:
                raise ValueError("No symmetric key available for encrypted tree")
        if len(salt) != 16 or len(tree_key) != 32:
            raise ValueError("Malformed encrypted tree key metadata")
        mac_key = self._tree_mac_key(tree_key, salt, version)
        expected_auth = self._tree_metadata_auth(metadata, mac_key)
        if not stdlib_hmac.compare_digest(expected_auth, metadata.get("auth", "")):
            raise ValueError("Encrypted tree inventory authentication failed")
        payload_root = source / "payloads"
        if payload_root.is_symlink() or not payload_root.is_dir():
            raise ValueError("Encrypted tree payload directory is missing or unsafe")
        _check_owned_directory(payload_root, private=True)
        if version == 2:
            logger.warning("Decrypting v2 encrypted tree; its inventory exposes paths and filesystem metadata")
            entries = self._validate_tree_entries(metadata.get("entries"))
            payload_magic = TREE_V2_PAYLOAD_MAGIC
            identity_name = TREE_V2_IDENTITY_NAME
            expected_payloads = {f"{entry['path']}.bbackup" for entry in entries if entry["type"] == "file"}
        else:
            if metadata.get("inventory") != TREE_INVENTORY_NAME or "entries" in metadata:
                raise ValueError("Malformed encrypted tree inventory header")
            inventory_blob = _read_regular_file(payload_root / TREE_INVENTORY_NAME)
            if not inventory_blob.startswith(TREE_INVENTORY_MAGIC) or len(inventory_blob) < len(TREE_INVENTORY_MAGIC) + 28:
                raise ValueError("Malformed encrypted tree inventory")
            nonce_start = len(TREE_INVENTORY_MAGIC)
            inventory_data = AESGCM(tree_key).decrypt(
                inventory_blob[nonce_start:nonce_start + 12],
                inventory_blob[nonce_start + 12:],
                _canonical_json(metadata),
            )
            entries = self._validate_tree_entries(json.loads(inventory_data))
            payload_ids = [entry.get("payload_id") for entry in entries if entry["type"] == "file"]
            if any(not isinstance(value, str) or len(value) != 48 or any(c not in "0123456789abcdef" for c in value) for value in payload_ids):
                raise ValueError("Malformed encrypted payload identifier")
            if len(set(payload_ids)) != len(payload_ids):
                raise ValueError("Duplicate encrypted payload identifier")
            expected_payloads = {f"{value}.bbackup" for value in payload_ids}
            expected_payloads.add(TREE_INVENTORY_NAME)
            payload_magic = TREE_PAYLOAD_MAGIC
            identity_name = TREE_IDENTITY_NAME
        expected_payloads.add(identity_name)
        actual_payloads = set()
        actual_payload_dirs = set()
        for root, dirs, files in os.walk(payload_root, topdown=True, followlinks=False):
            root_path = Path(root)
            if any((root_path / name).is_symlink() for name in dirs):
                raise ValueError("Symlink found in encrypted payload tree")
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
            for name in dirs:
                directory_info = os.lstat(root_path / name)
                if (
                    not stat.S_ISDIR(directory_info.st_mode)
                    or stat.S_IMODE(directory_info.st_mode) & 0o077
                    or (hasattr(os, "geteuid") and directory_info.st_uid != os.geteuid())
                ):
                    raise ValueError("Unsafe directory found in encrypted payload tree")
            actual_payload_dirs.update(
                (root_path / name).relative_to(payload_root).as_posix() for name in dirs
            )
            for name in files:
                path = root_path / name
                info = os.lstat(path)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_nlink != 1
                    or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                ):
                    raise ValueError("Non-regular file found in encrypted payload tree")
                actual_payloads.add(path.relative_to(payload_root).as_posix())
        if actual_payloads != expected_payloads:
            raise ValueError("Encrypted tree payload membership does not match inventory")
        expected_payload_dirs = {
            parent.as_posix()
            for payload in expected_payloads
            for parent in Path(payload).parents
            if parent.as_posix() != "."
        }
        if actual_payload_dirs != expected_payload_dirs:
            raise ValueError("Encrypted tree payload directory membership does not match inventory")

        identity_path = payload_root / identity_name
        identity_payload = _read_regular_file(identity_path)
        expected_identity = payload_magic + stdlib_hmac.new(
            mac_key, f"bbackup-tree-payload-identity-v{version}".encode(), hashlib.sha256
        ).digest()
        if not stdlib_hmac.compare_digest(identity_payload, expected_identity):
            raise ValueError("Encrypted tree payload identity authentication failed")

        self._destination_root(dest)
        try:
            for entry in sorted(entries, key=lambda value: (value["path"].count("/"), value["path"])):
                if entry["type"] == "directory":
                    target = dest if entry["path"] == "." else dest / entry["path"]
                    if target != dest:
                        target.mkdir(mode=0o700)
            for entry in entries:
                if entry["type"] != "file":
                    continue
                payload_name = f"{entry['path']}.bbackup" if version == 2 else f"{entry['payload_id']}.bbackup"
                payload_path = payload_root / payload_name
                payload = _read_regular_file(payload_path)
                if len(payload) < len(payload_magic) + 12 + 16 or not payload.startswith(payload_magic):
                    raise ValueError(f"Malformed encrypted payload for {entry['path']}")
                nonce_start = len(payload_magic)
                nonce = payload[nonce_start:nonce_start + 12]
                plaintext = AESGCM(tree_key).decrypt(
                    nonce,
                    payload[nonce_start + 12:],
                    _canonical_json(entry),
                )
                if len(plaintext) != entry["size"]:
                    raise ValueError(f"Encrypted payload size mismatch for {entry['path']}")
                output = dest / entry["path"]
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as plaintext_file:
                    plaintext_file.write(plaintext)

            pending = [entry for entry in entries if entry["type"] == "hardlink"]
            while pending:
                remaining = []
                made_progress = False
                for entry in pending:
                    target = dest / entry["target"]
                    output = dest / entry["path"]
                    if target.exists():
                        os.link(target, output, follow_symlinks=False)
                        made_progress = True
                    else:
                        remaining.append(entry)
                if remaining and not made_progress:
                    raise ValueError("Encrypted tree hardlink inventory is cyclic or incomplete")
                pending = remaining

            for entry in entries:
                if entry["type"] == "symlink":
                    output = dest / entry["path"]
                    os.symlink(entry["target"], output)

            for entry in sorted(entries, key=lambda value: (value["path"].count("/"), value["path"]), reverse=True):
                output = dest if entry["path"] == "." else dest / entry["path"]
                self._apply_tree_metadata(output, entry)
        except Exception:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
            raise

    @staticmethod
    def _apply_tree_metadata(path: Path, entry: Dict[str, Any]) -> None:
        kind = entry["type"]
        follow = kind != "symlink"
        if hasattr(os, "chown"):
            info = os.stat(path, follow_symlinks=follow)
            if info.st_uid != entry["uid"] or info.st_gid != entry["gid"]:
                os.chown(path, entry["uid"], entry["gid"], follow_symlinks=follow)
        if kind != "symlink":
            os.chmod(path, entry["mode"])
        os.utime(path, ns=(entry["atime_ns"], entry["mtime_ns"]), follow_symlinks=follow)

    def _decrypt_legacy_directory(self, source: Path, dest: Path) -> None:
        files = []
        for root, dirs, names in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            if any((root_path / name).is_symlink() for name in dirs):
                raise ValueError("Symlink found in legacy encrypted backup")
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
            for name in names:
                path = root_path / name
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ValueError("Unsupported entry in legacy encrypted backup")
                if path == source / TREE_METADATA_NAME:
                    continue
                if not name.endswith(".enc"):
                    raise ValueError(f"Partial legacy encrypted backup: unexpected file {path}")
                rel_path = path.relative_to(source).as_posix()[:-4]
                if _safe_tree_path(rel_path) is None:
                    raise ValueError(f"Unsafe legacy encrypted backup path: {rel_path}")
                files.append((path, rel_path))
        if not files:
            raise ValueError("Encrypted directory contains no payload files")
        self._destination_root(dest)
        try:
            for source_file, rel_path in files:
                dest_file = dest / rel_path
                dest_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not self.decrypt_file(source_file, dest_file):
                    raise ValueError(f"Legacy encrypted payload failed authentication: {rel_path}")
        except Exception:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
            raise
    
    def encrypt_backup(self, backup_dir: Path) -> Path:
        """
        Encrypt entire backup directory.
        
        Args:
            backup_dir: Backup directory to encrypt
        
        Returns:
            Path to encrypted backup directory
        """
        encrypted_dir = backup_dir.parent / f"{backup_dir.name}.enc"
        
        if self.encrypt_directory(backup_dir, encrypted_dir):
            return encrypted_dir
        else:
            logger.error("Failed to encrypt backup")
            return backup_dir  # Return original on failure
    
    def decrypt_backup(self, backup_dir: Path) -> Path:
        """
        Decrypt entire backup directory.
        
        Args:
            backup_dir: Encrypted backup directory
        
        Returns:
            Path to decrypted backup directory
        """
        backup_dir = Path(backup_dir)
        metadata_path = backup_dir / TREE_METADATA_NAME
        if not metadata_path.exists():
            if self.contains_versioned_tree_payload(backup_dir) or backup_dir.name.endswith(".enc"):
                raise RuntimeError("Authenticated tree inventory is missing")
            logger.info("Backup appears to be unencrypted")
            return backup_dir

        temp_root = Path(tempfile.mkdtemp(prefix="bbackup_decrypted_"))
        ensure_private_directory(temp_root)
        decrypted_dir = temp_root / "tree"
        try:
            if self.decrypt_directory(backup_dir, decrypted_dir):
                return decrypted_dir
            raise RuntimeError("Encrypted backup is malformed or could not be authenticated")
        except Exception:
            import shutil
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
    
    @staticmethod
    def generate_symmetric_key() -> bytes:
        """Generate a random symmetric key (32 bytes for AES-256)."""
        return secrets.token_bytes(32)
    
    @staticmethod
    def generate_keypair(algorithm: str = 'rsa-4096') -> Tuple[bytes, bytes]:
        """
        Generate asymmetric keypair.
        
        Args:
            algorithm: 'rsa-4096'
        
        Returns:
            Tuple of (public_key_bytes, private_key_bytes) in PEM format
        """
        if algorithm == 'rsa-4096':
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=4096,
                backend=default_backend()
            )
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}")
        
        public_key = private_key.public_key()
        
        # Serialize to PEM
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        
        return public_pem, private_pem
