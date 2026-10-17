"""
File-level version checking with SHA-256 checksums (Git-compatible).
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

from .repo import get_repo_url, parse_repo_url


def compute_file_checksum(file_path: Path) -> str:
    """
    Compute SHA-256 checksum of a file.
    
    Args:
        file_path: Path to file
    
    Returns:
        SHA-256 checksum as hex string
    """
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_tracked_files(repo_root: Path) -> List[Path]:
    """
    Get list of tracked files to check.
    
    Args:
        repo_root: Repository root directory
    
    Returns:
        List of file paths relative to repo root
    """
    tracked = []
    tracked_extensions = ['.py', '.yaml', '.yml', '.md', '.txt', '.sh']
    ignored_dirs = {'.git', '__pycache__', '.pytest_cache', 'bbackup.egg-info', '.venv', 'venv'}
    
    for root, dirs, files in os.walk(repo_root):
        # Filter ignored directories
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        
        root_path = Path(root)
        for file in files:
            file_path = root_path / file
            if file_path.suffix in tracked_extensions:
                rel_path = file_path.relative_to(repo_root)
                tracked.append(rel_path)
    
    return sorted(tracked)


def compute_local_checksums(repo_root: Path) -> Dict[str, Dict[str, any]]:
    """
    Compute checksums for all tracked files locally.
    
    Args:
        repo_root: Repository root directory
    
    Returns:
        Dict mapping file paths to checksum info
    """
    checksums = {}
    tracked_files = get_tracked_files(repo_root)
    
    for rel_path in tracked_files:
        file_path = repo_root / rel_path
        if file_path.exists():
            checksum = compute_file_checksum(file_path)
            mtime = file_path.stat().st_mtime
            checksums[str(rel_path)] = {
                "sha256": checksum,
                "mtime": mtime,
            }
    
    return checksums


def save_local_checksums(checksums: Dict, data_dir: Path) -> bool:
    """
    Save local checksums to file.
    
    Args:
        checksums: Checksum dictionary
        data_dir: Data directory path
    
    Returns:
        True if successful
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        checksum_file = data_dir / ".file_checksums.json"
        with open(checksum_file, 'w') as f:
            json.dump(checksums, f, indent=2)
        return True
    except Exception:
        return False


