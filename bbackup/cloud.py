"""Read-only S3-compatible version inventory and local checkpoint restore.

This module never writes to an object store and never creates a production
protection claim. Callers own quiescence coordination, signature trust, and any
manifest sealing workflow.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
MIN_NONCURRENT_DAYS = 30
EXPIRY_MARGIN_DAYS = 1
DEFAULT_PAGE_SIZE = 1000
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_LINE_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024**3
_FULL_ID = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_COPYRIGHT = "Copyright © 2026 Crux Experts LLC"
_COPYRIGHT_URL = "https://www.cruxexperts.com/"
_PRIVATE_NOTICE = (
    "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, "
    "distribution, or publication without prior written permission is prohibited."
)


class CloudError(RuntimeError):
    """A sanitized cloud inventory or restore failure."""


@dataclass(frozen=True, slots=True)
class LifecycleInspection:
    bucket: str
    versioning_enabled: bool
    current_expiration: bool
    minimum_noncurrent_days: int | None
    unknown_noncurrent_expiration: bool
    applicable_rules: int
    protection_qualified: bool = False


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    path: Path
    manifest_sha256: str
    item_count: int
    object_count: int
    delete_marker_count: int
    captured_at: str
    expires_at: str | None
    source_repository: str
    repository_id: str
    source_snapshot_id: str
    endpoint_url: str
    bucket: str
    prefix: str
    quiescent_window_confirmed: bool
    protection_qualified: bool = False


@dataclass(frozen=True, slots=True)
class RestoreRecord:
    manifest_sha256: str
    files_restored: int
    bytes_restored: int
    delete_markers_preserved_as_absent: int
    destination: Path
    expires_at: str | None
    protection_expired: bool


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CloudError("Invalid checkpoint timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CloudError("Invalid checkpoint timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CloudError("Invalid checkpoint timestamp")
    return parsed.astimezone(timezone.utc)


def _source_identity(repository: str, snapshot_id: str) -> None:
    if not isinstance(repository, str) or not _SOURCE_NAME.fullmatch(repository):
        raise CloudError("Source repository must be a logical repository name")
    if not isinstance(snapshot_id, str) or not _FULL_ID.fullmatch(snapshot_id):
        raise CloudError("A complete source snapshot ID is required")


def _repository_identity(repository_id: str) -> None:
    if not isinstance(repository_id, str) or not _FULL_ID.fullmatch(repository_id):
        raise CloudError("A complete source repository ID is required")


def _normalize_endpoint_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in "\0\r\n")
    ):
        raise ValueError("an explicit safe S3 endpoint URL is required")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid S3 endpoint URL") from None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"https", "http"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or "@" in parsed.netloc
    ):
        raise ValueError(
            "S3 endpoint URL cannot contain credentials, query, or fragment"
        )
    hostname = hostname.lower()
    if scheme == "http":
        loopback = hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError(
                "plain HTTP is allowed only for an explicit loopback endpoint"
            )
    if port == (443 if scheme == "https" else 80):
        port = None
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = authority_host + (f":{port}" if port is not None else "")
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _strict_json(data: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CloudError("Invalid checkpoint JSON") from None
    if not isinstance(value, dict):
        raise CloudError("Invalid checkpoint record")
    return value


def _json_line(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _rule_prefix(rule: Mapping[str, Any]) -> str | None:
    """Return a known rule prefix, or None when the filter needs conservative overlap."""

    if isinstance(rule.get("Prefix"), str):
        return rule["Prefix"]
    filter_value = rule.get("Filter")
    if not isinstance(filter_value, dict):
        return ""
    if isinstance(filter_value.get("Prefix"), str):
        return filter_value["Prefix"]
    and_value = filter_value.get("And")
    if isinstance(and_value, dict) and isinstance(and_value.get("Prefix"), str):
        return and_value["Prefix"]
    # Tag and size filters may apply to any key in this repository prefix.
    return None


def _rule_applies(rule: Mapping[str, Any], prefix: str) -> bool:
    rule_prefix = _rule_prefix(rule)
    if rule_prefix is None:
        return True
    return (
        not rule_prefix
        or prefix.startswith(rule_prefix)
        or rule_prefix.startswith(prefix)
    )


def _relative_key(key: Any, prefix: str) -> str:
    if not isinstance(key, str) or not key or "\0" in key:
        raise CloudError("Invalid object key in version inventory")
    if prefix and not key.startswith(prefix):
        raise CloudError("Object key is outside the configured prefix")
    relative = key[len(prefix) :]
    if not relative or relative.startswith("/") or "\\" in relative:
        raise CloudError("Unsafe object key in version inventory")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CloudError("Unsafe object key in version inventory")
    windows = PureWindowsPath(relative)
    if windows.drive or windows.root or PurePosixPath(relative).is_absolute():
        raise CloudError("Unsafe object key in version inventory")
    if any(ord(char) < 32 or ord(char) == 127 for char in relative):
        raise CloudError("Unsafe object key in version inventory")
    if any(":" in part for part in parts):
        raise CloudError("Unsafe object key in version inventory")
    return "/".join(parts)


def _is_lock_key(relative_key: str) -> bool:
    return relative_key == "locks" or relative_key.startswith("locks/")


def _insert_safe_path(connection: sqlite3.Connection, key: str, relative: str) -> None:
    parts = relative.split("/")
    parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
    if any(
        connection.execute(
            "SELECT 1 FROM seen_paths WHERE path = ?", (parent,)
        ).fetchone()
        for parent in parents
    ):
        raise CloudError("Object inventory contains a file/directory path conflict")
    if connection.execute(
        "SELECT 1 FROM seen_dirs WHERE path = ?", (relative,)
    ).fetchone():
        raise CloudError("Object inventory contains a file/directory path conflict")
    try:
        connection.execute("INSERT INTO seen_keys VALUES (?)", (key,))
        connection.execute("INSERT INTO seen_paths VALUES (?)", (relative,))
        connection.executemany(
            "INSERT OR IGNORE INTO seen_dirs VALUES (?)",
            ((parent,) for parent in parents),
        )
    except sqlite3.IntegrityError:
        raise CloudError("Version listing contains duplicate current keys") from None


def _ensure_directory_path(path: Path) -> None:
    if not path.is_absolute():
        raise CloudError("Destination must be an absolute local directory")
    if ".." in path.parts:
        raise CloudError("Destination path cannot contain parent traversal")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError:
            raise CloudError("Destination directory must already exist") from None
        if current.is_symlink() or not current.is_dir():
            raise CloudError("Destination cannot contain symlinks or non-directories")


class S3CloudAdapter:
    """Read exact current object versions and restore them to a local directory.

    Supply a boto3-compatible ``client`` for tests or an explicitly configured
    endpoint. Credentials are obtained by boto3's normal provider chain and are
    never copied into a checkpoint.
    """

    def __init__(
        self,
        bucket: str,
        *,
        client: Any | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        prefix: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if (
            not isinstance(bucket, str)
            or not bucket
            or any(c in bucket for c in "\0\r\n")
        ):
            raise ValueError("bucket must be a nonempty single-line name")
        if not isinstance(prefix, str) or "\0" in prefix or prefix.startswith("/"):
            raise ValueError("prefix must be a relative object-key prefix")
        if type(page_size) is not int or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 1000")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.page_size = page_size
        configured_endpoint = (
            _normalize_endpoint_url(endpoint_url) if endpoint_url is not None else None
        )
        self.region_name = region_name
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise CloudError("boto3 is required to create an S3 client") from None
            options = {}
            if configured_endpoint is not None:
                options["endpoint_url"] = configured_endpoint
            if region_name is not None:
                options["region_name"] = region_name
            options["config"] = Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"total_max_attempts": 5, "mode": "standard"},
                signature_version="s3v4",
            )
            client = boto3.client("s3", **options)
        if configured_endpoint is None:
            client_endpoint = getattr(
                getattr(client, "meta", None), "endpoint_url", None
            )
            if client_endpoint is None:
                raise ValueError(
                    "an explicit endpoint_url is required for clients without endpoint metadata"
                )
            configured_endpoint = _normalize_endpoint_url(client_endpoint)
        self.endpoint_url = configured_endpoint
        self.client = client

    def inspect_lifecycle(self) -> LifecycleInspection:
        """Read bucket versioning and applicable lifecycle expiration rules."""

        try:
            versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        except Exception:
            raise CloudError("Could not inspect bucket versioning") from None
        enabled = isinstance(versioning, dict) and versioning.get("Status") == "Enabled"
        try:
            lifecycle = self.client.get_bucket_lifecycle_configuration(
                Bucket=self.bucket
            )
            rules = lifecycle.get("Rules", []) if isinstance(lifecycle, dict) else []
        except Exception as exc:
            if _error_code(exc) == "NoSuchLifecycleConfiguration":
                rules = []
            else:
                raise CloudError(
                    "Could not inspect bucket lifecycle configuration"
                ) from None
        if not isinstance(rules, list):
            raise CloudError("Invalid bucket lifecycle response")
        applicable = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("Status") == "Enabled"
            and _rule_applies(rule, self.prefix)
        ]
        current_expiration = False
        minimum_noncurrent: int | None = None
        unknown_noncurrent = False
        for rule in applicable:
            expiration = rule.get("Expiration")
            if isinstance(expiration, dict) and any(
                key in expiration for key in ("Days", "Date")
            ):
                current_expiration = True
            noncurrent = rule.get("NoncurrentVersionExpiration")
            if noncurrent is not None:
                days = (
                    noncurrent.get("NoncurrentDays")
                    if isinstance(noncurrent, dict)
                    else None
                )
                if type(days) is not int or days < 1:
                    unknown_noncurrent = True
                else:
                    minimum_noncurrent = (
                        days
                        if minimum_noncurrent is None
                        else min(minimum_noncurrent, days)
                    )
        return LifecycleInspection(
            bucket=self.bucket,
            versioning_enabled=enabled,
            current_expiration=current_expiration,
            minimum_noncurrent_days=minimum_noncurrent,
            unknown_noncurrent_expiration=unknown_noncurrent,
            applicable_rules=len(applicable),
            protection_qualified=False,
        )

    @staticmethod
    def _require_checkpoint_policy(policy: LifecycleInspection) -> None:
        if not policy.versioning_enabled:
            raise CloudError("Bucket versioning must be enabled")
        if policy.current_expiration:
            raise CloudError(
                "Current-version expiration prevents a protected checkpoint"
            )
        if policy.unknown_noncurrent_expiration:
            raise CloudError("Noncurrent-version expiration policy is not understood")
        if (
            policy.minimum_noncurrent_days is not None
            and policy.minimum_noncurrent_days < MIN_NONCURRENT_DAYS
        ):
            raise CloudError(
                "Noncurrent versions must be retained for at least 30 days"
            )

    def _init_index(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE seen_keys (key TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE seen_paths (path TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE seen_dirs (path TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE page_markers (key_marker TEXT NOT NULL, version_marker TEXT NOT NULL, PRIMARY KEY(key_marker, version_marker))"
        )
        return connection

    def _current_versions(
        self,
        connection: sqlite3.Connection,
        cancel_event: Event | None,
    ) -> Iterator[dict[str, Any]]:
        key_marker: str | None = None
        version_marker: str | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CloudError("Checkpoint inventory cancelled")
            marker_pair = (key_marker or "", version_marker or "")
            try:
                connection.execute(
                    "INSERT INTO page_markers VALUES (?, ?)", marker_pair
                )
            except sqlite3.IntegrityError:
                raise CloudError(
                    "Object-version pagination repeated a marker"
                ) from None
            request = {
                "Bucket": self.bucket,
                "Prefix": self.prefix,
                "MaxKeys": self.page_size,
            }
            if key_marker is not None:
                request["KeyMarker"] = key_marker
            if version_marker is not None:
                request["VersionIdMarker"] = version_marker
            try:
                response = self.client.list_object_versions(**request)
            except Exception:
                raise CloudError("Could not list object versions") from None
            if not isinstance(response, dict):
                raise CloudError("Invalid object-version listing response")
            for collection_name, kind in (
                ("Versions", "object"),
                ("DeleteMarkers", "delete_marker"),
            ):
                records = response.get(collection_name, [])
                if not isinstance(records, list):
                    raise CloudError("Invalid object-version listing response")
                for record in records:
                    if (
                        not isinstance(record, dict)
                        or type(record.get("IsLatest")) is not bool
                    ):
                        raise CloudError("Invalid object-version listing entry")
                    if not record["IsLatest"]:
                        continue
                    key = record.get("Key")
                    version_id = record.get("VersionId")
                    relative = _relative_key(key, self.prefix)
                    if _is_lock_key(relative):
                        continue
                    if not isinstance(version_id, str) or not version_id:
                        raise CloudError("Object version is missing its version ID")
                    _insert_safe_path(connection, key, relative)
                    item: dict[str, Any] = {
                        "type": kind,
                        "key": key,
                        "version_id": version_id,
                    }
                    if kind == "object":
                        size = record.get("Size")
                        if type(size) is not int or size < 0:
                            raise CloudError(
                                "Current object version is missing its size"
                            )
                        item["size"] = size
                    yield item
            if not response.get("IsTruncated", False):
                break
            next_key = response.get("NextKeyMarker")
            next_version = response.get("NextVersionIdMarker")
            if not isinstance(next_key, str) or not next_key:
                raise CloudError(
                    "Truncated object listing is missing its next key marker"
                )
            if next_version is not None and not isinstance(next_version, str):
                raise CloudError("Invalid next version marker")
            next_pair = (next_key, next_version or "")
            if next_pair == marker_pair:
                raise CloudError("Object-version pagination did not progress")
            key_marker, version_marker = next_key, next_version

    def create_checkpoint(
        self,
        path: str | os.PathLike[str],
        *,
        source_repository: str,
        repository_id: str,
        source_snapshot_id: str,
        trusted_quiescent_window_confirmed: bool,
        captured_at: datetime | None = None,
        cancel_event: Event | None = None,
    ) -> CheckpointRecord:
        """Stream current versions and delete markers into an independent JSONL file."""

        _source_identity(source_repository, source_snapshot_id)
        _repository_identity(repository_id)
        if trusted_quiescent_window_confirmed is not True:
            raise CloudError("An explicitly coordinated quiescent window is required")
        policy = self.inspect_lifecycle()
        self._require_checkpoint_policy(policy)
        captured = captured_at or datetime.now(timezone.utc)
        captured_iso = _iso(captured)
        expiry = (
            _iso(
                captured
                + timedelta(days=policy.minimum_noncurrent_days - EXPIRY_MARGIN_DAYS)
            )
            if policy.minimum_noncurrent_days is not None
            else None
        )
        target = Path(path).absolute()
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise CloudError("Checkpoint parent directory must exist and be local")
        if os.path.lexists(target):
            raise CloudError("Checkpoint output already exists")
        file_descriptor, partial_name = tempfile.mkstemp(
            prefix=".cloud-checkpoint-", suffix=".partial", dir=target.parent
        )
        os.fchmod(file_descriptor, 0o600)
        digest = hashlib.sha256()
        counts = {"object": 0, "delete_marker": 0}
        try:
            with tempfile.TemporaryDirectory(
                prefix="cloud-index-", dir=target.parent
            ) as index_dir:
                connection = self._init_index(Path(index_dir) / "index.sqlite3")
                try:
                    header = {
                        "type": "checkpoint",
                        "schema_version": SCHEMA_VERSION,
                        "provider": "s3-compatible-unqualified",
                        "endpoint_url": self.endpoint_url,
                        "bucket": self.bucket,
                        "prefix": self.prefix,
                        "captured_at": captured_iso,
                        "expires_at": expiry,
                        "source_repository": source_repository,
                        "repository_id": repository_id,
                        "source_snapshot_id": source_snapshot_id,
                        "quiescent_window_confirmed": True,
                        "protection_qualified": False,
                        "copyright": _COPYRIGHT,
                        "copyright_url": _COPYRIGHT_URL,
                        "private_notice": _PRIVATE_NOTICE,
                    }
                    with os.fdopen(file_descriptor, "wb") as stream:
                        for line in (_json_line(header),):
                            stream.write(line)
                            digest.update(line)
                        for item in self._current_versions(connection, cancel_event):
                            line = _json_line(item)
                            stream.write(line)
                            digest.update(line)
                            counts[item["type"]] += 1
                        item_count = counts["object"] + counts["delete_marker"]
                        end_line = _json_line(
                            {
                                "type": "end",
                                "item_count": item_count,
                                "object_count": counts["object"],
                                "delete_marker_count": counts["delete_marker"],
                            }
                        )
                        stream.write(end_line)
                        digest.update(end_line)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    connection.close()
            manifest_sha256 = digest.hexdigest()
            os.link(partial_name, target)
            os.unlink(partial_name)
        except BaseException:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                os.unlink(partial_name)
            except OSError:
                pass
            raise
        return CheckpointRecord(
            path=target,
            manifest_sha256=manifest_sha256,
            item_count=counts["object"] + counts["delete_marker"],
            object_count=counts["object"],
            delete_marker_count=counts["delete_marker"],
            captured_at=captured_iso,
            expires_at=expiry,
            source_repository=source_repository,
            repository_id=repository_id,
            source_snapshot_id=source_snapshot_id,
            endpoint_url=self.endpoint_url,
            bucket=self.bucket,
            prefix=self.prefix,
            quiescent_window_confirmed=True,
            protection_qualified=False,
        )

    @staticmethod
    def _pin_manifest(source: Path, pinned_path: Path, cancel_event=None) -> str:
        """Copy one opened manifest stream into a private file while hashing it."""

        digest = hashlib.sha256()
        source_fd = -1
        pinned_fd = -1
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise CloudError("Restore cancelled before manifest preparation")
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            source_fd = os.open(source, flags)
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
                raise CloudError("Checkpoint manifest must be an existing regular file")
            pinned_fd = os.open(
                pinned_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            total = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CloudError("Restore cancelled during manifest preparation")
                block = os.read(source_fd, _READ_CHUNK_BYTES)
                if not block:
                    break
                total += len(block)
                if total > _MAX_MANIFEST_BYTES:
                    raise CloudError("Checkpoint manifest exceeds size limit")
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(pinned_fd, view)
                    view = view[written:]
            os.fsync(pinned_fd)
        except CloudError:
            raise
        except OSError:
            raise CloudError(
                "Could not securely read and pin checkpoint manifest"
            ) from None
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if pinned_fd >= 0:
                os.close(pinned_fd)
        return digest.hexdigest()

    @staticmethod
    def _manifest_line(stream) -> bytes:
        line = stream.readline(_MAX_MANIFEST_LINE_BYTES + 1)
        if not line or len(line) > _MAX_MANIFEST_LINE_BYTES or not line.endswith(b"\n"):
            raise CloudError("Checkpoint line is missing, too large, or incomplete")
        return line

    def _validate_manifest(
        self,
        manifest_path: Path,
        index_path: Path,
        *,
        expected_source_repository: str,
        expected_repository_id: str,
        expected_source_snapshot_id: str,
        cancel_event=None,
    ) -> tuple[sqlite3.Connection, dict[str, Any], int, int]:
        connection = sqlite3.connect(index_path)
        connection.execute(
            """CREATE TABLE objects (
                ordinal INTEGER PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                version_id TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                size INTEGER
            )"""
        )
        connection.execute("CREATE TABLE seen_keys (key TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE seen_paths (path TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE seen_dirs (path TEXT PRIMARY KEY)")
        object_count = 0
        marker_count = 0
        try:
            with manifest_path.open("rb") as stream:
                header = _strict_json(self._manifest_line(stream))
                required_header = {
                    "type",
                    "schema_version",
                    "provider",
                    "endpoint_url",
                    "bucket",
                    "prefix",
                    "captured_at",
                    "expires_at",
                    "source_repository",
                    "repository_id",
                    "source_snapshot_id",
                    "quiescent_window_confirmed",
                    "protection_qualified",
                    "copyright",
                    "copyright_url",
                    "private_notice",
                }
                if set(header) != required_header or header.get("type") != "checkpoint":
                    raise CloudError("Checkpoint header is invalid")
                if (
                    type(header.get("schema_version")) is not int
                    or header["schema_version"] != SCHEMA_VERSION
                ):
                    raise CloudError("Unsupported checkpoint format")
                if header.get("provider") != "s3-compatible-unqualified":
                    raise CloudError("Checkpoint provider is not supported")
                if header.get("endpoint_url") != self.endpoint_url:
                    raise CloudError("Checkpoint belongs to a different S3 endpoint")
                if (
                    header.get("bucket") != self.bucket
                    or header.get("prefix") != self.prefix
                ):
                    raise CloudError(
                        "Checkpoint belongs to a different bucket or prefix"
                    )
                if header.get("quiescent_window_confirmed") is not True:
                    raise CloudError("Checkpoint lacks quiescent-window confirmation")
                if header.get("protection_qualified") is not False:
                    raise CloudError("Checkpoint makes an unsupported protection claim")
                if (
                    header.get("copyright") != _COPYRIGHT
                    or header.get("copyright_url") != _COPYRIGHT_URL
                    or header.get("private_notice") != _PRIVATE_NOTICE
                ):
                    raise CloudError("Checkpoint ownership metadata is invalid")
                captured_at = _parse_time(header.get("captured_at"))
                expires_at = header.get("expires_at")
                if expires_at is not None:
                    if _parse_time(expires_at) <= captured_at:
                        raise CloudError(
                            "Checkpoint expiry must follow its capture time"
                        )
                if (
                    header.get("source_repository") != expected_source_repository
                    or header.get("repository_id") != expected_repository_id
                    or header.get("source_snapshot_id") != expected_source_snapshot_id
                ):
                    raise CloudError("Checkpoint source identity does not match")
                ordinal = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise CloudError("Restore cancelled during manifest validation")
                    line = self._manifest_line(stream)
                    row = _strict_json(line)
                    if row.get("type") == "end":
                        expected_end = {
                            "type",
                            "item_count",
                            "object_count",
                            "delete_marker_count",
                        }
                        if set(row) != expected_end:
                            raise CloudError("Checkpoint summary is invalid")
                        if (
                            type(row["item_count"]) is not int
                            or type(row["object_count"]) is not int
                            or type(row["delete_marker_count"]) is not int
                            or row["item_count"] != ordinal
                            or row["object_count"] != object_count
                            or row["delete_marker_count"] != marker_count
                        ):
                            raise CloudError(
                                "Checkpoint summary does not match its inventory"
                            )
                        if stream.read(1):
                            raise CloudError("Checkpoint has trailing data")
                        break
                    kind = row.get("type")
                    expected_fields = (
                        {"type", "key", "version_id", "size"}
                        if kind == "object"
                        else {"type", "key", "version_id"}
                        if kind == "delete_marker"
                        else set()
                    )
                    if not expected_fields or set(row) != expected_fields:
                        raise CloudError("Checkpoint inventory entry is invalid")
                    key = row.get("key")
                    relative = _relative_key(key, self.prefix)
                    if _is_lock_key(relative):
                        raise CloudError("Checkpoint contains a transient Restic lock")
                    version_id = row.get("version_id")
                    if not isinstance(version_id, str) or not version_id:
                        raise CloudError("Checkpoint version ID is invalid")
                    size = row.get("size") if kind == "object" else None
                    if kind == "object" and (type(size) is not int or size < 0):
                        raise CloudError("Checkpoint object size is invalid")
                    _insert_safe_path(connection, key, relative)
                    connection.execute(
                        "INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?)",
                        (ordinal, key, version_id, relative, kind, size),
                    )
                    ordinal += 1
                    if kind == "object":
                        object_count += 1
                    else:
                        marker_count += 1
        except BaseException:
            connection.close()
            raise
        return connection, header, object_count, marker_count

    @staticmethod
    def _empty_destination(destination: Path) -> None:
        _ensure_directory_path(destination)
        try:
            next(destination.iterdir())
        except StopIteration:
            return
        raise CloudError("Restore destination must be an empty local directory")

    @staticmethod
    def _target_file(root: Path, relative: str) -> Path:
        parts = relative.split("/")
        current = root
        for part in parts[:-1]:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise CloudError(
                        "Restore path traverses a symlink or non-directory"
                    ) from None
        target = current / parts[-1]
        if os.path.lexists(target):
            raise CloudError("Restore refuses to overwrite an existing path")
        return target

    def restore_checkpoint(
        self,
        manifest_path: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        expected_manifest_sha256: str,
        expected_source_repository: str,
        expected_repository_id: str,
        expected_source_snapshot_id: str,
        cancel_event: Event | None = None,
    ) -> RestoreRecord:
        """Restore exact object versions after caller-side signature verification.

        ``expected_manifest_sha256`` must come from the caller's trusted record
        (for example, after detached-signature verification). Partial files are
        deliberately preserved if a download or cancellation fails.
        """

        _source_identity(expected_source_repository, expected_source_snapshot_id)
        _repository_identity(expected_repository_id)
        if not isinstance(expected_manifest_sha256, str) or not _FULL_ID.fullmatch(
            expected_manifest_sha256
        ):
            raise CloudError("A trusted full manifest SHA256 is required")
        manifest = Path(manifest_path)
        if not manifest.is_file() or manifest.is_symlink():
            raise CloudError("Checkpoint manifest must be an existing regular file")
        target = Path(destination)
        self._empty_destination(target)
        files_restored = 0
        bytes_restored = 0
        with tempfile.TemporaryDirectory(prefix="cloud-restore-index-") as index_dir:
            pinned_manifest = Path(index_dir) / "trusted-manifest.jsonl"
            manifest_sha256 = self._pin_manifest(manifest, pinned_manifest, cancel_event)
            if manifest_sha256 != expected_manifest_sha256:
                raise CloudError("Checkpoint manifest SHA256 does not match")
            connection, _header, _objects, marker_count = self._validate_manifest(
                pinned_manifest,
                Path(index_dir) / "objects.sqlite3",
                expected_source_repository=expected_source_repository,
                expected_repository_id=expected_repository_id,
                expected_source_snapshot_id=expected_source_snapshot_id,
                cancel_event=cancel_event,
            )
            try:
                rows = connection.execute(
                    "SELECT key, version_id, relative_path, kind, size FROM objects ORDER BY ordinal"
                )
                for key, version_id, relative, kind, size in rows:
                    if cancel_event is not None and cancel_event.is_set():
                        raise CloudError(
                            "Restore interrupted; partial destination is preserved"
                        )
                    if kind == "delete_marker":
                        continue
                    try:
                        response = self.client.get_object(
                            Bucket=self.bucket, Key=key, VersionId=version_id
                        )
                    except Exception:
                        raise CloudError(
                            "Could not fetch an exact checkpoint object version; partial destination is preserved"
                        ) from None
                    body = response.get("Body") if isinstance(response, dict) else None
                    if body is None or not hasattr(body, "read"):
                        raise CloudError("Object response has no readable body")
                    if response.get("VersionId") != version_id:
                        try:
                            body.close()
                        except Exception:
                            pass
                        raise CloudError(
                            "Object response version does not match the checkpoint; partial destination is preserved"
                        )
                    try:
                        target_path = self._target_file(target, relative)
                    except BaseException:
                        try:
                            body.close()
                        except Exception:
                            pass
                        raise
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        descriptor = os.open(target_path, flags, 0o600)
                    except OSError:
                        try:
                            body.close()
                        except Exception:
                            pass
                        raise CloudError(
                            "Could not create a new restore file"
                        ) from None
                    restored_size = 0
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            while True:
                                if cancel_event is not None and cancel_event.is_set():
                                    raise CloudError(
                                        "Restore interrupted; partial destination is preserved"
                                    )
                                try:
                                    block = body.read(_READ_CHUNK_BYTES)
                                except Exception:
                                    raise CloudError(
                                        "Could not read an exact checkpoint object version; partial destination is preserved"
                                    ) from None
                                if not block:
                                    break
                                if not isinstance(block, bytes):
                                    raise CloudError(
                                        "Object stream returned invalid data"
                                    )
                                if restored_size + len(block) > size:
                                    raise CloudError(
                                        "Object stream exceeded checkpoint size; partial destination is preserved"
                                    )
                                output.write(block)
                                restored_size += len(block)
                            output.flush()
                            os.fsync(output.fileno())
                    finally:
                        try:
                            body.close()
                        except Exception:
                            pass
                    if restored_size != size:
                        raise CloudError(
                            "Restored object size does not match the checkpoint; partial destination is preserved"
                        )
                    files_restored += 1
                    bytes_restored += restored_size
            finally:
                connection.close()
        return RestoreRecord(
            manifest_sha256=manifest_sha256,
            files_restored=files_restored,
            bytes_restored=bytes_restored,
            delete_markers_preserved_as_absent=marker_count,
            destination=target,
            expires_at=_header["expires_at"],
            protection_expired=(
                _header["expires_at"] is not None
                and _parse_time(_header["expires_at"]) <= datetime.now(timezone.utc)
            ),
        )


__all__ = [
    "CheckpointRecord",
    "CloudError",
    "LifecycleInspection",
    "RestoreRecord",
    "S3CloudAdapter",
]
