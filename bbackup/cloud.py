"""S3-compatible version inventory, checkpoint restore, and reviewed retention.

Retention deletion requires a private central policy and recurring grant,
verified central recovery records, a fresh immutable plan, and a coordinated
quiescent window. The module never creates a production protection claim.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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

    def plan_central_retention(
        self,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        *,
        quiescent_window_confirmed: bool,
        repository_lease_id: str,
    ) -> dict[str, Any]:
        """Create a private immutable retention plan without deleting anything."""
        return _CentralRetention(self).plan(
            policy_path,
            grant_path,
            recovery_record_dirs,
            quiescent_window_confirmed=quiescent_window_confirmed,
            repository_lease_id=repository_lease_id,
        )

    def initialize_central_retention_catalog(
        self,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Explicitly enroll a new private catalog before the first plan."""
        return _CentralRetention(self).initialize_catalog(policy_path, grant_path)

    def apply_central_retention(
        self,
        plan_id: str,
        expected_plan_sha256: str,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        *,
        quiescent_window_confirmed: bool,
        ledger: Any,
        cancel_event: Event | None = None,
        mutation_client: Any | None = None,
        expected_repository_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one reviewed exact-version plan without replaying uncertainty."""
        return _CentralRetention(self).apply(
            plan_id,
            expected_plan_sha256,
            policy_path,
            grant_path,
            recovery_record_dirs,
            quiescent_window_confirmed=quiescent_window_confirmed,
            ledger=ledger,
            cancel_event=cancel_event,
            mutation_client=mutation_client,
            expected_repository_lease_id=expected_repository_lease_id,
        )

    def reconcile_central_retention(
        self,
        plan_path: str | os.PathLike[str],
        *,
        ledger: Any,
        expected_repository_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Read exact planned version IDs and reconcile without deleting."""
        return _CentralRetention(self).reconcile(
            plan_path, ledger=ledger,
            expected_repository_lease_id=expected_repository_lease_id,
        )


_RETENTION_POLICY_FIELDS = {
    "schema_version",
    "policy_id",
    "catalog_id",
    "revision",
    "endpoint_url",
    "bucket",
    "prefix",
    "repository_id",
    "trusted_verifier",
    "retention_counts",
    "grace_seconds",
    "plan_max_age_seconds",
    "max_apply_objects",
    "max_apply_bytes",
    "state_dir",
    "known_good_manifest_digests",
    "pending_manifest_digests",
}
_RETENTION_GRANT_FIELDS = {
    "schema_version",
    "policy_id",
    "catalog_id",
    "policy_revision",
    "policy_sha256",
    "endpoint_url",
    "bucket",
    "prefix",
    "repository_id",
    "action",
    "expires_at",
    "max_apply_objects",
    "max_apply_bytes",
    "stop_conditions",
}
_RETENTION_STOP_CONDITIONS = [
    "cancelled",
    "input_drift",
    "lock_unknown_or_changed",
    "mutation_outcome_uncertain",
    "policy_or_grant_expired",
]
_MIN_RETENTION_GRACE_SECONDS = 30 * 24 * 60 * 60
_PRIVATE_JSON_LIMIT = 16 * 1024 * 1024
_RETENTION_PLAN_FIELDS = {
    "schema_version",
    "operation_id",
    "created_at",
    "deadline",
    "target",
    "repository_lease_id",
    "policy_id",
    "policy_revision",
    "policy_sha256",
    "grant_sha256",
    "grant_expires_at",
    "apply_limits",
    "inventory_sha256",
    "version_count",
    "catalog_sha256",
    "accepted_manifest_digests",
    "selected_manifest_digests",
    "protected_sha256",
    "protected_version_count",
    "candidate_lock_sha256",
    "held_count",
    "bucket_evidence_sha256",
    "object_lock_enabled",
    "candidates",
    "key_inventory",
}
_RETENTION_JOURNAL_FIELDS = {
    "schema_version",
    "operation_id",
    "plan_sha256",
    "status",
    "updated_at",
    "items",
}


def _retention_json_default(item: Any) -> str:
    if isinstance(item, datetime):
        return _iso(item)
    raise TypeError("unsupported retention metadata")


def _retention_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_retention_json_default,
        )
        + "\n"
    ).encode("utf-8")


def _retention_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _retention_service_fields(response: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


def _no_symlink_path(path: Path, *, include_leaf: bool = True) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CloudError("Retention paths must be absolute and traversal-free")
    parts = path.parts if include_leaf else path.parts[:-1]
    current = Path(path.anchor)
    for part in parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise CloudError("Retention paths cannot contain symlinks")


def _ensure_private_directory(path: Path, *, create: bool = False) -> Path:
    path = path.absolute()
    _no_symlink_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise CloudError("Private retention state directory is missing") from None
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _no_symlink_path(path)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise CloudError("Retention state directory must be private")
    return path


def _read_private_json(path: Path, *, limit: int = _PRIVATE_JSON_LIMIT) -> tuple[dict[str, Any], bytes]:
    path = path.absolute()
    _no_symlink_path(path)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > limit
        ):
            raise CloudError("Retention metadata must be a bounded private file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise CloudError("Retention metadata exceeds its size limit")
        raw = b"".join(chunks)
        def unique_pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except CloudError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise CloudError("Retention metadata could not be read or parsed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise CloudError("Retention metadata must be a JSON object")
    return value, raw


def _write_private_json(path: Path, value: Mapping[str, Any], *, replace: bool) -> bytes:
    raw = _retention_json_bytes(value)
    _ensure_private_directory(path.parent)
    _no_symlink_path(path)
    descriptor = -1
    temporary = None
    try:
        if replace:
            descriptor, name = tempfile.mkstemp(prefix=".retention-", dir=path.parent)
            temporary = Path(name)
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if replace:
            os.replace(temporary, path)
            temporary = None
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except CloudError:
        raise
    except OSError:
        raise CloudError("Could not durably write private retention state") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return raw


def _write_immutable_plan(path: Path, value: Mapping[str, Any]) -> bytes:
    raw = _retention_json_bytes(value)
    _ensure_private_directory(path.parent)
    _no_symlink_path(path)
    descriptor = -1
    created = False
    complete = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        created = True
        os.fchmod(descriptor, 0o400)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        complete = True
    except OSError:
        raise CloudError("Could not durably create immutable retention plan") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not complete and os.path.lexists(path):
            try:
                path.unlink()
            except OSError:
                pass
    return raw


def _validate_retention_policy(
    policy: Mapping[str, Any], adapter: S3CloudAdapter
) -> tuple[dict[str, Any], Path]:
    if set(policy) != _RETENTION_POLICY_FIELDS:
        raise CloudError("Central retention policy schema is invalid")
    if type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
        raise CloudError("Unsupported central retention policy version")
    if not isinstance(policy.get("policy_id"), str) or not _SOURCE_NAME.fullmatch(policy["policy_id"]):
        raise CloudError("Central retention policy identity is invalid")
    if not isinstance(policy.get("catalog_id"), str) or not _FULL_ID.fullmatch(policy["catalog_id"]):
        raise CloudError("Central retention catalog identity is invalid")
    if type(policy.get("revision")) is not int or policy["revision"] < 1:
        raise CloudError("Central retention policy revision is invalid")
    try:
        endpoint = _normalize_endpoint_url(policy.get("endpoint_url"))
    except ValueError:
        raise CloudError("Central retention endpoint is invalid") from None
    if endpoint != adapter.endpoint_url or policy.get("bucket") != adapter.bucket:
        raise CloudError("Central retention target differs from the S3 adapter")
    if (
        not isinstance(policy.get("prefix"), str)
        or not policy["prefix"]
        or policy["prefix"] != adapter.prefix
    ):
        raise CloudError("Central retention prefix differs from the S3 adapter")
    _repository_identity(policy.get("repository_id"))
    try:
        from .recovery import _fingerprint

        verifier = _fingerprint(policy.get("trusted_verifier"))
    except Exception:
        raise CloudError("Central retention verifier fingerprint is invalid") from None
    counts = policy.get("retention_counts")
    if not isinstance(counts, dict) or set(counts) != {"daily", "weekly", "monthly"}:
        raise CloudError("Central retention checkpoint counts are invalid")
    if any(type(counts[name]) is not int or counts[name] < 0 for name in counts):
        raise CloudError("Central retention checkpoint counts are invalid")
    for name, minimum in (
        ("grace_seconds", _MIN_RETENTION_GRACE_SECONDS),
        ("plan_max_age_seconds", 1),
        ("max_apply_objects", 1),
        ("max_apply_bytes", 1),
    ):
        if type(policy.get(name)) is not int or policy[name] < minimum:
            raise CloudError("Central retention limits are invalid")
    digests = {}
    for name in ("known_good_manifest_digests", "pending_manifest_digests"):
        values = policy.get(name)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not _FULL_ID.fullmatch(value) for value in values)
            or len(set(values)) != len(values)
        ):
            raise CloudError("Central retention manifest pins are invalid")
        digests[name] = list(values)
    if not digests["known_good_manifest_digests"]:
        raise CloudError("Central retention requires an explicit known-good recovery checkpoint pin")
    state_value = policy.get("state_dir")
    if not isinstance(state_value, str) or not Path(state_value).is_absolute():
        raise CloudError("Central retention state directory must be absolute")
    state_dir = _ensure_private_directory(Path(state_value), create=True)
    clean = dict(policy)
    clean["endpoint_url"] = endpoint
    clean["trusted_verifier"] = verifier
    clean.update(digests)
    return clean, state_dir