def load_local_checksums(data_dir: Path) -> Dict:
    """
    Load local checksums from file.
    
    Args:
        data_dir: Data directory path
    
    Returns:
        Checksum dictionary or empty dict
    """
    checksum_file = data_dir / ".file_checksums.json"
    if checksum_file.exists():
        try:
            with open(checksum_file, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fetch_github_tree_checksums(repo_url: str, branch: str = "main") -> Dict[str, str]:
    """
    Fetch file checksums from GitHub using Git API.
    
    Args:
        repo_url: Repository URL
        branch: Branch name
    
    Returns:
        Dict mapping file paths to SHA-256 checksums
    """
    parsed = parse_repo_url(repo_url)
    if parsed["type"] != "github":
        return {}
    
    owner = parsed["owner"]
    repo = parsed["repo"]
    
    # Get tree SHA for branch
    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    
    try:
        response = requests.get(tree_url, timeout=10)
        if response.status_code != 200:
            return {}
        
        tree_data = response.json()
        checksums = {}
        
        # GitHub API returns SHA-1, we need to fetch file content and compute SHA-256
        # For now, we'll use the blob SHA as identifier and fetch content
        for item in tree_data.get("tree", []):
            if item.get("type") == "blob":
                path = item.get("path", "")
                blob_sha = item.get("sha", "")
                
                # Fetch blob content and compute SHA-256
                blob_url = f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{blob_sha}"
                blob_response = requests.get(blob_url, timeout=10)
                if blob_response.status_code == 200:
                    blob_data = blob_response.json()
                    content = blob_data.get("content", "")
                    # GitHub API returns base64 encoded content
                    import base64
                    try:
                        file_content = base64.b64decode(content)
                        sha256 = hashlib.sha256(file_content).hexdigest()
                        checksums[path] = sha256
                    except Exception:
                        pass
        
        return checksums
    except Exception:
        return {}


def fetch_manifest_checksums(repo_url: str, branch: str = "main") -> Dict[str, str]:
    """
    Fetch checksums from VERSION_MANIFEST.json file.
    
    Args:
        repo_url: Repository URL
        branch: Branch name
    
    Returns:
        Dict mapping file paths to SHA-256 checksums
    """
    # Construct manifest URL
    if parse_repo_url(repo_url)["type"] == "github":
        manifest_url = f"{repo_url}/raw/{branch}/VERSION_MANIFEST.json"
    elif parse_repo_url(repo_url)["type"] == "gitlab":
        manifest_url = f"{repo_url}/-/raw/{branch}/VERSION_MANIFEST.json"
    else:
        # Custom URL - try common patterns
        manifest_url = f"{repo_url}/VERSION_MANIFEST.json"
    
    try:
        response = requests.get(manifest_url, timeout=10)
        if response.status_code == 200:
            manifest = response.json()
            checksums = {}
            for file_path, file_info in manifest.get("files", {}).items():
                if isinstance(file_info, dict):
                    checksums[file_path] = file_info.get("sha256", "")
                elif isinstance(file_info, str):
                    checksums[file_path] = file_info
            return checksums
    except Exception:
        pass
    
    return {}


def fetch_remote_checksums(repo_url: str, branch: str = "main") -> Dict[str, str]:
    """
    Fetch remote file checksums using best available method.
    
    Args:
        repo_url: Repository URL
        branch: Branch name
    
    Returns:
        Dict mapping file paths to SHA-256 checksums
    """
    # Try manifest first (most efficient)
    checksums = fetch_manifest_checksums(repo_url, branch)
    if checksums:
        return checksums
    
    # Try GitHub API
    parsed = parse_repo_url(repo_url)
    if parsed["type"] == "github":
        checksums = fetch_github_tree_checksums(repo_url, branch)
        if checksums:
            return checksums
    
    # Fallback: return empty (will need direct file download)
    return {}


def compare_checksums(
    local_checksums: Dict[str, Dict],
    remote_checksums: Dict[str, str]
) -> Tuple[List[str], List[str], List[str]]:
    """
    Compare local and remote checksums.
    
    Args:
        local_checksums: Local checksums dict
        remote_checksums: Remote checksums dict
    
    Returns:
        Tuple of (changed_files, new_files, removed_files)
    """
    local_files = set(local_checksums.keys())
    remote_files = set(remote_checksums.keys())
    
    changed = []
    new_files = list(remote_files - local_files)
    removed = list(local_files - remote_files)
    
    for file_path in local_files & remote_files:
        local_sha = local_checksums[file_path].get("sha256", "")
        remote_sha = remote_checksums.get(file_path, "")
        if local_sha != remote_sha:
            changed.append(file_path)
    
    return changed, new_files, removed


def check_for_updates(repo_root: Path, repo_url: Optional[str] = None, branch: str = "main") -> Dict:
    """
    Check for updates by comparing local and remote checksums.
    
    Args:
        repo_root: Repository root directory
        repo_url: Repository URL (uses default if None)
        branch: Branch name
    
    Returns:
        Dict with update information
    """
    if repo_url is None:
        repo_url = get_repo_url()
    
    # Compute local checksums
    local_checksums = compute_local_checksums(repo_root)
    
    # Fetch remote checksums
    remote_checksums = fetch_remote_checksums(repo_url, branch)
    
    if not remote_checksums:
        return {
            "has_updates": False,
            "error": "Could not fetch remote checksums",
            "changed": [],
            "new": [],
            "removed": [],
        }
    
    # Compare
    changed, new, removed = compare_checksums(local_checksums, remote_checksums)
    
    return {
        "has_updates": len(changed) > 0 or len(new) > 0 or len(removed) > 0,
        "changed": changed,
        "new": new,
        "removed": removed,
        "local_count": len(local_checksums),
        "remote_count": len(remote_checksums),
    }
