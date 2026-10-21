"""Machine-bound Linux coverage inventory and exact reviewed scope.

Private declarations and candidates stay beneath the existing state root. This
module inventories names and metadata; it never opens protected file contents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from .enrollment import (coverage_evidence_bytes, evidence_bytes, policy_lease,
                         verify_bytes)
from .models import ConfigurationError, strict_json


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\$?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_METADATA = {"numeric_owner", "mode", "acl", "xattr", "links"}
_MAX_INVENTORY = 16 * 1024 * 1024
_MAX_ENTRIES = 100_000


class CoverageError(RuntimeError):
    """Sanitized coverage failure; private paths never appear in messages."""


@dataclass(frozen=True)
class InventoryPaths:
    passwd: Path = Path("/etc/passwd")
    group: Path = Path("/etc/group")
    mountinfo: Path = Path("/proc/self/mountinfo")
    packages: Path = Path("/var/lib/dpkg/status")
    application_units: Path = Path("/etc/systemd/system")


@dataclass(frozen=True)
class CoverageCheck:
    coverage_status: str
    reasons: tuple[str, ...]
    baseline_digest: str | None
    candidate_digest: str | None
    counts: dict[str, int]
    approved_roots: tuple[tuple[str, Path, str, str], ...] = field(default=(), repr=False)
    approved_exclusions: tuple[Path, ...] = field(default=(), repr=False)
    recovery_status: str = "untested"

    @property
    def capture_ready(self) -> bool:
        return self.coverage_status == "complete" and bool(self.approved_roots)

    def public_result(self) -> dict[str, object]:
        return {"coverage_status": self.coverage_status, "recovery_status": self.recovery_status,
                "capture_ready": self.capture_ready, "reasons": list(self.reasons),
                "baseline_digest": self.baseline_digest, "candidate_digest": self.candidate_digest,
                "counts": self.counts,
                "next_action": ("Capture only the reviewed source mappings" if self.capture_ready
                                else "Review current coverage discovery and exact exclusions")}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value: Any, description: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise CoverageError(f"Invalid {description} identifier")
    return value


def _account_name(value: Any) -> str:
    if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
        raise CoverageError("Invalid Linux account name")
    return value


def _sha(value: Any, description: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise CoverageError(f"Invalid {description} digest")
    return value


def _absolute(value: Any, description: str) -> Path:
    if (not isinstance(value, str) or not value.startswith("/") or "\0" in value
            or "\n" in value or "\r" in value or ".." in Path(value).parts):
        raise CoverageError(f"Invalid private {description} path")
    path = Path(value)
    if str(path) != value:
        raise CoverageError(f"Invalid private {description} path")
    return path


def validate_scope_policy(raw: Any) -> dict[str, Any]:
    """Validate the portable schema-1 policy before private path resolution."""
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "approved_roots", "require_privileged",
        "filesystem_boundaries", "metadata_expectations",
    }:
        raise CoverageError("Invalid portable coverage policy schema")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise CoverageError("Unsupported portable coverage policy version")
    roots = raw["approved_roots"]
    if not isinstance(roots, list) or not roots or len(roots) > 64:
        raise CoverageError("Portable coverage roots must be a bounded nonempty list")
    roots = [_identifier(root, "root") for root in roots]
    if len(set(roots)) != len(roots):
        raise CoverageError("Duplicate portable coverage root")
    if type(raw["require_privileged"]) is not bool or raw["filesystem_boundaries"] != "explicit":
        raise CoverageError("Coverage privilege and filesystem boundary policy must be explicit")
    expectations = raw["metadata_expectations"]
    if (not isinstance(expectations, dict) or set(expectations) != _METADATA
            or any(expectations[key] is not True for key in _METADATA)):
        raise CoverageError("Full numeric ownership, mode, ACL, xattr and link metadata are required")
    return {"schema_version": 1, "approved_roots": roots,
            "require_privileged": raw["require_privileged"],
            "filesystem_boundaries": "explicit",
            "metadata_expectations": {key: True for key in sorted(_METADATA)}}


def _read_private(path: Path, limit: int = 1024 * 1024) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise CoverageError("Coverage input must be an owned private regular file")
            content = stream.read(limit + 1)
    except OSError:
        raise CoverageError("Private coverage input is unavailable") from None
    if len(content) > limit:
        raise CoverageError("Private coverage input exceeds its size limit")
    return content


def _recipe(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"reason", "consequence", "recipe_id", "recipe_sha256"}:
        raise CoverageError("Coverage reconstruction recipe is incomplete")
    reason, consequence = raw["reason"], raw["consequence"]
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256
           or any(char in value for char in "\0\r\n") for value in (reason, consequence)):
        raise CoverageError("Coverage exclusion must explain reason and consequence")
    return {"reason": reason, "consequence": consequence,
            "recipe_id": _identifier(raw["recipe_id"], "recipe"),
            "recipe_sha256": _sha(raw["recipe_sha256"], "recipe")}


def _declaration(raw: Any, scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "roots", "exclusions", "mount_decisions",
        "identity_decisions", "applications", "package_reconstruction", "reconstruction_evidence",
    } or type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise CoverageError("Invalid private coverage declaration schema")
    roots = []
    if not isinstance(raw["roots"], list) or len(raw["roots"]) != len(scope["approved_roots"]):
        raise CoverageError("Private coverage roots must match portable scope")
    for item in raw["roots"]:
        if not isinstance(item, dict) or set(item) != {"id", "path", "source_id", "adapter"}:
            raise CoverageError("Invalid private coverage root")
        root = {"id": _identifier(item["id"], "root"),
                "path": str(_absolute(item["path"], "root")),
                "source_id": _identifier(item["source_id"], "source"),
                "adapter": item["adapter"]}
        if root["adapter"] not in ("files", "sqlite", "postgresql", "mysql", "mariadb"):
            raise CoverageError("Unsupported declared capture adapter")
        roots.append(root)
    if {root["id"] for root in roots} != set(scope["approved_roots"]):
        raise CoverageError("Private coverage roots differ from portable scope")
    if len({root["path"] for root in roots}) != len(roots):
        raise CoverageError("Duplicate private coverage path")
    exclusions = []
    if not isinstance(raw["exclusions"], list) or len(raw["exclusions"]) > 256:
        raise CoverageError("Invalid coverage exclusions")
    for item in raw["exclusions"]:
        if not isinstance(item, dict) or set(item) != {"root_id", "path", "recipe"}:
            raise CoverageError("Invalid exact coverage exclusion")
        root_id = _identifier(item["root_id"], "root")
        root_path = next((Path(root["path"]) for root in roots if root["id"] == root_id), None)
        path = _absolute(item["path"], "exclusion")
        if root_path is None or path == root_path or root_path not in path.parents:
            raise CoverageError("Exact exclusion must be inside its declared root")
        exclusions.append({"root_id": root_id, "path": str(path), "recipe": _recipe(item["recipe"])})
    if len({item["path"] for item in exclusions}) != len(exclusions):
        raise CoverageError("Duplicate exact coverage exclusion")
    mounts = []
    if not isinstance(raw["mount_decisions"], list) or len(raw["mount_decisions"]) > 512:
        raise CoverageError("Invalid mount decisions")
    for item in raw["mount_decisions"]:
        if not isinstance(item, dict) or set(item) != {"mount_id", "mountpoint", "decision"}:
            raise CoverageError("Invalid exact mount decision")
        if type(item["mount_id"]) is not int or item["mount_id"] <= 0 or item["decision"] not in ("include", "exclude"):
            raise CoverageError("Invalid exact mount decision")
        mounts.append({"mount_id": item["mount_id"],
                       "mountpoint": str(_absolute(item["mountpoint"], "mountpoint")),
                       "decision": item["decision"]})
    if len({item["mountpoint"] for item in mounts}) != len(mounts):
        raise CoverageError("Duplicate mount decision")
    identities = []
    if not isinstance(raw["identity_decisions"], list) or len(raw["identity_decisions"]) > 4096:
        raise CoverageError("Invalid identity reconstruction decisions")
    for item in raw["identity_decisions"]:
        if not isinstance(item, dict) or set(item) != {"name", "home", "recipe"}:
            raise CoverageError("Invalid identity reconstruction decision")
        identities.append({"name": _account_name(item["name"]),
                           "home": str(_absolute(item["home"], "home")),
                           "recipe": _recipe(item["recipe"])})
    if len({item["name"] for item in identities}) != len(identities):
        raise CoverageError("Duplicate identity decision")
    applications = []
    if not isinstance(raw["applications"], list) or len(raw["applications"]) > 512:
        raise CoverageError("Invalid application declarations")
    for item in raw["applications"]:
        if not isinstance(item, dict) or set(item) != {"id", "unit", "source_id", "state_roots", "startup_recipe"}:
            raise CoverageError("Invalid application declaration")
        if not isinstance(item["state_roots"], list) or len(item["state_roots"]) > 64:
            raise CoverageError("Invalid application state roots")
        applications.append({"id": _identifier(item["id"], "application"),
                             "unit": _identifier(item["unit"], "application unit") + ".service",
                             "source_id": _identifier(item["source_id"], "source"),
                             "state_roots": [str(_absolute(path, "application state")) for path in item["state_roots"]],
                             "startup_recipe": _recipe(item["startup_recipe"])})
    if len({item["id"] for item in applications}) != len(applications):
        raise CoverageError("Duplicate application declaration")
    package_recipe = raw["package_reconstruction"]
    if package_recipe is not None:
        package_recipe = _recipe(package_recipe)
    evidence = []
    if not isinstance(raw["reconstruction_evidence"], list) or len(raw["reconstruction_evidence"]) > 512:
        raise CoverageError("Invalid reconstruction evidence references")
    for item in raw["reconstruction_evidence"]:
        if not isinstance(item, dict) or set(item) != {
                "recipe_id", "recipe_sha256", "procedure_path", "evidence_path", "evidence_sha256"}:
            raise CoverageError("Invalid reconstruction evidence reference")
        evidence.append({"recipe_id": _identifier(item["recipe_id"], "recipe"),
                         "recipe_sha256": _sha(item["recipe_sha256"], "recipe"),
                         "procedure_path": str(_absolute(item["procedure_path"], "procedure")),
                         "evidence_path": str(_absolute(item["evidence_path"], "evidence")),
                         "evidence_sha256": _sha(item["evidence_sha256"], "evidence")})
    if len({(item["recipe_id"], item["recipe_sha256"]) for item in evidence}) != len(evidence):
        raise CoverageError("Duplicate reconstruction evidence reference")
    return {"schema_version": 1, "roots": roots, "exclusions": exclusions,
            "mount_decisions": mounts, "identity_decisions": identities,
            "applications": applications, "package_reconstruction": package_recipe,
            "reconstruction_evidence": evidence}


def _bounded_read(path: Path, limit: int = _MAX_INVENTORY) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CoverageError("Linux inventory source is not regular")
            content = stream.read(limit + 1)
    except OSError:
        raise CoverageError("Linux inventory source is unavailable") from None
    if len(content) > limit:
        raise CoverageError("Linux inventory exceeds its size limit")
    return content


def _mount_unescape(value: str) -> str:
    def replace(match):
        return chr(int(match.group(1), 8))
    return re.sub(r"\\([0-7]{3})", replace, value)


def _mount_inventory(raw: bytes) -> list[dict[str, Any]]:
    mounts = []
    try:
        for line in raw.decode("utf-8").splitlines():
            before, after = line.split(" - ", 1)
            fields, tail = before.split(), after.split()
            if len(fields) < 6 or len(tail) < 3:
                raise ValueError()
            mount_id, parent_id = int(fields[0]), int(fields[1])
            if mount_id <= 0 or parent_id <= 0:
                raise ValueError()
            mounts.append({"mount_id": mount_id, "parent_id": parent_id,
                           "root": _mount_unescape(fields[3]),
                           "mountpoint": _mount_unescape(fields[4]),
                           "filesystem": tail[0], "source_digest": hashlib.sha256(tail[1].encode()).hexdigest()})
    except (UnicodeError, ValueError):
        raise CoverageError("Linux mount inventory is malformed") from None
    if len(mounts) > 8192 or len({item["mount_id"] for item in mounts}) != len(mounts):
        raise CoverageError("Linux mount inventory is ambiguous")
    return sorted(mounts, key=lambda item: item["mount_id"])


def _accounts(raw: bytes, *, passwd: bool) -> list[dict[str, Any]]:
    accounts = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split(":")
            if len(fields) != (7 if passwd else 4):
                raise ValueError()
            name = _account_name(fields[0])
            uid = int(fields[2])
            if uid < 0:
                raise ValueError()
            account = {"name": name, "id": uid}
            if passwd:
                account["gid"] = int(fields[3])
                account["home"] = fields[5]
                account["shell"] = fields[6]
            else:
                account["members"] = fields[3].split(",") if fields[3] else []
            accounts.append(account)
    except (UnicodeError, ValueError, CoverageError):
        raise CoverageError("Linux account inventory is malformed") from None
    if len(accounts) > 65536 or len({item["name"] for item in accounts}) != len(accounts):
        raise CoverageError("Linux account inventory is ambiguous")
    return sorted(accounts, key=lambda item: item["name"])


def _application_units(directory: Path) -> list[dict[str, str]]:
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise CoverageError("Application unit inventory is not a directory")
    except OSError:
        raise CoverageError("Application unit inventory is unavailable") from None
    stack = [(directory, 0)]
    units = []
    visited = 0
    while stack:
        path, depth = stack.pop()
        if depth > 16 or visited > 8192:
            raise CoverageError("Application unit inventory exceeds its size limit")
        try:
            with os.scandir(path) as entries:
                children = sorted((Path(entry.path) for entry in entries), reverse=True)
        except OSError:
            raise CoverageError("Application unit inventory is unavailable") from None
        for child in children:
            visited += 1
            if visited > 8192:
                raise CoverageError("Application unit inventory exceeds its size limit")
            try:
                info = child.lstat()
            except OSError:
                raise CoverageError("Application unit inventory is unavailable") from None
            if stat.S_ISDIR(info.st_mode):
                stack.append((child, depth + 1))
            elif child.name.endswith(".service"):
                if stat.S_ISLNK(info.st_mode):
                    try:
                        target = os.readlink(child)
                    except OSError:
                        raise CoverageError("Application unit link is unavailable") from None
                    kind = "link:" + hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
                elif stat.S_ISREG(info.st_mode):
                    kind = "regular"
                else:
                    raise CoverageError("Application unit type is unsupported")
                units.append({"name": child.name, "relative_path": str(child.relative_to(directory)),
                              "kind": kind})
    return sorted(units, key=lambda item: item["relative_path"])


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _has_symlink_ancestor(path: Path) -> bool:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def _scope_location(path: Path, roots: dict[str, Path], exclusions: set[Path]) -> str:
    """Classify a logical state path by the bytes that capture will actually see."""
    if any(_within(path, exclusion) for exclusion in exclusions):
        return "excluded"
    if not any(_within(path, root) for root in roots.values()):
        return "external"
    if _has_symlink_ancestor(path):
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return "linked"
        if any(_within(target, exclusion) for exclusion in exclusions):
            return "linked"
        if not any(_within(target, root) for root in roots.values()):
            return "linked"
    return "captured"


def restic_literal_exclusion(path: Path) -> str:
    """Escape Restic glob metacharacters in one reviewed absolute path."""
    if not path.is_absolute():
        raise CoverageError("Coverage exclusion must be absolute")
    return "".join("\\" + char if char in "*?[]\\" else char for char in str(path))


def _evidence_reasons(declaration: dict[str, Any]) -> set[str]:
    required = [item["recipe"] for item in declaration["exclusions"]]
    required.extend(item["recipe"] for item in declaration["identity_decisions"])
    required.extend(item["startup_recipe"] for item in declaration["applications"])
    if declaration["package_reconstruction"] is not None:
        required.append(declaration["package_reconstruction"])
    references = {(item["recipe_id"], item["recipe_sha256"]): item
                  for item in declaration["reconstruction_evidence"]}
    reasons = set()
    for recipe in required:
        key = (recipe["recipe_id"], recipe["recipe_sha256"])
        reference = references.get(key)
        if reference is None:
            reasons.add("reconstruction_evidence_missing")
            continue
        try:
            procedure = _read_private(Path(reference["procedure_path"]))
            evidence_bytes = _read_private(Path(reference["evidence_path"]))
            record = strict_json(evidence_bytes.decode("utf-8"))
        except (CoverageError, ConfigurationError, UnicodeError):
            reasons.add("reconstruction_evidence_unverified")
            continue
        if (hashlib.sha256(procedure).hexdigest() != recipe["recipe_sha256"]
                or hashlib.sha256(evidence_bytes).hexdigest() != reference["evidence_sha256"]
                or not isinstance(record, dict) or set(record) != {
                    "schema_version", "recipe_id", "recipe_sha256", "environment", "checks"}
                or record["schema_version"] != 1
                or record["recipe_id"] != recipe["recipe_id"]
                or record["recipe_sha256"] != recipe["recipe_sha256"]
                or record["environment"] not in ("isolated-local-fixture", "reviewed-local-procedure")
                or record["checks"] != {"procedure_executed": True, "result_verified": True}):
            reasons.add("reconstruction_evidence_unverified")
    return reasons


def _walk(root: Path, exclusions: set[Path], mounts: dict[Path, dict], decisions: dict[Path, str],
          reasons: set[str], counts: dict[str, int], metadata: "hashlib._Hash",
          links: dict[tuple[int, int], tuple[int, int]]) -> None:
    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        if path in exclusions:
            counts["excluded"] += 1
            continue
        if depth > 128 or counts["entries"] >= _MAX_ENTRIES:
            reasons.add("inventory_limit_reached")
            return
        mount = mounts.get(path)
        if mount is not None:
            choice = decisions.get(path)
            if choice is None:
                reasons.add("mount_boundary_undecided")
                continue
            if choice == "exclude":
                reasons.add("mount_exclusion_requires_exact_recipe")
                continue
        try:
            info = path.lstat()
        except OSError:
            reasons.add("unreadable_entry")
            continue
        counts["entries"] += 1
        mode = stat.S_IFMT(info.st_mode)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            reasons.add("unsupported_entry_type")
            continue
        try:
            attrs = sorted(os.listxattr(path, follow_symlinks=False))
            if len(attrs) > 256:
                raise OSError("too many metadata attributes")
            attr_digests = []
            for name in attrs:
                value = os.getxattr(path, name, follow_symlinks=False)
                if len(value) > 1024 * 1024:
                    raise OSError("metadata attribute too large")
                attr_digests.append((name, hashlib.sha256(value).hexdigest()))
        except OSError:
            reasons.add("metadata_unreadable")
            attr_digests = []
        metadata.update(_canonical([str(path), info.st_uid, info.st_gid, info.st_mode & 0o7777,
                                    info.st_nlink, mode, attr_digests]))
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            pair = (info.st_dev, info.st_ino)
            observed, expected = links.get(pair, (0, info.st_nlink))
            links[pair] = (observed + 1, expected)
        if stat.S_ISLNK(info.st_mode):
            counts["symlinks"] += 1
            continue
        if stat.S_ISREG(info.st_mode):
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("inventory changed")
                finally:
                    os.close(descriptor)
            except OSError:
                reasons.add("unreadable_file")
            counts["files"] += 1
            continue
        counts["directories"] += 1
        try:
            with os.scandir(path) as entries:
                children = sorted((Path(entry.path) for entry in entries), reverse=True)
        except OSError:
            reasons.add("unreadable_directory")
            continue
        stack.extend((child, depth + 1) for child in children)
def _scan(declaration: dict[str, Any], scope: dict[str, Any], *, inventory: InventoryPaths,
          policy_bytes: bytes, binding_bytes: bytes, enforce_mount_ids: bool = True) -> dict[str, Any]:
    reasons: set[str] = set()
    if scope["require_privileged"] and os.geteuid() != 0:
        reasons.add("privileged_inventory_required")
    users = _accounts(_bounded_read(inventory.passwd), passwd=True)
    groups = _accounts(_bounded_read(inventory.group), passwd=False)
    mount_inventory = _mount_inventory(_bounded_read(inventory.mountinfo))
    package_digest = hashlib.sha256(_bounded_read(inventory.packages)).hexdigest()
    units = _application_units(inventory.application_units)
    roots = {item["id"]: Path(item["path"]) for item in declaration["roots"]}
    # Only mounts whose data boundary can affect a reviewed root belong in
    # drift identity. systemd PrivateTmp and other unrelated namespace mounts
    # may have new kernel mount IDs on every invocation.
    mounts = [item for item in mount_inventory if any(
        _within(Path(item["mountpoint"]), root) or _within(root, Path(item["mountpoint"]))
        for root in roots.values())]
    stable_mounts = sorted(({key: item[key] for key in
                             ("root", "mountpoint", "filesystem", "source_digest")}
                            for item in mounts),
                           key=lambda item: (item["mountpoint"], item["root"],
                                             item["filesystem"], item["source_digest"]))
    if any(item["adapter"] != "files" for item in declaration["roots"]):
        reasons.add("capture_adapter_unsupported_for_coverage")
    exclusions = {Path(item["path"]) for item in declaration["exclusions"]}
    reasons.update(_evidence_reasons(declaration))
    for exclusion in declaration["exclusions"]:
        path = Path(exclusion["path"])
        root = next(item for item in declaration["roots"] if item["id"] == exclusion["root_id"])
        if root["adapter"] != "files" or not Path(root["path"]).is_dir():
            reasons.add("exclusion_root_not_directory")
        if _has_symlink_ancestor(path.parent):
            reasons.add("exclusion_linked_path")
        try:
            path.lstat()
        except OSError:
            reasons.add("exclusion_target_missing")
    decisions = {Path(item["mountpoint"]): item["decision"]
                 for item in declaration["mount_decisions"]}
    mount_by_path = {Path(item["mountpoint"]): item for item in mounts}
    if len(mount_by_path) != len(mounts):
        reasons.add("mount_inventory_ambiguous")
    for decision in declaration["mount_decisions"]:
        mount = mount_by_path.get(Path(decision["mountpoint"]))
        if mount is None or (enforce_mount_ids and mount["mount_id"] != decision["mount_id"]):
            reasons.add("mount_decision_stale")
        if decision["decision"] == "exclude" and Path(decision["mountpoint"]) not in exclusions:
            reasons.add("mount_exclusion_requires_exact_recipe")
    for mount in mounts:
        mountpoint = Path(mount["mountpoint"])
        if any(_within(mountpoint, root) for root in roots.values()) and mountpoint not in decisions:
            reasons.add("mount_boundary_undecided")
    identities = {(item["name"], item["home"]) for item in declaration["identity_decisions"]}
    for user in users:
        home = Path(user["home"])
        location = _scope_location(home, roots, exclusions) if home.is_absolute() else "external"
        if location != "captured" and (user["name"], user["home"]) not in identities:
            reasons.add("user_home_unaccounted")
    app_units = {item["unit"] for item in declaration["applications"]}
    if {item["name"] for item in units} - app_units:
        reasons.add("application_unaccounted")
    for app in declaration["applications"]:
        if app["unit"] not in {item["name"] for item in units}:
            reasons.add("application_unit_missing")
        for state_root in app["state_roots"]:
            location = _scope_location(Path(state_root), roots, exclusions)
            if (location == "captured" and not any(
                    _within(Path(state_root), Path(root["path"])) and app["source_id"] == root["source_id"]
                    for root in declaration["roots"])) or location in ("external", "linked"):
                reasons.add("application_state_unaccounted")
    if not any(_within(inventory.packages, root) for root in roots.values()) and declaration["package_reconstruction"] is None:
        reasons.add("package_reinstall_unaccounted")
    counts = {"roots": len(roots), "entries": 0, "files": 0, "directories": 0,
              "symlinks": 0, "excluded": 0, "users": len(users), "groups": len(groups),
              "mounts": len(mounts), "applications": len(units)}
    metadata = hashlib.sha256()
    links: dict[tuple[int, int], tuple[int, int]] = {}
    for root in roots.values():
        try:
            if _has_symlink_ancestor(root):
                reasons.add("root_symlink_unsupported")
                continue
        except OSError:
            reasons.add("root_unreadable")
            continue
        _walk(root, exclusions, mount_by_path, decisions, reasons, counts, metadata, links)
    if any(observed < expected for observed, expected in links.values()):
        reasons.add("hardlink_scope_unproven")
    structural = {"users": users, "groups": groups, "mounts": stable_mounts,
                  "applications": units, "declared_applications": declaration["applications"],
                  "roots": [{"id": item["id"], "path": item["path"], "source_id": item["source_id"],
                             "adapter": item["adapter"], "exists": Path(item["path"]).exists()}
                            for item in declaration["roots"]],
                  "package_digest": package_digest}
    return {"schema_version": 1, "declaration_digest": _digest(declaration),
            "scope_policy_digest": _digest(scope), "enrollment": evidence_bytes(policy_bytes, binding_bytes),
            "structural_digest": _digest(structural), "metadata_digest": metadata.hexdigest(),
            "counts": counts, "reasons": sorted(reasons), "roots": declaration["roots"]}


def _private_directory(state: Path, *, create: bool = True) -> Path:
    base = Path(state) / "coverage"
    try:
        if create:
            base.mkdir(mode=0o700, exist_ok=True)
        info = base.lstat()
    except OSError:
        raise CoverageError("Private coverage state is unavailable") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise CoverageError("Private coverage state is unsafe")
    return base


def _private_child(base: Path, name: str, *, create: bool) -> Path:
    child = base / name
    try:
        if create:
            child.mkdir(mode=0o700, exist_ok=True)
        info = child.lstat()
    except OSError:
        raise CoverageError("Private coverage record directory is unavailable") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise CoverageError("Private coverage record directory is unsafe")
    return child


def _active_directory(base: Path, declaration_path: Path, scope: dict[str, Any], *, create: bool) -> Path:
    profiles = _private_child(base, "profiles", create=create)
    key = _digest({"scope_policy_digest": _digest(scope),
                   "declaration_path": str(Path(declaration_path))})
    return _private_child(profiles, key, create=create)


def _write_immutable(directory: Path, value: dict[str, Any]) -> str:
    digest = _digest(value)
    target = directory / f"{digest}.json"
    descriptor, name = tempfile.mkstemp(prefix=".coverage-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(_canonical(value))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if hashlib.sha256(_read_private(target)).hexdigest() != digest:
                raise CoverageError("Immutable coverage record was altered") from None
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def _inputs(declaration_path: Path, scope_policy: Any):
    scope = validate_scope_policy(scope_policy)
    try:
        declaration = _declaration(strict_json(_read_private(Path(declaration_path)).decode("utf-8")), scope)
    except (UnicodeError, ConfigurationError):
        raise CoverageError("Invalid private coverage declaration JSON") from None
    return declaration, scope


def discover_coverage(declaration_path: Path, scope_policy: Any, *, state: Path,
                      policy_bytes: bytes, binding_bytes: bytes,
                      inventory: InventoryPaths = InventoryPaths()) -> dict[str, object]:
    """Inventory bounded private scope and persist an immutable review candidate."""
    verify_bytes(state, policy_bytes, binding_bytes)
    declaration, scope = _inputs(declaration_path, scope_policy)
    candidate = _scan(declaration, scope, inventory=inventory,
                      policy_bytes=policy_bytes, binding_bytes=binding_bytes)
    base = _private_directory(state)
    candidates = _private_child(base, "candidates", create=True)
    digest = _write_immutable(candidates, candidate)
    return {"coverage_status": "complete" if not candidate["reasons"] else "incomplete",
            "recovery_status": "untested", "candidate_digest": digest,
            "declaration_digest": candidate["declaration_digest"],
            "reasons": candidate["reasons"], "counts": candidate["counts"],
            "next_action": "Review the exact candidate" if not candidate["reasons"]
                           else "Resolve inventory gaps and discover again"}


def review_coverage(declaration_path: Path, scope_policy: Any, candidate_digest: str, *, state: Path,
                    policy_bytes: bytes, binding_bytes: bytes,
                    inventory: InventoryPaths = InventoryPaths()) -> dict[str, object]:
    """Approve only a complete, exact current candidate under the policy lease."""
    _sha(candidate_digest, "candidate")
    with policy_lease(state, exclusive=True):
        verify_bytes(state, policy_bytes, binding_bytes)
        declaration, scope = _inputs(declaration_path, scope_policy)
        base = _private_directory(state)
        candidate_path = _private_child(base, "candidates", create=False) / f"{candidate_digest}.json"
        try:
            recorded = strict_json(_read_private(candidate_path).decode("utf-8"))
        except (UnicodeError, ConfigurationError):
            raise CoverageError("Reviewed coverage candidate is invalid") from None
        current = _scan(declaration, scope, inventory=inventory,
                        policy_bytes=policy_bytes, binding_bytes=binding_bytes)
        if _digest(recorded) != candidate_digest or _digest(current) != candidate_digest:
            raise CoverageError("Coverage candidate changed; discover and review again")
        if current["reasons"]:
            raise CoverageError("Coverage candidate has unresolved inventory gaps")
        baseline = {"schema_version": 1, "candidate_digest": candidate_digest,
                    "structural_digest": current["structural_digest"],
                    "declaration_digest": current["declaration_digest"],
                    "scope_policy_digest": current["scope_policy_digest"],
                    "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        baselines = _private_child(base, "baselines", create=True)
        baseline_digest = _write_immutable(baselines, baseline)
        active = coverage_evidence_bytes(policy_bytes, binding_bytes,
            declaration_digest=current["declaration_digest"],
            scope_policy_digest=current["scope_policy_digest"], baseline_digest=baseline_digest)
        active["candidate_digest"] = candidate_digest
        active["structural_digest"] = current["structural_digest"]
        active_dir = _active_directory(base, declaration_path, scope, create=True)
        descriptor, name = tempfile.mkstemp(prefix=".coverage-active-", dir=active_dir)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(_canonical(active))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, active_dir / "active.json")
            fd = os.open(active_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"coverage_status": "complete", "recovery_status": "untested",
            "baseline_digest": baseline_digest, "candidate_digest": candidate_digest,
            "next_action": "Run coverage check before capture"}


def check_coverage(declaration_path: Path, scope_policy: Any, *, state: Path,
                   policy_bytes: bytes, binding_bytes: bytes,
                   inventory: InventoryPaths = InventoryPaths()) -> CoverageCheck:
    """Verify enrollment, immutable baseline and current structural coverage."""
    try:
        verify_bytes(state, policy_bytes, binding_bytes)
        declaration, scope = _inputs(declaration_path, scope_policy)
        base = _private_directory(state, create=False)
        active_dir = _active_directory(base, declaration_path, scope, create=False)
        active = strict_json(_read_private(active_dir / "active.json").decode("utf-8"))
        if not isinstance(active, dict) or set(active) != {
                "schema_version", "machine_id_digest", "euid", "policy_digest", "bindings_digest",
                "declaration_digest", "scope_policy_digest", "baseline_digest",
                "candidate_digest", "structural_digest"}:
            raise CoverageError("Coverage active record is invalid")
        baseline_digest = _sha(active["baseline_digest"], "baseline")
        baseline = strict_json(_read_private(_private_child(base, "baselines", create=False) /
                                             f"{baseline_digest}.json").decode("utf-8"))
        if (not isinstance(baseline, dict)
                or set(baseline) != {"schema_version", "candidate_digest", "structural_digest",
                                     "declaration_digest", "scope_policy_digest", "reviewed_at"}
                or baseline.get("schema_version") != 1
                or _digest(baseline) != baseline_digest
                or active.get("candidate_digest") != baseline.get("candidate_digest")
                or active.get("structural_digest") != baseline.get("structural_digest")
                or active.get("declaration_digest") != baseline.get("declaration_digest")
                or active.get("scope_policy_digest") != baseline.get("scope_policy_digest")):
            raise CoverageError("Coverage baseline was altered")
    except (ConfigurationError, CoverageError, OSError, UnicodeError, KeyError):
        return CoverageCheck("unenrolled", ("coverage_enrollment_missing_or_invalid",), None, None, {})
    expected = coverage_evidence_bytes(policy_bytes, binding_bytes,
        declaration_digest=_digest(declaration), scope_policy_digest=_digest(scope),
        baseline_digest=baseline_digest)
    if any(active.get(key) != value for key, value in expected.items()):
        return CoverageCheck("drift", ("coverage_enrollment_changed",), baseline_digest, None, {})
    try:
        current = _scan(declaration, scope, inventory=inventory,
                        policy_bytes=policy_bytes, binding_bytes=binding_bytes,
                        enforce_mount_ids=False)
    except CoverageError:
        return CoverageCheck("incomplete", ("inventory_unavailable",), baseline_digest, None, {})
    candidate_digest = _digest(current)
    if current["structural_digest"] != baseline.get("structural_digest"):
        return CoverageCheck("drift", tuple(sorted(set(current["reasons"]) | {"structural_inventory_changed"})),
                             baseline_digest, candidate_digest, current["counts"])
    if current["reasons"]:
        return CoverageCheck("incomplete", tuple(current["reasons"]), baseline_digest,
                             candidate_digest, current["counts"])
    roots = tuple((root["id"], Path(root["path"]), root["source_id"], root["adapter"])
                  for root in declaration["roots"])
    exclusions = tuple(Path(item["path"]) for item in declaration["exclusions"])
    return CoverageCheck("complete", (), baseline_digest, candidate_digest,
                         current["counts"], roots, exclusions)


def evaluate_recovery_evidence(evidence_path: Path | None, *, baseline_digest: str,
                               snapshot_id: str, source_machine_digest: str) -> dict[str, object]:
    """Treat isolated fixture drills as emulation, never whole-machine qualification."""
    for value, label in ((baseline_digest, "baseline"), (snapshot_id, "snapshot"),
                         (source_machine_digest, "machine")):
        _sha(value, label)
    if evidence_path is None:
        return {"recovery_status": "untested", "reasons": ["recovery_evidence_missing"],
                "next_action": "Run an isolated replacement recovery drill"}
    try:
        record = strict_json(_read_private(Path(evidence_path)).decode("utf-8"))
    except (ConfigurationError, UnicodeError):
        raise CoverageError("Invalid private recovery evidence") from None
    required = {"schema_version", "environment", "baseline_digest", "snapshot_id",
                "replacement_machine_digest", "independent_escrow", "checks"}
    if not isinstance(record, dict) or set(record) != required or record.get("schema_version") != 1:
        raise CoverageError("Invalid recovery evidence schema")
    checks = record["checks"]
    expected_checks = {"metadata", "secrets", "users", "reinstall", "app_data", "startup"}
    if not isinstance(checks, dict) or set(checks) != expected_checks or any(type(value) is not bool for value in checks.values()):
        raise CoverageError("Recovery evidence checks are incomplete")
    if record["baseline_digest"] != baseline_digest or record["snapshot_id"] != snapshot_id:
        raise CoverageError("Recovery evidence differs from exact baseline or snapshot")
    replacement = _sha(record["replacement_machine_digest"], "replacement machine")
    if record["environment"] != "isolated-local-fixture":
        return {"recovery_status": "untested", "reasons": ["independent_qualification_not_verified"],
                "next_action": "Verify signed independent replacement evidence"}
    reasons = [name + "_unverified" for name, passed in checks.items() if not passed]
    if record["independent_escrow"] is not True:
        reasons.append("independent_escrow_unverified")
    if replacement == source_machine_digest:
        reasons.append("replacement_identity_not_distinct")
    return {"recovery_status": "emulated" if not reasons else "untested", "reasons": sorted(reasons),
            "baseline_digest": baseline_digest, "snapshot_id": snapshot_id,
            "next_action": "Obtain signed independent replacement evidence"}