def _validate_retention_grant(
    grant: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    policy_digest: str,
    now: datetime,
) -> tuple[dict[str, Any], datetime]:
    if set(grant) != _RETENTION_GRANT_FIELDS:
        raise CloudError("Recurring retention grant schema is invalid")
    if type(grant.get("schema_version")) is not int or grant["schema_version"] != 1:
        raise CloudError("Unsupported recurring retention grant version")
    expected = {
        "policy_id": policy["policy_id"],
        "catalog_id": policy["catalog_id"],
        "policy_revision": policy["revision"],
        "policy_sha256": policy_digest,
        "endpoint_url": policy["endpoint_url"],
        "bucket": policy["bucket"],
        "prefix": policy["prefix"],
        "repository_id": policy["repository_id"],
        "action": "delete_noncurrent_versions",
    }
    if any(grant.get(key) != value for key, value in expected.items()):
        raise CloudError("Recurring retention grant does not match this policy target")
    try:
        expires = _parse_time(grant.get("expires_at"))
    except CloudError:
        raise CloudError("Recurring retention grant expiry is invalid") from None
    if expires <= now:
        raise CloudError("Recurring retention grant has expired")
    for name in ("max_apply_objects", "max_apply_bytes"):
        value = grant.get(name)
        if type(value) is not int or value < 1 or value > policy[name]:
            raise CloudError("Recurring retention grant limits are invalid")
    if grant.get("stop_conditions") != _RETENTION_STOP_CONDITIONS:
        raise CloudError("Recurring retention grant stop conditions are invalid")
    return dict(grant), expires


def _retention_version_token(key: str, version_id: str) -> str:
    return _retention_digest(_retention_json_bytes({"key": key, "version_id": version_id}))


def _retention_target(policy: Mapping[str, Any]) -> dict[str, str]:
    return {
        "endpoint_url": policy["endpoint_url"],
        "bucket": policy["bucket"],
        "prefix": policy["prefix"],
        "repository_id": policy["repository_id"],
    }


def _validate_catalog(catalog: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    if set(catalog) != {
        "schema_version",
        "catalog_id",
        "target",
        "accepted_records",
        "first_observed_noncurrent",
    } or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise CloudError("Central retention catalog schema is invalid")
    if catalog.get("target") != _retention_target(policy):
        raise CloudError("Central retention catalog belongs to a different target")
    if catalog.get("catalog_id") != policy["catalog_id"]:
        raise CloudError("Central retention catalog identity differs from authority")
    accepted = catalog.get("accepted_records")
    first_seen = catalog.get("first_observed_noncurrent")
    if not isinstance(accepted, dict) or not isinstance(first_seen, dict):
        raise CloudError("Central retention catalog is invalid")
    for digest, entry in accepted.items():
        if (
            not isinstance(digest, str)
            or not _FULL_ID.fullmatch(digest)
            or not isinstance(entry, dict)
            or set(entry) != {"snapshot_id", "verified_at", "source_repository", "captured_at", "expires_at", "source_signer"}
        ):
            raise CloudError("Central retention catalog is invalid")
        if not isinstance(entry["snapshot_id"], str) or not _FULL_ID.fullmatch(entry["snapshot_id"]):
            raise CloudError("Central retention catalog is invalid")
        if not isinstance(entry["source_repository"], str) or not _SOURCE_NAME.fullmatch(entry["source_repository"]):
            raise CloudError("Central retention catalog is invalid")
        if not isinstance(entry["source_signer"], str) or not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", entry["source_signer"]):
            raise CloudError("Central retention catalog is invalid")
        for time_name in ("verified_at", "captured_at"):
            _parse_time(entry[time_name])
        if entry["expires_at"] is not None:
            _parse_time(entry["expires_at"])
    for token, timestamp in first_seen.items():
        if not isinstance(token, str) or not _FULL_ID.fullmatch(token):
            raise CloudError("Central retention catalog is invalid")
        if timestamp is not None:
            _parse_time(timestamp)
    return dict(catalog)


def _select_retention_records(
    records: Sequence[Mapping[str, Any]], counts: Mapping[str, int]
) -> set[str]:
    selected: set[str] = set()
    periods = (
        ("daily", lambda value: value.strftime("%Y-%m-%d")),
        ("weekly", lambda value: f"{value.isocalendar().year:04d}-W{value.isocalendar().week:02d}"),
        ("monthly", lambda value: value.strftime("%Y-%m")),
    )
    for name, bucket_for in periods:
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            verified = _parse_time(record["verified_at"])
            buckets.setdefault(bucket_for(verified), []).append(record)
        ordered = sorted(
            (
                min(items, key=lambda item: (_parse_time(item["verified_at"]), item["manifest_sha256"]))
                for items in buckets.values()
            ),
            key=lambda item: (_parse_time(item["verified_at"]), item["manifest_sha256"]),
        )
        count = counts[name]
        if count:
            selected.update(item["manifest_sha256"] for item in ordered[-count:])
    return selected


@contextmanager
def _retention_catalog_lock(state_dir: Path):
    path = state_dir / "catalog.lock"
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError:
        raise CloudError("Could not acquire the private retention catalog lock") from None
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class _CentralRetention:
    """Private reviewed retention workflow owned by the S3 cloud adapter."""

    def __init__(self, adapter: S3CloudAdapter) -> None:
        self.adapter = adapter

    def _authority(
        self,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
    ) -> tuple[dict[str, Any], dict[str, Any], Path, datetime, str, str, Path, Path]:
        policy_file = Path(policy_path).absolute()
        grant_file = Path(grant_path).absolute()
        policy, policy_raw = _read_private_json(policy_file)
        clean_policy, state_dir = _validate_retention_policy(policy, self.adapter)
        grant, grant_raw = _read_private_json(grant_file)
        now = datetime.now(timezone.utc)
        clean_grant, grant_expiry = _validate_retention_grant(
            grant,
            clean_policy,
            policy_digest=_retention_digest(policy_raw),
            now=now,
        )
        return (
            clean_policy,
            clean_grant,
            state_dir,
            now,
            _retention_digest(policy_raw),
            _retention_digest(grant_raw),
            policy_file,
            grant_file,
        )

    def _assert_live_authority(
        self,
        plan: Mapping[str, Any],
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
    ) -> None:
        if datetime.now(timezone.utc) >= _parse_time(plan["deadline"]):
            raise CloudError("Central retention plan has expired")
        latest = self._authority(policy_path, grant_path)
        if latest[4] != plan["policy_sha256"] or latest[5] != plan["grant_sha256"]:
            raise CloudError("Central retention authority changed")
        if datetime.now(timezone.utc) >= _parse_time(plan["deadline"]):
            raise CloudError("Central retention plan has expired")

    def _pin_records(
        self,
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        policy: Mapping[str, Any],
        temporary: Path,
        now: datetime,
    ) -> list[dict[str, Any]]:
        from . import recovery

        if not isinstance(recovery_record_dirs, Sequence) or isinstance(
            recovery_record_dirs, (str, bytes)
        ) or not recovery_record_dirs:
            raise CloudError("At least one accepted recovery record is required")
        records = []
        seen_digests = set()
        for index, source_value in enumerate(recovery_record_dirs):
            try:
                source_dir = Path(source_value).absolute()
            except (TypeError, ValueError):
                raise CloudError("Recovery record directory is invalid") from None
            _ensure_private_directory(source_dir)
            pinned_dir = temporary / f"record-{index:06d}"
            pinned_dir.mkdir(mode=0o700)
            try:
                for filename, limit in (
                    ("acceptance.json", 16384),
                    ("acceptance.sig", 1024**2),
                    ("checkpoint.sig", 1024**2),
                ):
                    recovery._copy(
                        source_dir / filename,
                        pinned_dir / filename,
                        limit=limit,
                    )
                candidate, _candidate_raw = _read_private_json(
                    pinned_dir / "acceptance.json", limit=16384
                )
                snapshot = candidate.get("snapshot_id")
                if (
                    candidate.get("repository_id") != policy["repository_id"]
                    or not isinstance(snapshot, str)
                    or not _FULL_ID.fullmatch(snapshot)
                ):
                    raise CloudError("Recovery acceptance identity is invalid")
                verified = recovery.verify_recovery_acceptance(
                    pinned_dir,
                    trusted_verifier=policy["trusted_verifier"],
                    repository_id=policy["repository_id"],
                    snapshot=snapshot,
                )
                if verified != candidate:
                    raise CloudError("Recovery acceptance changed while being verified")
                manifest_path = pinned_dir / "checkpoint.jsonl"
                manifest_digest = self.adapter._pin_manifest(
                    source_dir / "checkpoint.jsonl", manifest_path
                )
                if manifest_digest != verified.get("manifest_sha256"):
                    raise CloudError("Recovery manifest differs from its central acceptance")
                signature_digest = recovery.verify_checkpoint(
                    manifest_path,
                    pinned_dir / "checkpoint.sig",
                    verified["source_signer"],
                )
                if signature_digest != manifest_digest:
                    raise CloudError("Recovery source signature does not match its manifest")
                index_path = temporary / f"manifest-index-{index:06d}.sqlite3"
                connection, header, object_count, _marker_count = self.adapter._validate_manifest(
                    manifest_path,
                    index_path,
                    expected_source_repository=verified["source_repository"],
                    expected_repository_id=policy["repository_id"],
                    expected_source_snapshot_id=snapshot,
                )
                try:
                    connection.commit()
                    manifest_bytes = connection.execute(
                        "SELECT COALESCE(SUM(size), 0) FROM objects WHERE kind = 'object'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                verified_time = _parse_time(verified["verified_at"])
                if verified_time > now:
                    raise CloudError("Recovery acceptance is future-dated")
                if (
                    _parse_time(header["captured_at"])
                    != _parse_time(verified["captured_at"])
                    or header["expires_at"] != verified["expires_at"]
                    or object_count != verified["files_restored"]
                    or manifest_bytes != verified["bytes_restored"]
                ):
                    raise CloudError("Recovery manifest does not match its acceptance record")
                if manifest_digest in seen_digests:
                    raise CloudError("Duplicate recovery manifest was supplied")
                seen_digests.add(manifest_digest)
                records.append(
                    {
                        "manifest_sha256": manifest_digest,
                        "snapshot_id": snapshot,
                        "verified_at": _iso(verified_time),
                        "source_repository": verified["source_repository"],
                        "captured_at": _iso(_parse_time(verified["captured_at"])),
                        "expires_at": (
                            _iso(_parse_time(verified["expires_at"]))
                            if verified["expires_at"] is not None
                            else None
                        ),
                        "source_signer": verified["source_signer"],
                        "index_path": str(index_path),
                    }
                )
            except CloudError:
                raise
            except Exception:
                raise CloudError("Central recovery record could not be verified") from None
        return records

    @staticmethod
    def _catalog_path(state_dir: Path) -> Path:
        return state_dir / "retention-catalog.json"

    def _load_catalog(
        self, state_dir: Path, policy: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        path = self._catalog_path(state_dir)
        if not os.path.lexists(path):
            raise CloudError("Central retention catalog is missing; explicit enrollment is required")
        catalog, raw = _read_private_json(path)
        return _validate_catalog(catalog, policy), raw

    def initialize_catalog(
        self,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        policy, _grant, state_dir, _now, _policy_sha, _grant_sha = self._authority(
            policy_path, grant_path
        )[:6]
        with _retention_catalog_lock(state_dir):
            policy = self._authority(policy_path, grant_path)[0]
            path = self._catalog_path(state_dir)
            if os.path.lexists(path):
                raise CloudError("Central retention catalog is already enrolled")
            for name in ("retention-plans", "retention-operations"):
                directory = state_dir / name
                if os.path.lexists(directory):
                    _ensure_private_directory(directory)
                    if any(directory.iterdir()):
                        raise CloudError("Central retention history exists; lost catalog cannot be re-enrolled")
            catalog = {
                "schema_version": 1,
                "catalog_id": policy["catalog_id"],
                "target": _retention_target(policy),
                "accepted_records": {},
                "first_observed_noncurrent": {},
            }
            raw = _write_private_json(path, catalog, replace=False)
            return {"status": "enrolled", "catalog_id": policy["catalog_id"], "catalog_sha256": _retention_digest(raw)}

    def _merge_catalog_records(
        self,
        catalog: dict[str, Any],
        records: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> None:
        current = {
            record["manifest_sha256"]: {
                "snapshot_id": record["snapshot_id"],
                "verified_at": record["verified_at"],
                "source_repository": record["source_repository"],
                "captured_at": record["captured_at"],
                "expires_at": record["expires_at"],
                "source_signer": record["source_signer"],
            }
            for record in records
        }
        previous = catalog["accepted_records"]
        if set(previous) - set(current):
            raise CloudError("A previously accepted recovery record is missing from the catalog input")
        snapshot_digests = {
            entry["snapshot_id"]: digest for digest, entry in previous.items()
        }
        for digest, entry in current.items():
            old = previous.get(digest)
            if old is not None and old != entry:
                raise CloudError("A recovery acceptance was replaced or re-accepted")
            old_digest = snapshot_digests.get(entry["snapshot_id"])
            if old_digest is not None and old_digest != digest:
                raise CloudError("A previously accepted snapshot was re-accepted with a new manifest")
        merged = dict(previous)
        merged.update(current)
        pinned = set(policy["known_good_manifest_digests"]) | set(
            policy["pending_manifest_digests"]
        )
        if pinned - set(merged):
            raise CloudError("A policy-pinned recovery manifest is missing")
        catalog["accepted_records"] = merged

    def _observe_noncurrent(
        self,
        catalog: dict[str, Any],
        inventory: sqlite3.Connection,
        *,
        now: datetime,
    ) -> None:
        first_seen = catalog["first_observed_noncurrent"]
        for key, version_id, kind, is_latest in inventory.execute(
            "SELECT key, version_id, kind, is_latest FROM versions ORDER BY key, version_id"
        ):
            if kind != "object":
                continue
            token = _retention_version_token(key, version_id)
            if is_latest:
                first_seen[token] = None
                continue
            value = first_seen.get(token)
            if value is None:
                first_seen[token] = _iso(now)
            else:
                timestamp = _parse_time(value)
                if timestamp > now:
                    raise CloudError("Noncurrent-version grace catalog is future-dated")

    def _inventory(self, database_path: Path) -> tuple[sqlite3.Connection, str, int]:
        connection = sqlite3.connect(database_path)
        connection.execute(
            """CREATE TABLE versions (
                key TEXT NOT NULL,
                version_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                size INTEGER,
                is_latest INTEGER NOT NULL,
                PRIMARY KEY(key, version_id)
            )"""
        )
        key_marker = None
        version_marker = None
        page_markers = set()
        try:
            while True:
                marker_pair = (key_marker or "", version_marker or "")
                if marker_pair in page_markers:
                    raise CloudError("Object-version pagination repeated a marker")
                page_markers.add(marker_pair)
                request = {
                    "Bucket": self.adapter.bucket,
                    "Prefix": self.adapter.prefix,
                    "MaxKeys": self.adapter.page_size,
                }
                if key_marker is not None:
                    request["KeyMarker"] = key_marker
                if version_marker is not None:
                    request["VersionIdMarker"] = version_marker
                try:
                    response = self.adapter.client.list_object_versions(**request)
                except Exception:
                    raise CloudError("Could not read central retention version inventory") from None
                if not isinstance(response, dict):
                    raise CloudError("Central retention version inventory is malformed")
                for collection, kind in (("Versions", "object"), ("DeleteMarkers", "delete_marker")):
                    values = response.get(collection, [])
                    if not isinstance(values, list):
                        raise CloudError("Central retention version inventory is malformed")
                    for item in values:
                        if not isinstance(item, dict) or type(item.get("IsLatest")) is not bool:
                            raise CloudError("Central retention version inventory is malformed")
                        key = item.get("Key")
                        version_id = item.get("VersionId")
                        _relative_key(key, self.adapter.prefix)
                        if not isinstance(version_id, str) or not version_id:
                            raise CloudError("Central retention version ID is missing")
                        size = item.get("Size") if kind == "object" else None
                        if kind == "object" and (type(size) is not int or size < 0):
                            raise CloudError("Central retention version size is invalid")
                        try:
                            connection.execute(
                                "INSERT INTO versions VALUES (?, ?, ?, ?, ?)",
                                (key, version_id, kind, size, int(item["IsLatest"])),
                            )
                        except sqlite3.IntegrityError:
                            raise CloudError("Central retention inventory contains duplicate versions") from None
                truncated = response.get("IsTruncated", False)
                if type(truncated) is not bool:
                    raise CloudError("Central retention inventory pagination state is invalid")
                if not truncated:
                    break
                next_key = response.get("NextKeyMarker")
                next_version = response.get("NextVersionIdMarker")
                if not isinstance(next_key, str) or not next_key:
                    raise CloudError("Truncated central inventory lacks a key marker")
                if next_version is not None and not isinstance(next_version, str):
                    raise CloudError("Central inventory version marker is invalid")
                next_pair = (next_key, next_version or "")
                if next_pair == marker_pair:
                    raise CloudError("Central inventory pagination did not progress")
                key_marker, version_marker = next_key, next_version
            malformed_latest = connection.execute(
                """SELECT key FROM versions GROUP BY key
                   HAVING SUM(is_latest) != 1 LIMIT 1"""
            ).fetchone()
            if malformed_latest is not None:
                raise CloudError("Central retention inventory has an invalid latest-version set")
            connection.commit()
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(
                "SELECT key, version_id, kind, size, is_latest FROM versions ORDER BY key, version_id"
            ):
                digest.update(_retention_json_bytes({
                    "key": row[0], "version_id": row[1], "kind": row[2],
                    "size": row[3], "is_latest": bool(row[4]),
                }))
                count += 1
            return connection, digest.hexdigest(), count
        except BaseException:
            connection.close()
            raise

    def _bucket_evidence(self) -> dict[str, Any]:
        client = self.adapter.client
        try:
            versioning = client.get_bucket_versioning(Bucket=self.adapter.bucket)
        except Exception:
            raise CloudError("Could not inspect central bucket versioning") from None
        if not isinstance(versioning, dict) or versioning.get("Status") != "Enabled":
            raise CloudError("Central retention requires enabled bucket versioning")
        if set(versioning) - {"Status", "MFADelete", "ResponseMetadata"}:
            raise CloudError("Central bucket versioning evidence contains unknown fields")
        if versioning.get("MFADelete") not in (None, "Disabled", "Enabled"):
            raise CloudError("Central bucket MFA-delete evidence is unknown")
        if versioning.get("MFADelete") == "Enabled":
            raise CloudError("Central retention does not support MFA Delete buckets")
        try:
            lifecycle = client.get_bucket_lifecycle_configuration(Bucket=self.adapter.bucket)
        except Exception as exc:
            if _error_code(exc) == "NoSuchLifecycleConfiguration":
                lifecycle = {"Rules": [], "status": "NoSuchLifecycleConfiguration"}
            else:
                raise CloudError("Could not inspect central bucket lifecycle") from None
        if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get("Rules", []), list):
            raise CloudError("Central bucket lifecycle evidence is malformed")
        versioning = _retention_service_fields(versioning)
        lifecycle = _retention_service_fields(lifecycle)
        if set(lifecycle) - {"Rules", "TransitionDefaultMinimumObjectSize", "status"}:
            raise CloudError("Central bucket lifecycle evidence contains unknown fields")
        rules = lifecycle.get("Rules", [])
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or rule.get("Status") not in ("Enabled", "Disabled")
                or set(rule)
                - {
                    "ID",
                    "Prefix",
                    "Filter",
                    "Status",
                    "Expiration",
                    "Transitions",
                    "NoncurrentVersionTransitions",
                    "NoncurrentVersionExpiration",
                    "AbortIncompleteMultipartUpload",
                    "ExpiredObjectDeleteMarker",
                }
            ):
                raise CloudError("Central bucket lifecycle evidence is malformed")
            if rule["Status"] == "Enabled" and any(
                name in rule for name in (
                    "Expiration", "NoncurrentVersionExpiration", "ExpiredObjectDeleteMarker"
                )
            ):
                raise CloudError("Enabled bucket lifecycle expiration blocks central retention")
        try:
            object_lock = client.get_object_lock_configuration(Bucket=self.adapter.bucket)
            if not isinstance(object_lock, dict):
                raise CloudError("Central object-lock evidence is malformed")
            object_lock = _retention_service_fields(object_lock)
            if set(object_lock) != {"ObjectLockConfiguration"}:
                raise CloudError("Central object-lock evidence contains unknown fields")
            config = object_lock.get("ObjectLockConfiguration")
            if (
                not isinstance(config, dict)
                or config.get("ObjectLockEnabled") != "Enabled"
                or set(config) - {"ObjectLockEnabled", "Rule"}
            ):
                raise CloudError("Central object-lock configuration is unknown or disabled")
            rule = config.get("Rule")
            if rule is not None:
                if not isinstance(rule, dict) or set(rule) != {"DefaultRetention"}:
                    raise CloudError("Central default object-lock evidence is malformed")
                default_retention = rule["DefaultRetention"]
                if (
                    not isinstance(default_retention, dict)
                    or default_retention.get("Mode") not in {"GOVERNANCE", "COMPLIANCE"}
                    or set(default_retention) not in ({"Mode", "Days"}, {"Mode", "Years"})
                    or type(default_retention.get("Days", default_retention.get("Years"))) is not int
                    or default_retention.get("Days", default_retention.get("Years")) < 1
                ):
                    raise CloudError("Central default object-lock evidence is malformed")
            lock_enabled = True
        except CloudError:
            raise
        except Exception as exc:
            if _error_code(exc) in {"ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"}:
                object_lock = {"status": _error_code(exc)}
                lock_enabled = False
            else:
                raise CloudError("Could not inspect central object-lock configuration") from None
        return {
            "versioning": versioning,
            "lifecycle": lifecycle,
            "object_lock": object_lock,
            "object_lock_enabled": lock_enabled,
            "digests": {
                "versioning_sha256": _retention_digest(_retention_json_bytes(versioning)),
                "lifecycle_sha256": _retention_digest(_retention_json_bytes(lifecycle)),
                "object_lock_sha256": _retention_digest(_retention_json_bytes(object_lock)),
            },
        }

    def _version_lock(self, key: str, version_id: str, *, lock_enabled: bool, now: datetime) -> tuple[dict[str, Any], bool]:
        if not lock_enabled:
            return {"bucket_lock_enabled": False}, False
        client = self.adapter.client
        request = {"Bucket": self.adapter.bucket, "Key": key, "VersionId": version_id}
        try:
            legal_response = client.get_object_legal_hold(**request)
            retention_response = client.get_object_retention(**request)
        except Exception:
            raise CloudError("Per-version object-lock evidence is unknown") from None
        if not isinstance(legal_response, dict):
            raise CloudError("Per-version legal-hold evidence is malformed")
        if set(_retention_service_fields(legal_response)) != {"LegalHold"}:
            raise CloudError("Per-version legal-hold evidence contains unknown fields")
        legal = legal_response.get("LegalHold")
        if not isinstance(legal, dict) or legal.get("Status") not in {"ON", "OFF"}:
            raise CloudError("Per-version legal-hold evidence is unknown")
        if not isinstance(retention_response, dict):
            raise CloudError("Per-version retention evidence is malformed")
        if set(_retention_service_fields(retention_response)) - {"Retention"}:
            raise CloudError("Per-version retention evidence contains unknown fields")
        retention = retention_response.get("Retention")
        if retention is None:
            retention = {}
        if not isinstance(retention, dict):
            raise CloudError("Per-version retention evidence is unknown")
        allowed = {"Mode", "RetainUntilDate", "EventHold", "EventHoldDuration"}
        if set(retention) - allowed:
            raise CloudError("Per-version retention evidence contains unknown fields")
        retain_until = None
        if retention:
            if "Mode" not in retention or "RetainUntilDate" not in retention:
                raise CloudError("Per-version retention evidence is incomplete")
            if retention["Mode"] not in {"GOVERNANCE", "COMPLIANCE"}:
                raise CloudError("Per-version retention mode is unknown")
            value = retention["RetainUntilDate"]
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise CloudError("Per-version retention date is unknown")
            retain_until = value.astimezone(timezone.utc)
        event_hold = retention.get("EventHold")
        if event_hold is not None and event_hold not in {"ON", "OFF"}:
            raise CloudError("Per-version event-hold evidence is unknown")
        if "EventHoldDuration" in retention:
            raise CloudError("Per-version event-hold duration is unsupported")
        held = (
            legal["Status"] == "ON"
            or event_hold == "ON"
            or (retain_until is not None and retain_until > now)
        )
        evidence = {
            "bucket_lock_enabled": True,
            "legal_hold": legal["Status"],
            "mode": retention.get("Mode"),
            "retain_until": _iso(retain_until) if retain_until is not None else None,
            "event_hold": event_hold,
        }
        return evidence, held

    @staticmethod
    def _catalog_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "snapshot_id": record["snapshot_id"],
            "verified_at": record["verified_at"],
            "source_repository": record["source_repository"],
            "captured_at": record["captured_at"],
            "expires_at": record["expires_at"],
            "source_signer": record["source_signer"],
        }

    def _protected_versions(
        self,
        records: Sequence[Mapping[str, Any]],
        selected_digests: set[str],
        inventory: sqlite3.Connection,
        path: Path,
    ) -> tuple[sqlite3.Connection, str, int]:
        protected = sqlite3.connect(path)
        protected.execute(
            "CREATE TABLE protected (key TEXT NOT NULL, version_id TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(key, version_id))"
        )
        try:
            for record in records:
                if record["manifest_sha256"] not in selected_digests:
                    continue
                source = sqlite3.connect(record["index_path"])
                try:
                    protected.executemany(
                        """INSERT INTO protected VALUES (?, ?, ?)
                           ON CONFLICT(key, version_id) DO UPDATE SET
                           size = CASE WHEN size = excluded.size THEN size ELSE -1 END""",
                        source.execute(
                            "SELECT key, version_id, size FROM objects WHERE kind = 'object'"
                        ),
                    )
                    protected.commit()
                finally:
                    source.close()
            inventory.execute("ATTACH DATABASE ? AS protected_manifest", (str(path),))
            try:
                unavailable = inventory.execute(
                    """SELECT 1 FROM protected_manifest.protected AS p
                       LEFT JOIN versions AS v ON v.key = p.key AND v.version_id = p.version_id
                       WHERE p.size < 0 OR v.key IS NULL OR v.kind != 'object' OR v.size != p.size
                       LIMIT 1"""
                ).fetchone()
            finally:
                inventory.execute("DETACH DATABASE protected_manifest")
            if unavailable is not None:
                raise CloudError("A protected recovery checkpoint version is missing or has changed size")
            digest = hashlib.sha256()
            count = 0
            for key, version_id, size in protected.execute(
                "SELECT key, version_id, size FROM protected ORDER BY key, version_id"
            ):
                digest.update(_retention_json_bytes({"key": key, "version_id": version_id, "size": size}))
                count += 1
            return protected, digest.hexdigest(), count
        except BaseException:
            protected.close()
            raise

    def _plan_inputs(
        self,
        policy: Mapping[str, Any],
        state_dir: Path,
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        *,
        now: datetime,
        temporary: Path,
        update_catalog: bool,
    ) -> dict[str, Any]:
        records = self._pin_records(recovery_record_dirs, policy, temporary, now)
        catalog, original_catalog_bytes = self._load_catalog(state_dir, policy)
        self._merge_catalog_records(catalog, records, policy)
        inventory, inventory_digest, version_count = self._inventory(
            temporary / "inventory.sqlite3"
        )
        try:
            if update_catalog:
                self._observe_noncurrent(catalog, inventory, now=now)
            accepted = catalog["accepted_records"]
            selected_digests = _select_retention_records(
                [
                    {
                        "manifest_sha256": digest,
                        "verified_at": entry["verified_at"],
                    }
                    for digest, entry in accepted.items()
                ],
                policy["retention_counts"],
            )
            selected_digests.update(policy["known_good_manifest_digests"])
            selected_digests.update(policy["pending_manifest_digests"])
            selected_digests.update(
                digest for digest, entry in accepted.items()
                if now - _parse_time(entry["verified_at"])
                < timedelta(seconds=policy["grace_seconds"])
            )
            protected, protected_digest, protected_count = self._protected_versions(
                records,
                selected_digests,
                inventory,
                temporary / "protected.sqlite3",
            )
            try:
                evidence = self._bucket_evidence()
                first_seen = catalog["first_observed_noncurrent"]
                candidates = []
                held_count = 0
                for key, version_id, kind, size, is_latest in inventory.execute(
                    "SELECT key, version_id, kind, size, is_latest FROM versions ORDER BY key, version_id"
                ):
                    if kind != "object" or is_latest:
                        continue
                    if protected.execute(
                        "SELECT 1 FROM protected WHERE key = ? AND version_id = ?",
                        (key, version_id),
                    ).fetchone():
                        continue
                    token = _retention_version_token(key, version_id)
                    observed = first_seen.get(token)
                    if not isinstance(observed, str):
                        raise CloudError("Noncurrent-version grace evidence is missing")
                    first_time = _parse_time(observed)
                    if first_time > now:
                        raise CloudError("Noncurrent-version grace evidence is future-dated")
                    if (now - first_time).total_seconds() < policy["grace_seconds"]:
                        continue
                    lock, held = self._version_lock(
                        key,
                        version_id,
                        lock_enabled=evidence["object_lock_enabled"],
                        now=now,
                    )
                    lock_digest = _retention_digest(_retention_json_bytes(lock))
                    if held:
                        held_count += 1
                        continue
                    candidates.append(
                        {
                            "key": key,
                            "version_id": version_id,
                            "size": size,
                            "first_observed_noncurrent": observed,
                            "lock_sha256": lock_digest,
                            "object_id": token,
                        }
                    )
                candidate_keys = {candidate["key"] for candidate in candidates}
                key_inventory = {}
                for key in sorted(candidate_keys):
                    key_inventory[key] = [
                        {
                            "version_id": version_id,
                            "kind": kind,
                            "size": size,
                            "is_latest": bool(is_latest),
                        }
                        for version_id, kind, size, is_latest in inventory.execute(
                            "SELECT version_id, kind, size, is_latest FROM versions WHERE key = ? ORDER BY version_id",
                            (key,),
                        )
                    ]
                lock_digest = _retention_digest(
                    _retention_json_bytes(
                        [
                            {"object_id": item["object_id"], "lock_sha256": item["lock_sha256"]}
                            for item in candidates
                        ]
                    )
                )
                if update_catalog:
                    catalog_raw = _retention_json_bytes(catalog)
                    catalog_path = self._catalog_path(state_dir)
                    _write_private_json(
                        catalog_path,
                        catalog,
                        replace=original_catalog_bytes is not None,
                    )
                else:
                    catalog_raw = original_catalog_bytes
                    if catalog_raw is None:
                        catalog_raw = _retention_json_bytes(catalog)
                catalog_digest = _retention_digest(catalog_raw)
                manifest_records = sorted(
                    digest for digest in accepted
                )
                protected.close()
                return {
                    "records": records,
                    "accepted_manifest_digests": manifest_records,
                    "selected_manifest_digests": sorted(selected_digests),
                    "inventory": inventory,
                    "inventory_sha256": inventory_digest,
                    "version_count": version_count,
                    "protected_sha256": protected_digest,
                    "protected_version_count": protected_count,
                    "catalog": catalog,
                    "catalog_sha256": catalog_digest,
                    "evidence": evidence,
                    "candidates": candidates,
                    "key_inventory": key_inventory,
                    "candidate_lock_sha256": lock_digest,
                    "held_count": held_count,
                }
            except BaseException:
                protected.close()
                raise
        except BaseException:
            inventory.close()
            raise

    @staticmethod
    def _plans_directory(state_dir: Path, *, create: bool = False) -> Path:
        return _ensure_private_directory(state_dir / "retention-plans", create=create)

    @staticmethod
    def _operations_directory(state_dir: Path, *, create: bool = False) -> Path:
        return _ensure_private_directory(state_dir / "retention-operations", create=create)

    @staticmethod
    def _plan_path(state_dir: Path, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise CloudError("Central retention plan ID is invalid")
        return _CentralRetention._plans_directory(state_dir) / f"{plan_id}.json"

    @staticmethod
    def _journal_path(state_dir: Path, operation_id: str) -> Path:
        return _CentralRetention._operations_directory(state_dir, create=True) / f"{operation_id}.json"

    def _read_plan(self, path: Path) -> tuple[dict[str, Any], bytes, str]:
        plan, raw = _read_private_json(path)
        if set(plan) != _RETENTION_PLAN_FIELDS or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
            raise CloudError("Central retention plan schema is invalid")
        operation_id = plan.get("operation_id")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise CloudError("Central retention plan identity is invalid")
        if path.name != f"{operation_id}.json" or path.parent.name != "retention-plans":
            raise CloudError("Central retention plan path does not match its identity")
        lease_id = plan.get("repository_lease_id")
        if not isinstance(lease_id, str) or not re.fullmatch(r"[a-f0-9]{64}", lease_id):
            raise CloudError("Central retention plan repository lease identity is invalid")
        _repository_identity(plan.get("target", {}).get("repository_id") if isinstance(plan.get("target"), dict) else None)
        if plan.get("target") != {
            "endpoint_url": self.adapter.endpoint_url,
            "bucket": self.adapter.bucket,
            "prefix": self.adapter.prefix,
            "repository_id": plan.get("target", {}).get("repository_id") if isinstance(plan.get("target"), dict) else None,
        }:
            raise CloudError("Central retention plan target is invalid")
        for name in (
            "policy_sha256",
            "grant_sha256",
            "inventory_sha256",
            "catalog_sha256",
            "protected_sha256",
            "candidate_lock_sha256",
        ):
            if not isinstance(plan.get(name), str) or not _FULL_ID.fullmatch(plan[name]):
                raise CloudError("Central retention plan digest is invalid")
        evidence = plan.get("bucket_evidence_sha256")
        if not isinstance(evidence, dict) or set(evidence) != {
            "versioning_sha256", "lifecycle_sha256", "object_lock_sha256"
        } or any(not isinstance(value, str) or not _FULL_ID.fullmatch(value) for value in evidence.values()):
            raise CloudError("Central retention bucket evidence is invalid")
        for name in ("created_at", "deadline", "grant_expires_at"):
            _parse_time(plan.get(name))
        if type(plan.get("version_count")) is not int or plan["version_count"] < 0:
            raise CloudError("Central retention plan inventory count is invalid")
        if type(plan.get("object_lock_enabled")) is not bool:
            raise CloudError("Central retention plan object-lock state is invalid")
        limits = plan.get("apply_limits")
        if not isinstance(limits, dict) or set(limits) != {"max_apply_objects", "max_apply_bytes"} or any(
            type(value) is not int or value < 1 for value in limits.values()
        ):
            raise CloudError("Central retention plan limits are invalid")
        candidates = plan.get("candidates")
        if not isinstance(candidates, list):
            raise CloudError("Central retention plan candidates are invalid")
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "key", "version_id", "size", "first_observed_noncurrent", "lock_sha256", "object_id"
            }:
                raise CloudError("Central retention plan candidate is invalid")
            _relative_key(candidate.get("key"), self.adapter.prefix)
            if not isinstance(candidate.get("version_id"), str) or not candidate["version_id"]:
                raise CloudError("Central retention plan version ID is invalid")
            if type(candidate.get("size")) is not int or candidate["size"] < 0:
                raise CloudError("Central retention plan candidate size is invalid")
            _parse_time(candidate.get("first_observed_noncurrent"))
            if not isinstance(candidate.get("lock_sha256"), str) or not _FULL_ID.fullmatch(candidate["lock_sha256"]):
                raise CloudError("Central retention plan candidate lock digest is invalid")
            if candidate.get("object_id") != _retention_version_token(candidate["key"], candidate["version_id"]):
                raise CloudError("Central retention plan candidate identity is invalid")
        key_inventory = plan.get("key_inventory")
        if not isinstance(key_inventory, dict):
            raise CloudError("Central retention plan key inventory is invalid")
        for key, items in key_inventory.items():
            _relative_key(key, self.adapter.prefix)
            if not isinstance(items, list):
                raise CloudError("Central retention plan key inventory is invalid")
            for item in items:
                if not isinstance(item, dict) or set(item) != {"version_id", "kind", "size", "is_latest"}:
                    raise CloudError("Central retention plan key inventory is invalid")
                if not isinstance(item["version_id"], str) or not item["version_id"]:
                    raise CloudError("Central retention plan key inventory is invalid")
                if item["kind"] not in {"object", "delete_marker"} or type(item["is_latest"]) is not bool:
                    raise CloudError("Central retention plan key inventory is invalid")
                if item["kind"] == "object" and (type(item["size"]) is not int or item["size"] < 0):
                    raise CloudError("Central retention plan key inventory is invalid")
                if item["kind"] == "delete_marker" and item["size"] is not None:
                    raise CloudError("Central retention plan key inventory is invalid")
        return plan, raw, _retention_digest(raw)

    @staticmethod
    def _operation_ledger(ledger: Any | None) -> Any:
        if ledger is None or not callable(getattr(ledger, "begin", None)) or not callable(
            getattr(ledger, "reconcile", None)
        ):
            raise CloudError("An existing repository operation ledger is required")
        return ledger

    def _list_key_versions(self, key: str) -> list[dict[str, Any]]:
        _relative_key(key, self.adapter.prefix)
        versions = []
        key_marker = None
        version_marker = None
        seen_pages = set()
        seen_versions = set()
        while True:
            marker = (key_marker or "", version_marker or "")
            if marker in seen_pages:
                raise CloudError("Exact-key version pagination repeated a marker")
            seen_pages.add(marker)
            request = {"Bucket": self.adapter.bucket, "Prefix": key, "MaxKeys": self.adapter.page_size}
            if key_marker is not None:
                request["KeyMarker"] = key_marker
            if version_marker is not None:
                request["VersionIdMarker"] = version_marker
            try:
                response = self.adapter.client.list_object_versions(**request)
            except Exception:
                raise CloudError("Could not recheck the exact object version inventory") from None
            if not isinstance(response, dict):
                raise CloudError("Exact-key version inventory is malformed")
            for collection, kind in (("Versions", "object"), ("DeleteMarkers", "delete_marker")):
                items = response.get(collection, [])
                if not isinstance(items, list):
                    raise CloudError("Exact-key version inventory is malformed")
                for item in items:
                    if not isinstance(item, dict) or type(item.get("IsLatest")) is not bool:
                        raise CloudError("Exact-key version inventory is malformed")
                    item_key = item.get("Key")
                    _relative_key(item_key, self.adapter.prefix)
                    version_id = item.get("VersionId")
                    if not isinstance(version_id, str) or not version_id:
                        raise CloudError("Exact-key version ID is missing")
                    if item_key != key:
                        continue
                    token = (item_key, version_id)
                    if token in seen_versions:
                        raise CloudError("Exact-key version inventory contains duplicates")
                    seen_versions.add(token)
                    size = item.get("Size") if kind == "object" else None
                    if kind == "object" and (type(size) is not int or size < 0):
                        raise CloudError("Exact-key object version size is invalid")
                    versions.append({
                        "version_id": version_id,
                        "kind": kind,
                        "size": size,
                        "is_latest": item["IsLatest"],
                    })
            truncated = response.get("IsTruncated", False)
            if type(truncated) is not bool:
                raise CloudError("Exact-key version pagination state is invalid")
            if not truncated:
                break
            next_key = response.get("NextKeyMarker")
            next_version = response.get("NextVersionIdMarker")
            if not isinstance(next_key, str) or not next_key:
                raise CloudError("Truncated exact-key inventory lacks a key marker")
            if next_version is not None and not isinstance(next_version, str):
                raise CloudError("Exact-key version marker is invalid")
            next_marker = (next_key, next_version or "")
            if next_marker == marker:
                raise CloudError("Exact-key version pagination did not progress")
            key_marker, version_marker = next_key, next_version
        if versions and sum(item["is_latest"] for item in versions) != 1:
            raise CloudError("Exact-key inventory has an invalid latest-version set")
        return sorted(versions, key=lambda item: item["version_id"])

    def _single_attempt_mutation_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise CloudError("boto3 is required for central retention deletion") from None
        options = {
            "endpoint_url": self.adapter.endpoint_url,
            "config": Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"total_max_attempts": 1, "mode": "standard"},
                signature_version="s3v4",
            ),
        }
        if self.adapter.region_name is not None:
            options["region_name"] = self.adapter.region_name
        try:
            return boto3.client("s3", **options)
        except Exception:
            raise CloudError("Could not create a single-attempt retention client") from None

    @staticmethod
    def _require_single_attempt_client(client: Any) -> None:
        meta = getattr(client, "meta", None)
        if meta is None:
            return
        config = getattr(meta, "config", None)
        retries = getattr(config, "retries", None)
        if not isinstance(retries, dict) or retries.get("total_max_attempts") != 1:
            raise CloudError("Retention mutation clients must be configured for one attempt")

    @staticmethod
    def _journal_value(plan: Mapping[str, Any], plan_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_id": plan["operation_id"],
            "plan_sha256": plan_sha256,
            "status": "prepared",
            "updated_at": _iso(datetime.now(timezone.utc)),
            "items": [
                {"object_id": item["object_id"], "status": "pending"}
                for item in plan["candidates"]
            ],
        }

    @staticmethod
    def _save_journal(path: Path, journal: dict[str, Any]) -> None:
        journal["updated_at"] = _iso(datetime.now(timezone.utc))
        _write_private_json(path, journal, replace=os.path.lexists(path))

    def plan(
        self,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        *,
        quiescent_window_confirmed: bool,
        repository_lease_id: str,
    ) -> dict[str, Any]:
        if quiescent_window_confirmed is not True:
            raise CloudError("Central retention requires an explicitly coordinated quiescent window")
        if not isinstance(repository_lease_id, str) or not re.fullmatch(r"[a-f0-9]{64}", repository_lease_id):
            raise CloudError("Central retention repository lease identity is invalid")
        authority = self._authority(policy_path, grant_path)
        policy, grant, state_dir, now, policy_digest, grant_digest = authority[:6]
        with _retention_catalog_lock(state_dir):
            authority = self._authority(policy_path, grant_path)
            policy, grant, state_dir, now, policy_digest, grant_digest = authority[:6]
            with tempfile.TemporaryDirectory(prefix="retention-plan-", dir=state_dir) as temporary_name:
                temporary = Path(temporary_name)
                os.chmod(temporary, 0o700)
                inputs = self._plan_inputs(
                    policy,
                    state_dir,
                    recovery_record_dirs,
                    now=now,
                    temporary=temporary,
                    update_catalog=True,
                )
                candidates = inputs["candidates"]
                total_bytes = sum(item["size"] for item in candidates)
                limits = {
                    "max_apply_objects": min(policy["max_apply_objects"], grant["max_apply_objects"]),
                    "max_apply_bytes": min(policy["max_apply_bytes"], grant["max_apply_bytes"]),
                }
                if len(candidates) > limits["max_apply_objects"] or total_bytes > limits["max_apply_bytes"]:
                    raise CloudError("Central retention candidates exceed the authorized apply bounds")
                operation_id = uuid.uuid4().hex
                created = datetime.now(timezone.utc)
                deadline = min(
                    created + timedelta(seconds=policy["plan_max_age_seconds"]),
                    _parse_time(grant["expires_at"]),
                )
                plan = {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "created_at": _iso(created),
                    "deadline": _iso(deadline),
                    "target": _retention_target(policy),
                    "repository_lease_id": repository_lease_id,
                    "policy_id": policy["policy_id"],
                    "policy_revision": policy["revision"],
                    "policy_sha256": policy_digest,
                    "grant_sha256": grant_digest,
                    "grant_expires_at": _iso(_parse_time(grant["expires_at"])),
                    "apply_limits": limits,
                    "inventory_sha256": inputs["inventory_sha256"],
                    "version_count": inputs["version_count"],
                    "catalog_sha256": inputs["catalog_sha256"],
                    "accepted_manifest_digests": inputs["accepted_manifest_digests"],
                    "selected_manifest_digests": inputs["selected_manifest_digests"],
                    "protected_sha256": inputs["protected_sha256"],
                    "protected_version_count": inputs["protected_version_count"],
                    "candidate_lock_sha256": inputs["candidate_lock_sha256"],
                    "held_count": inputs["held_count"],
                    "bucket_evidence_sha256": inputs["evidence"]["digests"],
                    "object_lock_enabled": inputs["evidence"]["object_lock_enabled"],
                    "candidates": candidates,
                    "key_inventory": inputs["key_inventory"],
                }
                plan_path = self._plans_directory(state_dir, create=True) / f"{operation_id}.json"
                raw = _write_immutable_plan(plan_path, plan)
        return {
            "status": "planned",
            "operation_id": operation_id,
            "plan_sha256": _retention_digest(raw),
            "candidate_count": len(candidates),
            "candidate_bytes": total_bytes,
            "protected_version_count": inputs["protected_version_count"],
            "held_version_count": inputs["held_count"],
            "inventory_version_count": inputs["version_count"],
            "deadline": plan["deadline"],
        }

    def apply(
        self,
        plan_id: str,
        expected_plan_sha256: str,
        policy_path: str | os.PathLike[str],
        grant_path: str | os.PathLike[str],
        recovery_record_dirs: Sequence[str | os.PathLike[str]],
        *,
        quiescent_window_confirmed: bool,
        ledger: Any,
        cancel_event: Event | None = None,
        mutation_client: Any | None = None,
        expected_repository_lease_id: str | None = None,
    ) -> dict[str, Any]:
        if quiescent_window_confirmed is not True:
            raise CloudError("Central retention requires an explicitly coordinated quiescent window")
        if not isinstance(expected_plan_sha256, str) or not _FULL_ID.fullmatch(expected_plan_sha256):
            raise CloudError("Central retention expected plan digest is invalid")
        authority = self._authority(policy_path, grant_path)
        policy, grant, state_dir, _now, policy_digest, grant_digest = authority[:6]
        with _retention_catalog_lock(state_dir):
            policy, grant, state_dir, now, policy_digest, grant_digest = self._authority(
                policy_path, grant_path
            )[:6]
            plan_path = self._plan_path(state_dir, plan_id)
            plan, plan_raw, plan_digest = self._read_plan(plan_path)
            if (
                expected_repository_lease_id is not None
                and plan["repository_lease_id"] != expected_repository_lease_id
            ):
                raise CloudError("Central retention plan belongs to a different repository lease")
            if plan_digest != expected_plan_sha256:
                raise CloudError("Central retention plan digest differs from the reviewed plan")
            if plan["operation_id"] != plan_id or plan["target"] != _retention_target(policy):
                raise CloudError("Central retention plan does not match the current target")
            if plan["policy_id"] != policy["policy_id"] or plan["policy_revision"] != policy["revision"]:
                raise CloudError("Central retention policy differs from the reviewed plan")
            if policy_digest != plan["policy_sha256"] or grant_digest != plan["grant_sha256"]:
                raise CloudError("Central retention authority changed after planning")
            if now >= _parse_time(plan["deadline"]):
                raise CloudError("Central retention plan has expired")
            if now >= _parse_time(plan["grant_expires_at"]):
                raise CloudError("Central retention grant has expired")
            if len(plan["candidates"]) > plan["apply_limits"]["max_apply_objects"] or sum(
                item["size"] for item in plan["candidates"]
            ) > plan["apply_limits"]["max_apply_bytes"]:
                raise CloudError("Central retention plan exceeds its authorized apply bounds")
            journal_path = self._journal_path(state_dir, plan_id)
            if os.path.lexists(journal_path):
                raise CloudError("Central retention plan was already applied or has an unresolved attempt")
            from .operations import OperationPlan, OperationStatus

            active_ledger = self._operation_ledger(ledger)
            operation_plan = OperationPlan(
                operation_id=plan_id,
                repository_id=plan["repository_lease_id"],
                label="central-retention",
                argv=("delete_noncurrent_versions",),
                destructive=True,
            )
            lease = active_ledger.begin(operation_plan)
            try:
                with tempfile.TemporaryDirectory(prefix="retention-apply-", dir=state_dir) as temporary_name:
                    temporary = Path(temporary_name)
                    os.chmod(temporary, 0o700)
                    current_inputs = self._plan_inputs(
                        policy,
                        state_dir,
                        recovery_record_dirs,
                        now=now,
                        temporary=temporary,
                        update_catalog=False,
                    )
                    comparisons = (
                        ("inventory_sha256", current_inputs["inventory_sha256"]),
                        ("version_count", current_inputs["version_count"]),
                        ("catalog_sha256", current_inputs["catalog_sha256"]),
                        ("accepted_manifest_digests", current_inputs["accepted_manifest_digests"]),
                        ("selected_manifest_digests", current_inputs["selected_manifest_digests"]),
                        ("protected_sha256", current_inputs["protected_sha256"]),
                        ("protected_version_count", current_inputs["protected_version_count"]),
                        ("candidate_lock_sha256", current_inputs["candidate_lock_sha256"]),
                        ("candidates", current_inputs["candidates"]),
                        ("key_inventory", current_inputs["key_inventory"]),
                        ("bucket_evidence_sha256", current_inputs["evidence"]["digests"]),
                        ("object_lock_enabled", current_inputs["evidence"]["object_lock_enabled"]),
                    )
                    if any(plan[name] != value for name, value in comparisons):
                        raise CloudError("Central retention inputs drifted after planning")

                self._assert_live_authority(plan, policy_path, grant_path)
                mutation = mutation_client if mutation_client is not None else self._single_attempt_mutation_client()
                self._require_single_attempt_client(mutation)
                journal_path = self._journal_path(state_dir, plan_id)
                if os.path.lexists(journal_path):
                    raise CloudError("Central retention plan was already applied or has an unresolved attempt")
                journal = self._journal_value(plan, plan_digest)
                self._save_journal(journal_path, journal)
            except BaseException:
                try:
                    lease.finish(OperationStatus.FAILED, 2)
                finally:
                    lease.close()
                raise
            expected_by_key = {key: [dict(item) for item in items] for key, items in plan["key_inventory"].items()}
            deleted_count = 0
            deleted_bytes = 0

            def cancelled_at(index: int, *, after_intent: bool = False) -> dict[str, Any] | None:
                if cancel_event is None or not cancel_event.is_set():
                    return None
                if after_intent:
                    journal["items"][index]["status"] = "pending"
                journal["status"] = "cancelled"
                self._save_journal(journal_path, journal)
                lease.finish(OperationStatus.CANCELLED, 130)
                return {
                    "status": "cancelled",
                    "operation_id": plan_id,
                    "deleted_count": deleted_count,
                    "deleted_bytes": deleted_bytes,
                    "remaining_count": len(plan["candidates"]) - deleted_count,
                }

            try:
                for index, candidate in enumerate(plan["candidates"]):
                    cancelled = cancelled_at(index)
                    if cancelled is not None:
                        return cancelled
                    try:
                        self._assert_live_authority(plan, policy_path, grant_path)
                    except CloudError:
                        journal["status"] = "stopped"
                        self._save_journal(journal_path, journal)
                        lease.finish(OperationStatus.FAILED, 2)
                        return {
                            "status": "stopped",
                            "operation_id": plan_id,
                            "deleted_count": deleted_count,
                            "deleted_bytes": deleted_bytes,
                            "remaining_count": len(plan["candidates"]) - deleted_count,
                        }
                    expected_versions = expected_by_key.get(candidate["key"], [])
                    try:
                        current_versions = self._list_key_versions(candidate["key"])
                        candidate_record = next(
                            item for item in expected_versions if item["version_id"] == candidate["version_id"]
                        )
                    except (CloudError, StopIteration):
                        current_versions = []
                        candidate_record = None
                    if (
                        current_versions != expected_versions
                        or candidate_record is None
                        or candidate_record["kind"] != "object"
                        or candidate_record["is_latest"]
                    ):
                        journal["status"] = "stopped"
                        self._save_journal(journal_path, journal)
                        lease.finish(OperationStatus.FAILED, 2)
                        return {
                            "status": "stopped",
                            "operation_id": plan_id,
                            "deleted_count": deleted_count,
                            "deleted_bytes": deleted_bytes,
                            "remaining_count": len(plan["candidates"]) - deleted_count,
                        }
                    try:
                        lock, held = self._version_lock(
                            candidate["key"],
                            candidate["version_id"],
                            lock_enabled=plan["object_lock_enabled"],
                            now=datetime.now(timezone.utc),
                        )
                    except CloudError:
                        lock, held = {}, True
                    try:
                        self._assert_live_authority(plan, policy_path, grant_path)
                    except CloudError:
                        held = True
                    if held or _retention_digest(_retention_json_bytes(lock)) != candidate["lock_sha256"]:
                        journal["status"] = "stopped"
                        self._save_journal(journal_path, journal)
                        lease.finish(OperationStatus.FAILED, 2)
                        return {
                            "status": "stopped",
                            "operation_id": plan_id,
                            "deleted_count": deleted_count,
                            "deleted_bytes": deleted_bytes,
                            "remaining_count": len(plan["candidates"]) - deleted_count,
                        }
                    cancelled = cancelled_at(index)
                    if cancelled is not None:
                        return cancelled
                    journal["items"][index]["status"] = "intent"
                    self._save_journal(journal_path, journal)
                    try:
                        self._assert_live_authority(plan, policy_path, grant_path)
                    except CloudError:
                        journal["items"][index]["status"] = "pending"
                        journal["status"] = "stopped"
                        self._save_journal(journal_path, journal)
                        lease.finish(OperationStatus.FAILED, 2)
                        return {
                            "status": "stopped",
                            "operation_id": plan_id,
                            "deleted_count": deleted_count,
                            "deleted_bytes": deleted_bytes,
                            "remaining_count": len(plan["candidates"]) - deleted_count,
                        }
                    cancelled = cancelled_at(index, after_intent=True)
                    if cancelled is not None:
                        return cancelled
                    try:
                        response = mutation.delete_object(
                            Bucket=self.adapter.bucket,
                            Key=candidate["key"],
                            VersionId=candidate["version_id"],
                        )
                        if not isinstance(response, dict) or response.get("VersionId") != candidate["version_id"]:
                            raise CloudError("Retention delete response did not confirm the exact version")
                        remaining_versions = self._list_key_versions(candidate["key"])
                        if any(item["version_id"] == candidate["version_id"] for item in remaining_versions):
                            raise CloudError("Retention delete response could not be verified")
                    except Exception:
                        journal["items"][index]["status"] = "uncertain"
                        journal["status"] = "uncertain"
                        self._save_journal(journal_path, journal)
                        return {
                            "status": "uncertain",
                            "operation_id": plan_id,
                            "deleted_count": deleted_count,
                            "deleted_bytes": deleted_bytes,
                            "uncertain_count": 1,
                            "remaining_count": len(plan["candidates"]) - deleted_count,
                        }
                    journal["items"][index]["status"] = "confirmed"
                    self._save_journal(journal_path, journal)
                    expected_by_key[candidate["key"]] = [
                        item for item in expected_versions if item["version_id"] != candidate["version_id"]
                    ]
                    deleted_count += 1
                    deleted_bytes += candidate["size"]
                journal["status"] = "complete"
                self._save_journal(journal_path, journal)
                lease.finish(OperationStatus.SUCCEEDED, 0)
                return {
                    "status": "complete",
                    "operation_id": plan_id,
                    "deleted_count": deleted_count,
                    "deleted_bytes": deleted_bytes,
                    "remaining_count": 0,
                }
            finally:
                lease.close()

    def reconcile(
        self,
        plan_path: str | os.PathLike[str],
        *,
        ledger: Any,
        expected_repository_lease_id: str | None = None,
    ) -> dict[str, Any]:
        path = Path(plan_path).absolute()
        _ensure_private_directory(path.parent)
        plan, _raw, _digest = self._read_plan(path)
        state_dir = _ensure_private_directory(path.parent.parent)
        if path.parent != self._plans_directory(state_dir):
            raise CloudError("Central retention plan is outside its private state directory")
        with _retention_catalog_lock(state_dir):
            return self._reconcile_locked(
                path, ledger=ledger,
                expected_repository_lease_id=expected_repository_lease_id,
            )

    def _reconcile_locked(
        self,
        path: Path,
        *,
        ledger: Any,
        expected_repository_lease_id: str | None = None,
    ) -> dict[str, Any]:
        plan, _plan_raw, plan_digest = self._read_plan(path)
        if (
            expected_repository_lease_id is not None
            and plan["repository_lease_id"] != expected_repository_lease_id
        ):
            raise CloudError("Central retention plan belongs to a different repository lease")
        state_dir = _ensure_private_directory(path.parent.parent)
        journal_path = self._journal_path(state_dir, plan["operation_id"])
        journal, _journal_raw = _read_private_json(journal_path)
        if set(journal) != _RETENTION_JOURNAL_FIELDS or journal.get("schema_version") != 1:
            raise CloudError("Central retention operation journal schema is invalid")
        if journal.get("operation_id") != plan["operation_id"] or journal.get("plan_sha256") != plan_digest:
            raise CloudError("Central retention operation journal does not match its plan")
        if not isinstance(journal.get("items"), list) or len(journal["items"]) != len(plan["candidates"]):
            raise CloudError("Central retention operation journal items are invalid")
        observed = []
        for candidate, item in zip(plan["candidates"], journal["items"], strict=True):
            if not isinstance(item, dict) or set(item) != {"object_id", "status"} or item["object_id"] != candidate["object_id"]:
                raise CloudError("Central retention operation journal items are invalid")
            try:
                versions = self._list_key_versions(candidate["key"])
            except CloudError:
                observed.append("unknown")
                continue
            present = any(version["version_id"] == candidate["version_id"] for version in versions)
            observed.append("present" if present else "absent")
        counts = {status: observed.count(status) for status in ("absent", "present", "unknown")}
        journal["items"] = [
            {"object_id": item["object_id"], "status": status}
            for item, status in zip(journal["items"], observed, strict=True)
        ]
        if counts["unknown"]:
            journal["status"] = "uncertain"
            outcome = None
            public_status = "uncertain"
        elif counts["present"]:
            journal["status"] = "reconciled_abandoned"
            from .operations import ReconciliationOutcome

            outcome = ReconciliationOutcome.ABANDONED
            public_status = "reconciled_abandoned"
        else:
            journal["status"] = "reconciled_succeeded"
            from .operations import ReconciliationOutcome

            outcome = ReconciliationOutcome.SUCCEEDED
            public_status = "reconciled_succeeded"
        self._save_journal(journal_path, journal)
        if outcome is not None:
            try:
                active_ledger = self._operation_ledger(ledger)
                active_ledger.reconcile(
                    plan["repository_lease_id"],
                    plan["operation_id"],
                    outcome,
                )
            except (ValueError, KeyError):
                pass
        return {
            "status": public_status,
            "operation_id": plan["operation_id"],
            "absent_count": counts["absent"],
            "present_count": counts["present"],
            "unknown_count": counts["unknown"],
        }


__all__ = [
    "CheckpointRecord",
    "CloudError",
    "LifecycleInspection",
    "RestoreRecord",
    "S3CloudAdapter",
]
